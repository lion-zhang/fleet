"""Finding the machines you can already reach, and saying why one will not let us in.

fleet manages no access, so these two are how it stays easy: discovery reads the places
you already list machines (ssh config, tailnet), and doctor turns a refusal into the one
line that fixes it -- for the user to run, since access is theirs to grant.
"""

from __future__ import annotations

import json

import pytest
from typer.testing import CliRunner

from fleet import cli
from fleet.models import Device, Kind, ProbeResult, Status
from fleet.ops import discover, doctor
from fleet.ssh import keys
from fleet.state import inventory as inv

# What `tailscale status --json` says, trimmed to the fields read.
STATUS = {"Peer": {
    "n1": {"HostName": "GPU-Box", "DNSName": "gpu-box.example.ts.net.", "OS": "linux",
           "Online": True, "TailscaleIPs": ["100.64.0.5"]},
    "n2": {"HostName": "iPhone", "DNSName": "iphone.example.ts.net.", "OS": "iOS",
           "Online": True, "TailscaleIPs": ["100.64.0.6"]},
    "n3": {"HostName": "studio", "DNSName": "studio.example.ts.net.", "OS": "macOS",
           "Online": False, "TailscaleIPs": ["100.64.0.7"]},
}}


@pytest.fixture
def home(tmp_path, monkeypatch):
    (tmp_path / ".ssh").mkdir()
    # Where ssh would dial a Host is asked of `ssh -G`; no test here runs ssh.
    monkeypatch.setattr(discover, "_resolved", lambda host, config, **k: None)
    return tmp_path


def test_hosts_are_read_from_ssh_config_with_includes_and_without_patterns(home):
    (home / ".ssh" / "config").write_text(
        "Host *\n  ServerAliveInterval 30\nHost lab lab-alt\n  HostName 10.0.0.9\n"
        "Host !nope *.corp\nInclude conf.d/*\nHost localhost\n")
    (home / ".ssh" / "conf.d").mkdir()
    (home / ".ssh" / "conf.d" / "vast").write_text("Host vast-a100  # rental\n  Port 40001\n")
    names = [c.name for c in discover.ssh_config(home)]
    assert names == ["lab", "lab-alt", "vast-a100"]


def test_where_git_is_pushed_is_not_offered_as_a_machine(home):
    """`fleet ls --discover` listed github.com, and a second GitHub account's alias,
    as machines to add: ssh there has no shell to give."""
    (home / ".ssh" / "config").write_text(
        "Host github.com\n  User git\nHost github-work\n  HostName github.com\n"
        "  IdentityFile ~/.ssh/work\nHost gitlab.corp.example\n  User git\n"
        "Host hf.co\nHost lab\n  HostName 10.0.0.9\n  User me\n")
    assert [c.name for c in discover.ssh_config(home)] == ["lab"]


def test_host_lines_are_read_as_ssh_reads_them(home):
    """A tab after `Host` hid the machine; the "=" of `Host = gpu` was listed as one;
    `Host "gpu"` kept its quotes, and a quoted name with a space was split in two."""
    (home / ".ssh" / "config").write_text(
        'Host\ttabbed\nHost = eq\nHost=tight\nHost "quoted"\nHost "has space" ok\n')
    assert [c.name for c in discover.ssh_config(home)] == ["tabbed", "eq", "tight",
                                                           "quoted", "ok"]


def test_a_relative_include_in_an_included_file_is_read_from_dot_ssh(home):
    """ssh reads `Include work/*` against ~/.ssh wherever the line is; fleet read it
    against the folder of the file holding it, and listed none of those hosts."""
    ssh = home / ".ssh"
    (ssh / "config").write_text("Include conf.d/*\n")
    (ssh / "conf.d").mkdir()
    (ssh / "conf.d" / "all").write_text("Include work/*\n")
    (ssh / "work").mkdir()
    (ssh / "work" / "boxes").write_text("Host work-gpu\n")
    assert [c.name for c in discover.ssh_config(home)] == ["work-gpu"]


def test_an_include_that_includes_itself_is_read_once(home):
    (home / ".ssh" / "config").write_text("Include config\nHost a\n")
    assert [c.name for c in discover.ssh_config(home)] == ["a"]


