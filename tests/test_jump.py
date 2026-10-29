"""Machines behind a jump host: what reaches the jump host, and whose failure it was.

ssh says a jump host's failures in the same words as the machine's, so a jump host that
did not resolve, refused, or turned the key away read as the machine being down or saying
no -- and doctor sent people to enable sshd on a machine nothing had reached. The samples
below are what OpenSSH 9.6 printed for each, through `-J`.
"""

from __future__ import annotations

import pytest

from fleet.models import Device, Kind, ProbeResult, Status
from fleet.ops import doctor
from fleet.probe import runner
from fleet.ssh import cmd as sshcmd
from fleet.ssh.cmd import Endpoint, Hop, build_argv, jump_hops
from fleet.state import inventory as inv

TARGET = Endpoint(target="gpu.example.ts.net", user="me", port=2251,
                  jump="hop@bastion.example.ts.net:2241")
PROXY_TAIL = "Connection closed by UNKNOWN port 65535"


def test_jump_hops_reads_every_form_ssh_takes():
    assert jump_hops("bastion") == [Hop("", "bastion", 22)]
    assert jump_hops("ssh://me@[fd00::1]:2200, a@b:3") == [Hop("me", "fd00::1", 2200),
                                                         Hop("a", "b", 3)]
    assert jump_hops("me@[127.0.0.1]:2241")[0].spec == "me@127.0.0.1:2241"
    assert Hop("me", "fd00::1", 2200).spec == "me@[fd00::1]:2200"


@pytest.mark.parametrize("stderr, status, says", [
    ("ssh: Could not resolve hostname bastion.example.ts.net: Name or service not known\n"
     + PROXY_TAIL, Status.UNREACHABLE, "has no address"),
    ("ssh: connect to host bastion.example.ts.net port 2241: Connection refused\n"
     + PROXY_TAIL, Status.REFUSED, "did not answer: Connection refused"),
    ("ssh: connect to host bastion.example.ts.net port 2241: Connection timed out\n"
     + PROXY_TAIL, Status.TIMEOUT, "did not answer: Connection timed out"),
    ("hop@bastion.example.ts.net: Permission denied (publickey).\n" + PROXY_TAIL,
     Status.AUTH_FAILED, "did not let us in"),
])
def test_a_failure_at_the_jump_host_is_said_of_the_jump_host(stderr, status, says):
    got, detail = runner.classify_stderr(stderr)
    detail = runner.through_jump(TARGET, stderr, detail)
    assert got is status
    assert detail.startswith("the jump host hop@bastion.example.ts.net:2241 ")
    assert says in detail


@pytest.mark.parametrize("stderr, status", [
    ("channel 0: open failed: connect failed: Connection refused\nstdio forwarding failed\n"
     + PROXY_TAIL, Status.REFUSED),
    ("channel 0: open failed: connect failed: Name or service not known\n"
     "stdio forwarding failed\n" + PROXY_TAIL, Status.UNREACHABLE),
    ("me@gpu.example.ts.net: Permission denied (publickey).", Status.AUTH_FAILED),
])
def test_a_failure_behind_the_jump_host_is_still_the_machines(stderr, status):
    # The jump host let us through: "closed", from its stdio forward's last line, hid
    # the machine refusing or being unknown to the jump host.
    got, detail = runner.classify_stderr(stderr)
    assert got is status
    assert runner.through_jump(TARGET, stderr, detail) == detail


def test_a_port_that_never_speaks_ssh_through_a_jump_host_names_both():
    stderr = ("Connection timed out during banner exchange\n"
              "Connection to UNKNOWN port 65535 timed out")
    _, detail = runner.classify_stderr(stderr)
    assert "the jump host hop@bastion.example.ts.net:2241, or the machine behind it" in (
        runner.through_jump(TARGET, stderr, detail))


def _dev():
    return Device(id="id:g", name="gpu", kind=Kind.PERMANENT, endpoints=[
        {"target": TARGET.target, "user": "me", "port": 2251, "jump": TARGET.jump}])


def test_doctor_sends_people_to_the_jump_host_not_to_sshd_on_the_machine():
    v = doctor.advise(_dev(), Status.REFUSED.value,
                      "the jump host hop@bastion.example.ts.net:2241 did not answer: "
                      "Connection refused")
    assert "never reached" in v.why
    assert not any("systemctl" in f for f in v.fix)
    assert v.fix[0].endswith("ssh -p 2241 hop@bastion.example.ts.net -v")
    assert any("-J user@jump-host" in f for f in v.fix)


