"""`fleet wake`: the packet, what the probe reads of a machine's cards, who sends the
packet, and what the command says. Nothing here sends anything on the network."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess

import pytest
from typer.testing import CliRunner

from fleet import cli, notes
from fleet.models import Device, Kind, Nic, ProbeResult, Snapshot, Status
from fleet.ops import doctor, wake
from fleet.probe.parse import parse_payload
from fleet.render.view import device_view
from fleet.state import inventory as inv
from fleet.state import store

HOME_GW = "02:00:00:00:00:fe"
TOWER_MAC = "02:00:00:00:00:01"


def _snap(mac=TOWER_MAC, ip="192.168.0.105", gw=HOME_GW, wol=True, **kw) -> dict:
    return Snapshot(hostname="h", nics=[Nic("enp4s0", mac, ip, 24 if ip else None, wol)],
                    gateway_mac=gw, **kw).to_dict()


def _dev(name, kind=Kind.PERMANENT) -> Device:
    return Device(id=f"id:{name}", name=name, kind=kind,
                  endpoints=[{"target": f"{name}.lan", "user": "me", "port": 22}])


# ------------------------------------------------------------------ the packet
def test_magic_packet_is_102_bytes_of_ff_then_the_mac_sixteen_times():
    p = wake.magic_packet("AA:BB:CC:DD:EE:FF")
    mac = bytes.fromhex("aabbccddeeff")
    assert len(p) == 102
    assert p[:6] == b"\xff" * 6
    assert p[6:12] == mac
    assert p == b"\xff" * 6 + mac * 16


@pytest.mark.parametrize("text", ["AA:BB:CC:DD:EE:FF", "aa-bb-cc-dd-ee-ff", "aabb.ccdd.eeff",
                                  "AABBCCDDEEFF", " aa:bb:cc:dd:ee:ff "])
def test_a_mac_is_read_however_it_is_written(text):
    assert notes.normalise_mac(text) == "aa:bb:cc:dd:ee:ff"


def test_macos_arp_drops_leading_zeros():
    assert notes.normalise_mac("0:11:2:33:4:55") == "00:11:02:33:04:55"


@pytest.mark.parametrize("text", ["zz:bb:cc:dd:ee:ff", "aa:bb", "", None, "00:00:00:00:00:00",
                                  "ff:ff:ff:ff:ff:ff", "aa:bb:cc:dd:ee:ff:00"])
def test_what_is_not_a_card_address_is_refused(text):
    assert notes.normalise_mac(text) is None


@pytest.mark.parametrize("ip,prefix,want", [("192.168.1.20", 24, "192.168.1.255"),
                                            ("10.0.5.9", 16, "10.0.255.255"),
                                            ("", None, "255.255.255.255"),
                                            ("10.0.0.1", 32, "255.255.255.255")])
def test_broadcast_address(ip, prefix, want):
    assert wake.broadcast_for(ip, prefix) == want


# ------------------------------------------------------------------ what the probe reads
PAYLOAD = """\
#FLEET v1
host.hostname=tower
#NIC name|mac|ipv4|prefix|wol
enp4s0|02:00:00:00:00:01|192.168.0.105|24|1
wlo1|02-00-00-00-00-03|||
eno1|02:00:00:00:00:02|169.254.1.2|16|0
bad|not-a-mac|1.2.3.4|8|1
#GATEWAY ip|mac
192.168.0.1|02:00:00:00:00:fe
#END rc=0
"""


def test_the_probe_reads_each_card_and_the_router():
    snap = parse_payload(PAYLOAD)
    assert snap.nics[0] == Nic("enp4s0", TOWER_MAC, "192.168.0.105", 24, True)
    assert snap.nics[1] == Nic("wlo1", "02:00:00:00:00:03", "", None, None)
    # a card that found no network has no subnet worth broadcasting on
    assert snap.nics[2].ipv4 == "" and snap.nics[2].wol is False
    assert len(snap.nics) == 3
    assert snap.gateway_mac == HOME_GW


def test_an_older_probe_without_the_sections_still_parses():
    snap = parse_payload("#FLEET v1\nhost.hostname=x\n#END rc=0\n")
    assert snap.nics == [] and snap.gateway_mac == ""


@pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="POSIX payload")
def test_this_machines_probe_carries_the_sections():
    from fleet.probe.runner import run_probe_local

    res = run_probe_local(light=True)
    assert res.ok, res.error_detail
    assert res.snapshot.gateway_mac == "" or notes.normalise_mac(res.snapshot.gateway_mac)
    assert all(notes.normalise_mac(n.mac) for n in res.snapshot.nics)


# ------------------------------------------------------------------ machine.yaml
def test_machine_yaml_names_the_card():
    skipped: list[str] = []
    out = notes.check({"wake": {"mac": ["AA-BB-CC-DD-EE-FF", "nope", "aa:bb:cc:dd:ee:ff"]}},
                      skipped)
    assert out["wake"] == {"macs": ["aa:bb:cc:dd:ee:ff"]}
    assert any("nope" in s for s in skipped)
    assert notes.check({"wake": {"mac": "11:22:33:44:55:66"}})["wake"]["macs"] == \
        ["11:22:33:44:55:66"]
    skipped = []
    assert "wake" not in notes.check({"wake": "aa:bb:cc:dd:ee:ff"}, skipped)
    assert skipped


def test_the_declared_card_wins_over_what_the_probe_saw():
    snap = _snap()
    snap["notes"] = {"wake": {"macs": ["11:22:33:44:55:66"]}}
    assert wake.target_macs(snap) == ["11:22:33:44:55:66"]


def test_a_card_known_to_be_unarmed_is_not_sent_to():
    snap = _snap(wol=False)
    assert wake.target_macs(snap) == []
    snap["nics"].append({"name": "eth1", "mac": "11:22:33:44:55:66", "ipv4": "",
                         "prefix": None, "wol": None})
    assert wake.target_macs(snap) == ["11:22:33:44:55:66"]


def test_the_cards_in_use_are_preferred_to_idle_ones():
    snap = _snap()
    snap["nics"].append({"name": "en5", "mac": "11:22:33:44:55:66", "ipv4": "",
                         "prefix": None, "wol": True})
    assert wake.target_macs(snap) == [TOWER_MAC]


# ------------------------------------------------------------------ who sends it
def test_same_lan_is_the_same_router_not_the_same_subnet():
    a, b = _snap(), _snap(ip="192.168.0.12", gw="00:11:22:33:44:55")
    assert wake.same_lan(a, b) == ""
    assert wake.same_lan(a, _snap(ip="192.168.0.12")) == "gateway"
    assert wake.same_lan(a, _snap(ip="192.168.0.12", gw="")) == "subnet"
    assert wake.same_lan(a, _snap(ip="10.1.0.2", gw="")) == ""


def test_the_relay_is_an_awake_machine_on_the_targets_lan():
    tower, ws, cloud = _dev("gpu-tower"), _dev("workstation"), _dev("cloud-1")
    others = [(ws, {"status": "ok", "last_ok_at": 10}, _snap(ip="192.168.0.12")),
              (cloud, {"status": "ok", "last_ok_at": 20}, _snap(ip="10.0.0.61", gw="02:00:00:00:00:ff"))]
    away = _snap(ip="172.20.10.3", gw="aa:aa:aa:aa:aa:01")
    senders = wake.choose_senders(tower, _snap(), away, others)
    assert [(s.name, s.how) for s in senders] == [("workstation", "gateway")]


def test_a_machine_that_is_down_is_never_the_relay():
    tower, ws = _dev("gpu-tower"), _dev("workstation")
    others = [(ws, {"status": "timeout"}, _snap(ip="192.168.0.12"))]
    assert wake.choose_senders(tower, _snap(), None, others) == []


def test_own_machines_go_before_a_rental_then_the_most_recently_seen():
    tower = _dev("gpu-tower")
    a, b, c = _dev("a", Kind.SHARED), _dev("b"), _dev("c")
    others = [(a, {"status": "ok", "last_ok_at": 99}, _snap()),
              (b, {"status": "ok", "last_ok_at": 5}, _snap()),
              (c, {"status": "ok", "last_ok_at": 50}, _snap()),
              (_dev("r", Kind.RENTAL), {"status": "ok"}, _snap())]
    assert [s.name for s in wake.choose_senders(tower, _snap(), None, others)] == ["c", "b", "a"]


def test_this_machine_sends_when_it_is_on_the_lan():
    senders = wake.choose_senders(_dev("gpu-tower"), _snap(), _snap(ip="192.168.0.34"),
                                  [(_dev("ws"), {"status": "ok"}, _snap())], here_name="laptop")
    assert (senders[0].name, senders[0].device, senders[0].how) == ("laptop", None, "local")
    assert senders[1].name == "ws"


def test_via_is_taken_as_given():
    ws = _dev("ws")
    assert wake.choose_senders(_dev("t"), _snap(), None, [], via=ws) == \
        [wake.Sender("ws", ws, "via")]


class _Sock:
    def __init__(self, *a, refuse=()):
        self.sent, self.opts, self.refuse = [], [], refuse

    def setsockopt(self, *a):
        self.opts.append(a)

    def sendto(self, data, addr):
        if addr[0] in self.refuse:
            raise OSError(65, "No route to host")
        self.sent.append((data, addr))

    def close(self):
        pass


def test_sending_from_here_goes_to_both_ports_three_times():
    s = _Sock()
    wake.send_local([TOWER_MAC], "192.168.0.255", sock_factory=lambda *a: s, sleep=lambda _: None)
    assert (socket.SOL_SOCKET, socket.SO_BROADCAST, 1) in s.opts
    assert {a for _, a in s.sent} == {("192.168.0.255", 9), ("192.168.0.255", 7),
                                      ("255.255.255.255", 9), ("255.255.255.255", 7)}
    assert len(s.sent) == 3 * 2 * 2
    assert all(d == wake.magic_packet(TOWER_MAC) for d, _ in s.sent)


def test_one_address_refused_is_not_a_failure_but_all_are():
    # macOS: "No route to host" for 255.255.255.255 on an unbound socket
    s = _Sock(refuse=("255.255.255.255",))
    wake.send_local([TOWER_MAC], "192.168.0.255", sock_factory=lambda *a: s, sleep=lambda _: None)
    assert s.sent
    s = _Sock(refuse=("192.168.0.255", "255.255.255.255"))
    with pytest.raises(OSError):
        wake.send_local([TOWER_MAC], "192.168.0.255", sock_factory=lambda *a: s,
                        sleep=lambda _: None)


@pytest.mark.skipif(os.name == "nt" or not shutil.which("sh"), reason="POSIX shell")
def test_the_relay_script_is_valid_shell_and_python():
    script = wake.relay_script([TOWER_MAC], "192.168.0.255", windows=False)
    assert TOWER_MAC in script and "192.168.0.255" in script
    assert subprocess.run(["sh", "-n"], input=script.encode()).returncode == 0
    py = script.split("python3 -c '", 1)[1].split("'\n", 1)[0]
    compile(py, "relay", "exec")


def test_the_windows_relay_uses_udpclient():
    script = wake.relay_script([TOWER_MAC], "192.168.0.255", windows=True)
    assert "System.Net.Sockets.UdpClient" in script and "EnableBroadcast" in script
    assert f"'{TOWER_MAC}'" in script and "9, 7" in script


def test_a_relay_that_fails_says_why():
    ws = _dev("ws")
    ok, why = wake.send(wake.Sender("ws", ws, "gateway"), [TOWER_MAC], "192.168.0.255",
                        endpoint=object(),
                        run=lambda *a, **k: (False, "ssh: connect to host ws.lan port 22: "
                                                    "Operation timed out"))
    assert not ok and "timed out" in why


def test_a_rental_or_an_unknown_card_is_not_wakeable():
    assert wake.wakeable(_dev("t"), _snap())
    assert not wake.wakeable(_dev("t", Kind.RENTAL), _snap())
    assert not wake.wakeable(_dev("t"), None)


# ------------------------------------------------------------------ the command
@pytest.fixture
def fleet(monkeypatch):
    """laptop (this machine, away from home), gpu-tower asleep, workstation awake."""
    devs = [_dev("laptop"), _dev("gpu-tower"), _dev("workstation"), _dev("vast", Kind.RENTAL)]
    inv.save(devs)
    conn = store.connect()
    try:
        for name, snap in (("gpu-tower", _snap()), ("workstation", _snap(ip="192.168.0.12")),
                           ("vast", _snap(ip="10.9.0.2", gw="aa:aa:aa:aa:aa:02"))):
            store.record(conn, f"id:{name}", ProbeResult(status=Status.OK,
                                                         snapshot=Snapshot(**_snap_args(snap))))
        store.record(conn, "id:gpu-tower", ProbeResult(status=Status.TIMEOUT,
                                                       error_detail="timed out"))
    finally:
        conn.close()
    monkeypatch.setattr(cli.identity, "local_device_id", lambda: "id:laptop")
    away = Snapshot(**_snap_args(_snap(ip="172.20.10.3", gw="aa:aa:aa:aa:aa:01")))
    monkeypatch.setattr(cli, "run_probe_local", lambda **k: ProbeResult(Status.OK, snapshot=away))
    sent: list = []

    def run_script(ep, script, *, platform="posix", timeout=30):
        sent.append((ep.target, script))
        return True, ""
    monkeypatch.setattr("fleet.ops.remote.run_script", run_script)
    monkeypatch.setattr(wake, "send_local", lambda *a, **k: sent.append(("here", a)))
    monkeypatch.setattr(cli.time, "sleep", lambda s: None)
    return sent


def _snap_args(d: dict) -> dict:
    return {"hostname": d["hostname"], "gateway_mac": d["gateway_mac"],
            "nics": [Nic(**n) for n in d["nics"]]}


def _answers(monkeypatch, statuses):
    seq = iter(statuses)
    monkeypatch.setattr(cli, "_rows", lambda names, **k: [{"status": next(seq)}])


def test_wake_relays_through_the_awake_machine_and_waits_for_it(fleet, monkeypatch):
    _answers(monkeypatch, ["timeout", "refused", "ok"])
    r = CliRunner().invoke(cli.app, ["wake", "gpu-tower", "--json"])
    assert r.exit_code == 0, r.output
    out = json.loads(r.output)
    assert out["woke"] and out["via"] == "workstation" and out["how"] == "gateway"
    assert out["macs"] == [TOWER_MAC] and out["broadcast"] == "192.168.0.255"
    assert [t for t, _ in fleet] == ["workstation.lan"]
    assert TOWER_MAC in fleet[0][1]


def test_wake_without_waiting(fleet, monkeypatch):
    monkeypatch.setattr(cli, "_rows", lambda *a, **k: pytest.fail("probed"))
    r = CliRunner().invoke(cli.app, ["wake", "gpu-tower", "--no-wait", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["sent"] is True


def test_a_machine_that_never_answers_is_exit_1_and_says_the_packet_went(fleet, monkeypatch):
    monkeypatch.setattr(cli, "_rows", lambda *a, **k: [{"status": "timeout"}])
    r = CliRunner().invoke(cli.app, ["wake", "gpu-tower", "--timeout", "0"])
    assert r.exit_code == 1
    assert "did not answer" in r.output and "went out" in r.output


def test_a_refused_key_is_awake(fleet, monkeypatch):
    _answers(monkeypatch, ["auth_failed"])
    r = CliRunner().invoke(cli.app, ["wake", "gpu-tower"])
    assert r.exit_code == 0, r.output
    assert "fleet doctor gpu-tower" in r.output


def test_the_next_relay_is_tried_when_one_fails(fleet, monkeypatch):
    inv.save([*inv.load(), _dev("nas")])
    conn = store.connect()
    try:
        store.record(conn, "id:nas", ProbeResult(status=Status.OK,
                                                 snapshot=Snapshot(**_snap_args(_snap(ip="192.168.0.2")))))
    finally:
        conn.close()
    calls = []

    def run_script(ep, script, **k):
        calls.append(ep.target)
        return (False, "timed out") if len(calls) == 1 else (True, "")
    monkeypatch.setattr("fleet.ops.remote.run_script", run_script)
    r = CliRunner().invoke(cli.app, ["wake", "gpu-tower", "--no-wait", "--json"])
    out = json.loads(r.output)
    assert r.exit_code == 0 and len(out["tried"]) == 2
    assert out["tried"][0]["ok"] is False and out["tried"][0]["error"] == "timed out"


def test_every_relay_failing_is_exit_1(fleet, monkeypatch):
    monkeypatch.setattr("fleet.ops.remote.run_script", lambda *a, **k: (False, "no python3"))
    r = CliRunner().invoke(cli.app, ["wake", "gpu-tower", "--json"])
    assert r.exit_code == 1
    assert json.loads(r.output)["error"] == "the packet could not be sent"


def test_from_home_the_packet_goes_from_here(fleet, monkeypatch):
    home = Snapshot(**_snap_args(_snap(ip="192.168.0.34")))
    monkeypatch.setattr(cli, "run_probe_local", lambda **k: ProbeResult(Status.OK, snapshot=home))
    r = CliRunner().invoke(cli.app, ["wake", "gpu-tower", "--no-wait", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.output)["how"] == "local" and fleet[0][0] == "here"


def test_no_machine_up_on_its_lan(fleet, monkeypatch):
    conn = store.connect()
    try:
        store.record(conn, "id:workstation", ProbeResult(status=Status.TIMEOUT))
    finally:
        conn.close()
    r = CliRunner().invoke(cli.app, ["wake", "gpu-tower", "--json"])
    assert r.exit_code == 2
    out = json.loads(r.output)
    assert "no machine on gpu-tower's network" in out["error"] and "--via" in out["fix"][0]


@pytest.mark.parametrize("name,words", [("vast", "provider"), ("laptop", "this machine")])
def test_refusals(fleet, name, words):
    r = CliRunner().invoke(cli.app, ["wake", name])
    assert r.exit_code == 2 and words in r.output


def test_unknown_name(fleet):
    r = CliRunner().invoke(cli.app, ["wake", "gpu-towr"])
    assert r.exit_code == 1 and "No device named" in r.output


def test_already_up_sends_nothing(fleet):
    r = CliRunner().invoke(cli.app, ["wake", "workstation", "--json"])
    assert r.exit_code == 0
    assert json.loads(r.output)["already_up"] and fleet == []


def test_no_card_known(fleet):
    inv.save([*inv.load(), _dev("old")])
    conn = store.connect()
    try:
        store.record(conn, "id:old", ProbeResult(status=Status.OK, snapshot=Snapshot()))
        store.record(conn, "id:old", ProbeResult(status=Status.TIMEOUT))
    finally:
        conn.close()
    r = CliRunner().invoke(cli.app, ["wake", "old", "--json"])
    assert r.exit_code == 2
    out = json.loads(r.output)
    assert "network card" in out["error"] and any("wake: {mac:" in f for f in out["fix"])


def test_cards_all_unarmed_says_how_to_arm_them(fleet):
    inv.save([*inv.load(), _dev("off")])
    conn = store.connect()
    try:
        store.record(conn, "id:off", ProbeResult(
            status=Status.OK, snapshot=Snapshot(**_snap_args(_snap(wol=False)))))
        store.record(conn, "id:off", ProbeResult(status=Status.TIMEOUT))
    finally:
        conn.close()
    r = CliRunner().invoke(cli.app, ["wake", "off", "--json"])
    assert r.exit_code == 2
    out = json.loads(r.output)
    assert "Wake-on-LAN is off" in out["error"]
    assert any("ethtool -s IFACE wol g" in f for f in out["fix"])


# ------------------------------------------------------------------ the hints
def test_ls_marks_an_offline_machine_that_can_be_woken():
    tower = _dev("gpu-tower")
    assert device_view(tower, {"status": "timeout"}, _snap())["wakeable"] is True
    assert device_view(tower, {"status": "ok"}, _snap())["wakeable"] is False
    assert device_view(_dev("v", Kind.RENTAL), {"status": "timeout"}, _snap())["wakeable"] is False


def test_doctor_suggests_waking_an_offline_machine():
    tower = _dev("gpu-tower")
    v = doctor.advise(tower, "timeout", "timed out", snap=_snap())
    assert v.fix[0] == "if it is asleep:  fleet wake gpu-tower"
    v = doctor.advise(tower, "timeout", "timed out", snap=None)
    assert not any("fleet wake" in f for f in v.fix)