def test_tailnet_peers_that_can_run_sshd_are_listed_and_phones_are_not():
    got = {c.name: c for c in discover.tailnet(STATUS)}
    assert set(got) == {"gpu-box", "studio"}
    assert got["gpu-box"].target == "gpu-box.example.ts.net"
    assert got["gpu-box"].ssh_command == "ssh gpu-box.example.ts.net"
    assert got["studio"].online is False and got["studio"].os == "macos"


def test_no_tailscale_is_no_tailnet_not_an_error(monkeypatch):
    monkeypatch.setattr(discover, "_tailscale", lambda: "")
    assert discover.tailnet_status() is None and discover.tailnet() == []


def test_a_machine_already_added_is_marked_with_its_name(home):
    (home / ".ssh" / "config").write_text("Host gpu-box\n")
    inv.save([Device(id="id:g", name="big",
                     endpoints=[{"target": "gpu-box.example.ts.net", "user": "me"}])])
    got = discover.candidates(home=home, status=STATUS)
    # one machine, from whichever source named it first
    assert [c.name for c in got].count("gpu-box") == 1
    by = {c.name: c for c in got}
    assert by["studio"].added == ""


def test_a_host_that_dials_an_added_machine_is_not_offered_as_new(home, monkeypatch):
    """`Host lab` pointing at 10.0.0.9:2222 is the machine added as `ssh -p 2222
    me@10.0.0.9`. Matched by name alone, discover offered it as a machine to add."""
    (home / ".ssh" / "config").write_text("Host lab\n  HostName 10.0.0.9\n  Port 2222\n"
                                          "Host other\n  HostName 10.0.0.9\n")
    inv.save([Device(id="id:l", name="box",
                     endpoints=[{"target": "10.0.0.9", "user": "me", "port": 2222}])])
    where = {"lab": ("10.0.0.9", 2222), "other": ("10.0.0.9", 22)}
    monkeypatch.setattr(discover, "_resolved", lambda host, config, **k: where[host])
    by = {c.name: c for c in discover.candidates(home=home, status={})}
    assert by["lab"].added == "box"
    assert by["other"].added == ""         # same address, another port: another machine


def test_tailnet_peers_listed_by_address_are_not_one_machine(home):
    """With no DNS name a peer is listed by its 100.x address, and the part before the
    first dot -- "100" -- made every peer after the first look like a duplicate."""
    status = {"Peer": {k: {**v, "DNSName": ""} for k, v in STATUS["Peer"].items()}}
    assert {c.target for c in discover.candidates(home=home, status=status)} == {
        "100.64.0.5", "100.64.0.7"}


def test_ls_discover_lists_them_as_json(home, monkeypatch):
    monkeypatch.setattr(discover, "candidates",
                        lambda **k: discover.tailnet(STATUS))
    r = CliRunner().invoke(cli.app, ["ls", "--discover", "--json"])
    assert r.exit_code == 0, r.output
    names = [c["name"] for c in json.loads(r.stdout)["candidates"]]
    assert names == ["gpu-box", "studio"]


def test_adding_a_discovered_name_uses_the_command_that_reaches_it(monkeypatch):
    monkeypatch.setattr(discover, "candidates", lambda **k: discover.tailnet(STATUS))
    assert cli._discovered_command("gpu-box") == "ssh gpu-box.example.ts.net"
    assert cli._discovered_command("ssh root@1.2.3.4") == "ssh root@1.2.3.4"
    assert cli._discovered_command("unknown") == "unknown"   # a bare ssh alias of its own


# ---------------------------------------------------------------- doctor
def _dev(**kw):
    d = Device(id="id:x", name="box", kind=kw.pop("kind", Kind.PERMANENT),
               endpoints=[{"target": "10.0.0.5", "user": "me", "port": 22}])
    for k, v in kw.items():
        setattr(d, k, v)
    return d


@pytest.fixture
def mykey(tmp_path, monkeypatch):
    pub = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIEfO2BAfm3M8YGY39abf7HwQ9kNkKOR7nesP3z7/PatB me"
    monkeypatch.setattr(keys, "public_key", lambda *a: (tmp_path / "id.pub", pub))
    return pub