def test_doctor_says_the_key_is_not_offered_to_the_jump_host():
    v = doctor.advise(_dev(), Status.AUTH_FAILED.value,
                      "the jump host hop@bastion.example.ts.net:2241 did not let us in: "
                      "credentials rejected")
    assert not any("authorized_keys" in f for f in v.fix)
    assert any("Host bastion.example.ts.net" in f for f in v.fix)


def test_a_name_the_jump_host_cannot_look_up_is_not_this_machines_dns():
    v = doctor.advise(_dev(), Status.UNREACHABLE.value, "DNS lookup failed")
    assert v.why.startswith("the jump host hop@bastion.example.ts.net:2241 knows no machine")


def test_the_jump_host_is_dialled_with_fleets_options(monkeypatch):
    # Not `-J`, which gives the jump host none of them: one never dialled before was
    # asked about its host key with no terminal, and one that was down had no timeout.
    monkeypatch.setattr(sshcmd, "IS_WINDOWS", False)
    argv = build_argv(TARGET, multiplex=False, connect_timeout=8)
    assert "-J" not in argv
    proxy = next(a for a in argv if a.startswith("ProxyCommand="))
    assert "BatchMode=yes" in proxy and "StrictHostKeyChecking=accept-new" in proxy
    assert "ConnectTimeout=6" in proxy      # gives up before the machine's own wait does
    assert proxy.endswith("-p 2241 -l hop -W '[%h]:%p' -- bastion.example.ts.net")
    two = build_argv(Endpoint(target="t", jump="a@one,two:2"), multiplex=False)
    assert next(a for a in two if a.startswith("ProxyCommand=")).endswith(
        "-J a@one -p 2 -W '[%h]:%p' -- two")


def test_windows_keeps_the_jump_host_as_ssh_gives_it(monkeypatch):
    monkeypatch.setattr(sshcmd, "IS_WINDOWS", True)
    argv = build_argv(TARGET, multiplex=False)
    assert argv[argv.index("-J") + 1] == TARGET.jump


def test_two_routes_to_one_address_do_not_share_a_connection(monkeypatch):
    # A master opened by the route through one jump host answered for the route through
    # another, so `fleet add` through a jump host that was down said "ok".
    monkeypatch.setattr(sshcmd, "control_dir", lambda: "/tmp/fleet-test")

    def path(ep):
        argv = build_argv(ep)
        return next(a for a in argv if a.startswith("ControlPath="))
    plain = Endpoint(target="h", user="me")
    one, two = (Endpoint(target="h", user="me", jump=j) for j in ("a", "b"))
    keyed = Endpoint(target="h", user="me", identity="/k")
    assert len({path(plain), path(one), path(two), path(keyed)}) == 4
    assert path(plain) == "ControlPath=/tmp/fleet-test/%C"


def test_a_silent_failure_through_a_jump_host_is_asked_again_unmultiplexed(monkeypatch):
    # A master connection gives its jump host's ssh no stderr: every failure through it
    # came back as a bare "closed", and `fleet add` said to enable sshd.
    calls = []

    def once(ep, **kw):
        calls.append(kw["multiplex"])
        if kw["multiplex"]:
            return ProbeResult(status=Status.CLOSED, error_class="closed",
                               error_detail="peer closed the connection without a diagnostic")
        return ProbeResult(status=Status.REFUSED, error_class="refused",
                           error_detail="the jump host x did not answer: Connection refused",
                           stderr_tail="ssh: connect to host x port 22: Connection refused")
    monkeypatch.setattr(runner, "_run_probe_once", once)
    res = runner.run_probe(Endpoint(target="h", jump="x"))
    assert calls == [True, False]
    assert res.status is Status.REFUSED
    calls.clear()
    runner.run_probe(Endpoint(target="h"))     # no jump host: nothing to ask again
    assert calls == [True]


def test_another_jump_host_or_key_for_a_known_machine_is_another_route():
    # "already recorded with this endpoint", and fleet went on by the old jump host.
    def dev(**ep):
        return Device(id="id:g", name="gpu", endpoints=[
            {"target": "h", "user": "me", "port": 22, **ep}])
    devices = [dev(jump="a", identity="/k")]
    _, action = inv.upsert(devices, dev(jump="b"))
    assert action == "endpoint_added"
    _, action = inv.upsert(devices, dev(jump="a", identity="/other"))
    assert action == "endpoint_added"
    # A key left unsaid is not another key: the one recorded may be another machine's path.
    _, action = inv.upsert(devices, dev(jump="a"))
    assert action == "unchanged"
    assert len(devices[0].endpoints) == 3