def test_a_refused_linux_machine_is_told_the_line_that_puts_your_key_on_it(mykey):
    v = doctor.advise(_dev(), Status.AUTH_FAILED.value, "Permission denied (publickey)")
    assert "does not accept your key" in v.why
    assert any(mykey in line and "authorized_keys" in line for line in v.fix)


def test_a_refused_windows_machine_gets_powershell_for_both_kinds_of_user(mykey):
    v = doctor.advise(_dev(), Status.AUTH_FAILED.value, "denied",
                      server="OpenSSH_for_Windows_9.5")
    text = "\n".join(v.fix)
    assert "administrators_authorized_keys" in text and "$HOME\\.ssh\\authorized_keys" in text
    assert "icacls" in text, "sshd ignores the admin file unless only SYSTEM and admins can write it"


def test_tailscale_ssh_points_at_the_policy_not_at_a_key(mykey):
    v = doctor.advise(_dev(), Status.AUTH_FAILED.value, "denied", server="Tailscale")
    assert "policy" in v.why and not any("authorized_keys" in f for f in v.fix)


def test_a_tailscale_check_rule_is_named_for_what_it_is(mykey):
    v = doctor.advise(_dev(), Status.AUTH_FAILED.value, "",
                      server="Tailscale",
                      verbose="# Tailscale SSH requires an additional check. To authenticate, "
                              "visit: https://login.tailscale.com/a/abc")
    assert "browser" in v.why and any('"accept"' in f for f in v.fix)


def test_a_rental_is_told_to_add_the_key_to_the_provider_account(mykey):
    v = doctor.advise(_dev(kind=Kind.RENTAL, provider="vastai"), Status.AUTH_FAILED.value,
                      "denied")
    assert "provider" in v.fix[0] and "vast.ai" in v.fix[0]


@pytest.mark.skipif(not __import__("shutil").which("ssh-keygen"), reason="needs ssh-keygen")
def test_a_machine_pinned_to_a_key_is_told_that_key_not_yours(tmp_path, monkeypatch, mykey):
    """Added with `-i key.pem` it is reached with IdentitiesOnly, so only that key gets in.
    With no .pub beside the .pem, doctor gave the line for ~/.ssh/id_ed25519.pub -- which
    let nothing in -- or, with no key there, "you have no ssh key yet"."""
    import subprocess

    key = tmp_path / "rental.pem"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)], check=True)
    pub = (tmp_path / "rental.pem.pub").read_text().strip()
    (tmp_path / "rental.pem.pub").unlink()
    dev = _dev()
    dev.endpoints[0]["identity"] = str(key)
    for yours in (mykey, None):
        monkeypatch.setattr(keys, "public_key", lambda *a: yours and (tmp_path / "id.pub", yours))
        v = doctor.advise(dev, Status.AUTH_FAILED.value, "Permission denied (publickey)")
        text = "\n".join(v.fix)
        assert pub in text and mykey not in text and "no ssh key yet" not in text
        assert f"ssh-keygen -y -f {key}" in text           # ssh-copy-id -i needs the .pub
    v = doctor.advise(_dev(kind=Kind.RENTAL, provider="vastai"), Status.AUTH_FAILED.value, "x")
    assert not any("ssh-copy-id" in f for f in v.fix)


def test_no_key_at_all_says_to_make_one_first(monkeypatch):
    monkeypatch.setattr(keys, "public_key", lambda *a: None)
    v = doctor.advise(_dev(), Status.AUTH_FAILED.value, "denied")
    assert "ssh-keygen" in v.fix[0]


@pytest.mark.parametrize("status, word", [
    (Status.TIMEOUT.value, "nothing answered"),
    (Status.REFUSED.value, "no ssh server"),
    (Status.HOST_KEY_MISMATCH.value, "host key changed"),
])
def test_every_other_failure_has_its_own_answer(status, word):
    assert word in doctor.advise(_dev(), status, "").why


@pytest.mark.parametrize("port,fix", [
    (22, "ssh-keygen -R 10.0.0.5"),
    (40001, "ssh-keygen -R '[10.0.0.5]:40001'"),
])
def test_a_changed_host_key_is_removed_for_that_port_alone(port, fix):
    """On any port but 22 known_hosts keeps the key as [host]:port; the bare host is the
    sshd on port 22 -- a provider's gateway, the box itself. The fix removed that key
    too, and the next connection there took whatever key it was shown."""
    dev = _dev(endpoints=[{"target": "10.0.0.5", "user": "me", "port": port}])
    (line,) = doctor.advise(dev, Status.HOST_KEY_MISMATCH.value, "").fix
    assert line.endswith(fix) and line.count("ssh-keygen") == 1


def test_doctor_changes_nothing_and_answers_as_json(monkeypatch, mykey):
    inv.save([_dev()])
    monkeypatch.setattr(doctor, "diagnose", lambda dev, **k: doctor.advise(
        dev, Status.AUTH_FAILED.value, "denied"))
    before = inv.INVENTORY_PATH.read_text()
    r = CliRunner().invoke(cli.app, ["doctor", "box", "--json"])
    assert r.exit_code == 0, r.output
    (m,) = json.loads(r.stdout)["machines"]
    assert m["name"] == "box" and m["fix"]
    assert inv.INVENTORY_PATH.read_text() == before



def test_doctor_looks_at_the_failing_machines_side_by_side(monkeypatch, mykey):
    """One after another, every machine switched off cost its whole timeout: twenty of
    them kept `fleet doctor` silent for minutes, and two hundred never finished."""
    import time

    from fleet.state import store

    names = [f"box{i}" for i in range(8)]
    inv.save([_dev(id=f"id:{n}", name=n) for n in names])
    monkeypatch.setattr(store, "latest", lambda conn, dev_id: ({"status": "timeout"}, None))

    def slow(dev, **k):
        time.sleep(0.5)
        return doctor.advise(dev, Status.TIMEOUT.value, "")

    monkeypatch.setattr(doctor, "diagnose", slow)
    started = time.monotonic()
    r = CliRunner().invoke(cli.app, ["doctor", "--json"])
    assert r.exit_code == 0, r.output
    assert time.monotonic() - started < 3          # eight in a row took four seconds
    assert [m["name"] for m in json.loads(r.stdout)["machines"]] == names
    r = CliRunner().invoke(cli.app, ["doctor"])
    assert [ln.split()[1] for ln in r.stdout.splitlines() if ln.startswith("✗")] == names


# ---------------------------------------------------------------- add
def test_a_machine_that_refuses_us_is_not_recorded_and_the_fix_is_said(monkeypatch, mykey):
    monkeypatch.setattr(cli, "onboard", lambda cmd, **k: (
        _dev(), ProbeResult(status=Status.AUTH_FAILED, error_detail="Permission denied")))
    r = CliRunner().invoke(cli.app, ["add", "ssh me@10.0.0.5", "--json"])
    assert r.exit_code == 1
    out = json.loads(r.stdout)
    assert out["ok"] is False and out["fix"] and "authorized_keys" in "\n".join(out["fix"])
    assert inv.live(inv.load()) == []


def test_ssh_copy_id_is_offered_only_at_a_terminal(monkeypatch, mykey):
    """An agent cannot type a password, so without a terminal it is never started."""
    started = []
    monkeypatch.setattr(cli.subprocess, "run", lambda argv, **k: started.append(argv))
    dev = _dev()
    res = ProbeResult(status=Status.AUTH_FAILED)
    monkeypatch.setattr(cli.sys.stdin, "isatty", lambda: False, raising=False)
    assert cli._offer_copy_id("ssh me@10.0.0.5", dev, res, name=None, kind=None,
                              alias="", taken=set()) == (dev, res)
    assert not started


def test_ssh_copy_id_argv_carries_port_identity_and_jump(tmp_path, monkeypatch):
    from fleet.ssh.cmd import Endpoint

    monkeypatch.setattr(keys.shutil, "which", lambda name: "/usr/bin/ssh-copy-id")
    argv = keys.copy_id_argv(Endpoint(target="1.2.3.4", user="root", port=40001,
                                      identity="/k", jump="bastion"))
    assert argv == ["/usr/bin/ssh-copy-id", "-p", "40001", "-i", "/k", "-o",
                    "ProxyJump=bastion", "root@1.2.3.4"]


# ---------------------------------------------------------------- first run
def test_the_first_run_records_this_machine_once_and_starts_no_fleet(monkeypatch):
    from fleet.ops import firstrun, identity

    monkeypatch.delenv("FLEET_NO_FIRST_RUN", raising=False)
    monkeypatch.setattr(firstrun, "teach_agents", lambda: ["claude"])
    monkeypatch.setattr(identity, "local_device_id", lambda: "id:me")
    import fleet.onboard as onboard

    monkeypatch.setattr(onboard, "onboard_self", lambda **k: (
        Device(id="id:me", name="laptop"), ProbeResult(status=Status.OK)))
    said = []
    assert firstrun.maybe("ls", said.append) is True
    assert "recorded laptop" in said[0] and "taught claude" in said[0]
    assert "center" not in said[0]
    assert [d.name for d in inv.load()] == ["laptop"]
    assert firstrun.maybe("ls", said.append) is False          # once


def test_the_first_run_says_fleet_0_5s_list_is_not_read(home, monkeypatch):
    """0.6 starts a new list, by design; saying nothing about the old one left people
    finding out from the troubleshooting page that their machines were "gone"."""
    from fleet import config
    from fleet.ops import firstrun, identity

    monkeypatch.delenv("FLEET_NO_FIRST_RUN", raising=False)
    monkeypatch.setattr(firstrun, "teach_agents", lambda: [])
    monkeypatch.setattr(identity, "local_device_id", lambda: "id:me")
    import fleet.onboard as onboard

    monkeypatch.setattr(onboard, "onboard_self", lambda **k: (
        Device(id="id:me", name="laptop"), ProbeResult(status=Status.OK)))
    config.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    (config.CONFIG_DIR / "inventory.yaml").write_text(
        "version: 1\ndevices:\n- {id: 'net:10.0.0.5:22', name: gpu}\n"
        "- {id: 'net:10.0.0.6:22', name: cpu}\n- {id: 'id:me', name: laptop, role: center}\n")
    said = []
    assert firstrun.maybe("ls", lambda text, *a: said.append(text))
    assert any("inventory.yaml" in s and "2 other machine" in s for s in said)


def test_the_first_run_says_nothing_of_a_0_5_list_that_is_not_there(home, monkeypatch):
    from fleet.ops import firstrun, identity

    monkeypatch.delenv("FLEET_NO_FIRST_RUN", raising=False)
    monkeypatch.setattr(firstrun, "teach_agents", lambda: [])
    monkeypatch.setattr(identity, "local_device_id", lambda: "id:me")
    import fleet.onboard as onboard

    monkeypatch.setattr(onboard, "onboard_self", lambda **k: (
        Device(id="id:me", name="laptop"), ProbeResult(status=Status.OK)))
    said = []
    firstrun.maybe("ls", lambda text, *a: said.append(text))
    assert not any("inventory.yaml" in s for s in said)


def test_a_tailnet_offers_its_machines_by_their_magicdns_names_and_no_exit_nodes():
    """A Mullvad exit node was offered as a machine to add; "Lin's MacBook Pro" was
    offered under a name `--hide` refused; two laptops named "laptop" shared one name."""
    status = {"Peer": {
        "a": {"HostName": "Lin's MacBook Pro", "DNSName": "lins-macbook-pro.example.ts.net.",
              "OS": "macOS", "TailscaleIPs": ["100.64.0.8"]},
        "b": {"HostName": "laptop", "DNSName": "laptop.example.ts.net.", "OS": "linux"},
        "c": {"HostName": "laptop", "DNSName": "laptop-1.example.ts.net.", "OS": "linux"},
        "d": {"HostName": "us-nyc-wg-301", "DNSName": "us-nyc-wg-301.example.ts.net.",
              "OS": "linux", "ExitNodeOption": True, "Location": {"Country": "USA"}},
    }}
    assert [c.name for c in discover.tailnet(status)] == [
        "laptop", "laptop-1", "lins-macbook-pro"]


# ---------------------------------------------------------------- hiding candidates
def test_a_hidden_candidate_is_not_offered_until_unhidden(home, monkeypatch):
    (home / ".ssh" / "config").write_text("Host lab\nHost old-box\n")
    monkeypatch.setattr(discover, "tailnet", lambda status=None: [])
    discover.set_hidden(["old-box"], hide=True)
    assert [c.name for c in discover.candidates(home=home)] == ["lab"]
    assert [c.name for c in discover.candidates(home=home, include_hidden=True)] == [
        "lab", "old-box"]
    discover.set_hidden(["OLD-BOX"], hide=False)
    assert [c.name for c in discover.candidates(home=home)] == ["lab", "old-box"]


def test_ls_discover_hide_says_what_is_hidden(monkeypatch):
    monkeypatch.setattr(discover, "candidates", lambda **k: [])
    r = CliRunner().invoke(cli.app, ["ls", "--discover", "--hide", "printer", "--json"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["hidden"] == ["printer"]


def test_discover_names_each_entry_for_a_machine_and_marks_what_is_hidden(monkeypatch):
    """Six Host entries for one added machine read "already added: lab, lab, lab, lab,
    lab, lab"; under --all a hidden one was listed with nothing to say so, and a hidden
    name no longer found listed nothing at all, though --all was said to list them."""
    C = discover.Candidate
    monkeypatch.setattr(discover, "candidates", lambda **k: [
        C("alpha", "alpha", "ssh alpha", "ssh-config", added="lab"),
        C("beta", "beta", "ssh beta", "ssh-config", added="lab"),
        C("delta", "delta", "ssh delta", "ssh-config")])
    monkeypatch.setattr(discover, "hidden", lambda: {"delta", "gone-box"})
    r = CliRunner().invoke(cli.app, ["ls", "--discover", "--all"], env={"COLUMNS": "200"})
    assert "already added: lab (as alpha, beta)" in r.output, r.output
    assert "delta (hidden)" in r.output and "not found now: gone-box" in r.output, r.output
    r = CliRunner().invoke(cli.app, ["ls", "--discover", "--all", "--json"])
    assert {c["name"]: c["hidden"] for c in json.loads(r.stdout)["candidates"]} == {
        "alpha": False, "beta": False, "delta": True}


# ---------------------------------------------------------------- edit: where it lands
@pytest.fixture
def this_box(tmp_path, monkeypatch):
    """This machine, recorded, with its notes in a home of the test's own."""
    from fleet.ops import identity

    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(identity, "local_device_id", lambda: "id:me")
    inv.save([Device(id="id:me", name="me")])
    return tmp_path / ".config" / "fleet" / "machine.yaml"


@pytest.mark.skipif(__import__("sys").platform == "win32", reason="POSIX sh")
def test_tags_cost_note_and_servers_land_in_the_machines_own_file(this_box):
    from fleet import notes

    r = CliRunner().invoke(cli.app, ["edit", "me", "--tag", "GPU-Box", "--cost", "1.5",
                                     "--note", "the lab box", "--endpoint", "mine=9000/v1",
                                     "--json"])
    assert r.exit_code == 0, r.output
    assert notes.parse(this_box.read_text()) == {
        "cost": {"usd_per_hour": 1.5}, "tags": ["gpu-box"], "notes": "the lab box",
        "endpoints": {"mine": {"port": 9000, "path": "/v1"}}}
    assert inv.load()[0].tags == [], "not into this machine's list"
    r = CliRunner().invoke(cli.app, ["edit", "me", "--note", "", "--cost", "0",
                                     "--endpoint", "mine=", "--json"])
    assert notes.parse(this_box.read_text()) == {"tags": ["gpu-box"]}


def test_local_keeps_tags_and_cost_in_this_machines_list(this_box):
    r = CliRunner().invoke(cli.app, ["edit", "me", "--tag", "spare", "--cost", "2",
                                     "--local", "--json"])
    assert r.exit_code == 0, r.output
    assert inv.load()[0].tags == ["spare"] and not this_box.exists()


def test_an_unreachable_machine_is_not_written_somewhere_else_silently(monkeypatch):
    inv.save([Device(id="id:far", name="far",
                     endpoints=[{"target": "10.0.0.9", "user": "me", "port": 22}])])
    from fleet.ops import machinefile

    def refused(*a, **k):
        raise machinefile.MachineFileError("Permission denied (publickey)")
    monkeypatch.setattr(machinefile, "change", refused)
    r = CliRunner().invoke(cli.app, ["edit", "far", "--tag", "x", "--json"])
    assert r.exit_code == 1
    out = json.loads(r.stdout)
    assert out["ok"] is False and "--local" in out["hint"]
    assert inv.load()[0].tags == []


@pytest.mark.parametrize("spec", ["mine=http://x", "mine=0", "Bad Name=9000", "mine"])
def test_a_wrong_endpoint_is_refused_before_anything_is_written(this_box, spec):
    r = CliRunner().invoke(cli.app, ["edit", "me", "--endpoint", spec])
    assert r.exit_code == 2 and not this_box.exists()


def test_fields_keeps_only_what_was_asked(monkeypatch):
    monkeypatch.setattr(cli, "_rows", lambda *a, **k: [
        {"name": "a", "status": "ok", "kind": "permanent", "gpus": [], "usd_per_hour": 1,
         "facts": [], "tags": [], "free_vram_mib": 0}])
    inv.save([Device(id="id:a", name="a")])
    r = CliRunner().invoke(cli.app, ["ls", "--json", "--fields", "usd_per_hour"])
    assert r.exit_code == 0, r.output
    assert json.loads(r.stdout)["devices"] == [{"name": "a", "usd_per_hour": 1}]


def test_a_port_that_takes_the_connection_but_never_speaks_ssh_is_not_called_off():
    """ssh's "Connection timed out during banner exchange" matched the plain timeout:
    "nothing answered at that address: it is off, asleep..." for a machine that took the
    connection -- another program on the port, or a wedged sshd."""
    from fleet.probe.runner import classify_stderr

    status, detail = classify_stderr("Connection timed out during banner exchange\n"
                                     "Connection to 1.2.3.4 port 40001 timed out\n")
    assert status is Status.TIMEOUT
    dev = Device(id="id:a", name="a100", kind=Kind.RENTAL,
                 endpoints=[{"target": "1.2.3.4", "user": "root", "port": 40001}])
    v = doctor.advise(dev, status.value, detail)
    assert "never speaks ssh" in v.why and "off, asleep" not in v.why
    assert any("port" in line for line in v.fix)


def test_a_failed_add_points_at_fleet_add_not_at_a_machine_it_did_not_record():
    """Under "nothing recorded", the fix said `fleet edit gpu --ssh ...`: no such machine."""
    dev = Device(id="id:a", name="gpu", endpoints=[{"target": "10.0.0.5", "user": "me"}])
    for status, detail in (("timeout", "network timeout"), ("unreachable", "no route"),
                           ("auth_failed", "too many authentication failures")):
        fix = " ".join(doctor.advise(dev, status, detail, adding=True).fix)
        assert "fleet edit" not in fix and 'fleet add "' in fix, status
    assert "fleet edit gpu" in " ".join(doctor.advise(dev, "timeout", "network timeout").fix)


def test_a_name_that_does_not_resolve_is_not_called_off():
    """ssh's "Could not resolve hostname" read as "it is off, asleep": a typo sent you
    to check a machine that was on."""
    from fleet.models import Device, Kind
    from fleet.ops import doctor
    dev = Device(id="x", name="lab", kind=Kind.PERMANENT,
                 endpoints=[{"target": "lab.exmaple.com", "user": "me", "port": 22}])
    v = doctor.advise(dev, "unreachable", "DNS lookup failed")
    assert "lab.exmaple.com" in v.why and "misspelt" in v.why and "off" not in v.why
    assert "off, asleep" in doctor.advise(dev, "unreachable", "no route to host").why


def test_doctor_on_an_address_not_in_the_list_says_add_not_edit(monkeypatch):
    from fleet.models import Device, Kind, ProbeResult, Status
    from fleet.ops import doctor
    from fleet.probe import runner
    monkeypatch.setattr(runner, "probe_endpoints", lambda eps, **kw: ProbeResult(
        status=Status.UNREACHABLE, error_detail="DNS lookup failed"))
    dev = Device(id="", name="newbox", kind=Kind.PERMANENT,
                 endpoints=[{"target": "newbox", "user": "me", "port": 22}])
    v = doctor.diagnose(dev, state=None, snap=None)
    assert any("fleet add" in f for f in v.fix)
    assert not any("fleet edit" in f for f in v.fix)
