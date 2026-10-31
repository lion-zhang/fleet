"""Why a machine will not let us in, and the one thing to do about it.

fleet manages no access, so when a machine refuses, the fix is yours to make -- and
usually one line. This says which line, for that machine: put your key on it (the words
differ on Windows, and for an administrator there), add it to your rental provider's
account, allow it in your Tailscale SSH policy, or start its sshd. It never applies any
of them: what lets someone into a machine is decided by the people who own it.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field, replace

from ..models import Device, Kind, ProbeResult, Status
from ..ssh import keys
from ..probe.runner import JUMP_DETAIL
from ..ssh.cmd import Endpoint, build_argv, jump_hops, run as sshrun
from ..state import inventory as inv
from .wake import wakeable

_PROVIDERS = {
    "vastai": "vast.ai: Account → SSH Keys, then recreate the instance or run it there",
    "runpod": "RunPod: Settings → SSH Public Keys; pods started after that accept it",
    "autodl": "AutoDL: 控制台 → 设置 → SSH 公钥 (or run the line below on the instance)",
}


@dataclass
class Verdict:
    name: str
    status: str
    why: str
    fix: list[str] = field(default_factory=list)
    server: str = ""          # what the ssh server says it is: OpenSSH_9.6, Tailscale...
    route: str = ""           # the ssh command tried

    def to_dict(self) -> dict:
        return asdict(self)


def server_banner(ep: Endpoint, timeout: int = 10) -> tuple[str, str]:
    """(what the server says it is, ssh's verbose stderr), from one connection attempt."""
    argv = build_argv(ep, multiplex=False, connect_timeout=8, remote="exit 0")
    argv.insert(1, "-v")
    try:
        p = sshrun(argv, timeout=timeout)
        text = (p.stderr or b"").decode(errors="replace")
    except (subprocess.TimeoutExpired, OSError) as exc:
        return "", str(exc)
    m = re.search(r"remote software version (\S+)", text)
    return (m.group(1) if m else ""), text


def _provider(dev: Device, snap: dict | None) -> str:
    if dev.provider:
        return dev.provider
    if (snap or {}).get("vast_label"):
        return "vastai"
    if (snap or {}).get("runpod_id"):
        return "runpod"
    if (snap or {}).get("autodl"):
        return "autodl"
    return ""


def _key_lines(ep: Endpoint | None, *, windows: bool, copy_id: bool = True) -> list[str]:
    # The key this machine is reached with, when the endpoint names one; else yours.
    # Never yours instead of that one: it is reached with IdentitiesOnly, so the line for
    # ~/.ssh/id_ed25519.pub let nothing in, and beside a .pem with no .pub it said "you
    # have no ssh key yet".
    make_pub: list[str] = []
    if ep is not None and ep.identity:
        found, make_pub = keys.pinned_public_key(ep.identity)
        if found is None:
            return make_pub + ["then run fleet doctor again for the line that puts it on "
                               "the machine"]
    elif (found := keys.public_key()) is None:
        return ["you have no ssh key yet -- make one first:  ssh-keygen -t ed25519",
                "then run fleet doctor again for the line that puts it on the machine"]
    _, pub = found
    if windows:
        return ["on that machine, in PowerShell (as the user fleet logs in as):",
                keys.windows_install_line(pub, admin=False),
                "or, if that user is an administrator, as administrator:",
                keys.windows_install_line(pub, admin=True)]
    lines = []
    if copy_id and ep is not None and (argv := keys.copy_id_argv(ep)):
        # ssh-copy-id -i reads KEY.pub, and stopped at "failed to open ID file" without it
        lines += [*make_pub, "from here, typing its password once:", " ".join(argv),
                  "or on that machine:"]
    else:
        lines += ["on that machine:"]
    return lines + [keys.posix_install_line(pub)]


def _route(dev: Device, used: str = ""):
    """The route the attempt went by: the one that answered, or the failure being
    explained. The first by preference only when nothing says which -- "✓ ok · ssh -p
    2299 ..." named a route that was down while the second one let us in."""
    eps = sorted(inv.endpoints_of(dev), key=lambda e: e.preference)
    return next((e for e in eps if used and e.name == used), eps[0] if eps else None)


def advise(dev: Device, status: str, detail: str, *, snap: dict | None = None,
           server: str = "", verbose: str = "", used: str = "",
           adding: bool = False) -> Verdict:
    """The verdict for one failed attempt, from what it said.

    `adding`: the machine is not recorded -- `fleet add` failed -- so a new address goes
    to `fleet add` again. "fleet edit NAME --ssh" there named a machine that did not
    exist, under "nothing recorded"."""
    ep = _route(dev, used)

    def readdress(cmd: str) -> str:
        return (f'fleet add "{cmd}"' if adding
                else f'fleet edit {dev.name} --ssh "{cmd}"')
    v = Verdict(name=dev.name, status=status, why="", server=server,
                route=ep.ssh_command() if ep else "")
    text = f"{detail}\n{verbose}".lower()
    windows = ("windows" in server.lower() or "windows" in str((snap or {}).get("os", "")).lower())
    tailscale = "tailscale" in server.lower()

    if status == Status.OK.value:
        v.why = "it answers, and lets us in"
        return v
    if ep is None:
        v.why = "there is no address recorded for it"
        v.fix = [readdress("ssh user@host")]
        return v
    if status == Status.HOST_KEY_MISMATCH.value:
        v.why = ("its host key changed. A rental's port given to someone else does this, "
                 "and so does a reinstall -- but so would someone in the middle")
        # known_hosts holds the HostName, not an ssh config alias that points at it --
        # and on any port but 22 as [host]:port alone. The bare host is the sshd on port
        # 22 (a rental provider's gateway, the box itself): removing its key as well left
        # that one to be taken on trust, whatever key it showed, next time.
        v.fix = [f"if you know why it changed:  ssh-keygen -R {ep.address}" if ep.port in (22, None)
                 else f"if you know why it changed:  ssh-keygen -R '[{ep.address}]:{ep.port}'"]
        return v
    if ep.jump and detail.startswith(JUMP_DETAIL):
        # Said of the machine, this sent people to enable sshd on it, to put the key on
        # it, or to look for it on another address -- when nothing ever reached it.
        hop = next((h for h in jump_hops(ep.jump) if f" {h.shown} " in detail),
                   None) or jump_hops(ep.jump)[0]
        v.why = f"{detail[:200]}, so {ep.target} behind it was never reached"
        v.fix = [f"check the jump host on its own:  {hop.ssh_command()} -v"]
        if "did not let us in" in detail:
            v.fix.append(f"-i is offered to {ep.target} only: for the jump host, put "
                         f"IdentityFile under Host {hop.host} in ~/.ssh/config, or "
                         "ssh-add the key")
        v.fix.append("if the jump host moved:  "
                     + readdress(replace(ep, jump="user@jump-host").ssh_command()))
        return v
    if status == Status.TIMEOUT.value and "never speaks ssh" in text:
        v.why = ("something takes the connection on that port, but never speaks ssh: "
                 "another program holds the port, or the ssh server there is stuck")
        v.fix = [f"check the port:  {ep.ssh_command()} -v",
                 f"if it moved:  {readdress('ssh -p PORT user@host')}"]
        if ep.jump:
            # Which one never spoke ssh, ssh does not say: the jump host is checked alone.
            v.fix.insert(1, "check the jump host on its own:  "
                            f"{jump_hops(ep.jump)[0].ssh_command()} -v")
        if dev.kind is Kind.RENTAL:
            v.fix.insert(0, "a rental's port given to another instance does this; check "
                            "the provider's dashboard for its current port")
        return v
    if status == Status.UNREACHABLE.value and "dns lookup failed" in text:
        # "it is off, asleep" sent people to check a machine whose name was mistyped.
        v.why = (f"no machine goes by the name {ep.address}: it is misspelt, or only a "
                 "VPN's or tailnet's own DNS knows it")
        if ep.jump:
            # Looked up by the jump host, not here: this machine's DNS is not the question.
            v.why = (f"the jump host {ep.jump} knows no machine by the name {ep.address}: "
                     "it is misspelt, or a name only DNS on this side knows -- give the "
                     "address the jump host reaches it at")
        v.fix = ["check the spelling; on a VPN or tailnet, that this machine is connected",
                 f"if it moved:  {readdress('ssh user@new-address')}"]
        return v
    if status in (Status.TIMEOUT.value, Status.UNREACHABLE.value):
        v.why = ("nothing answered at that address: it is off, asleep, or not on a network "
                 "this machine can reach")
        if detail.startswith("could not connect: "):
            # Said by this machine, not the other: a firewall or sandbox here says
            # "Permission denied", a missing IPv6 route only "failure".
            v.why = (f"this machine could not open a connection to it "
                     f"({detail.removeprefix('could not connect: ')}): it is off, or a "
                     "firewall or network here stands in the way")
        v.fix = ["check it is on; on a VPN or tailnet, that both ends are connected",
                 f"if it moved:  {readdress('ssh user@new-address')}"]
        if not adding and wakeable(dev, snap):
            v.fix.insert(0, f"if it is asleep:  fleet wake {dev.name}")
        return v
    if status in (Status.REFUSED.value, Status.CLOSED.value):
        v.why = "the machine is there, but no ssh server is listening on that port"
        v.fix = ["Linux:   sudo systemctl enable --now ssh   (sshd on some systems)",
                 "macOS:   System Settings → General → Sharing → Remote Login",
                 "Windows: Start-Service sshd; Set-Service sshd -StartupType Automatic"]
        if dev.kind is Kind.RENTAL:
            v.fix.insert(0, "a rental that stopped answering on its port has usually "
                            "been destroyed or moved; check the provider's dashboard")
        return v
    if "another machine answers" in text:
        v.why = (f"{detail[:160]} -- it was given another machine's address, or a "
                 "machine cloned from the same image took its address")
        v.fix = [f"if it moved:  {readdress('ssh user@new-address')}",
                 f"if that one is new:  fleet add \"{ep.ssh_command()}\""]
        return v
    if status != Status.AUTH_FAILED.value:
        v.why = f"it answered, but the probe failed: {detail[:160]}"
        # Not added yet, it has no name to give `fleet ssh`: "fleet ssh ssh -p 22 me@h"
        # named the command typed at doctor.
        v.fix = [(f"{ep.ssh_command()} 'echo ok'" if adding
                  else f"fleet ssh {dev.name} -- 'echo ok'") + "   to see what the shell says"]
        return v

    # It is up, and said no.
    if tailscale:
        if "check" in text or "login.tailscale.com" in text or "/a/" in text:
            v.why = ("Tailscale SSH wants you to sign in again in a browser (a `check` "
                     "rule) -- an agent never can")
            v.fix = ["in your tailnet policy, give this rule \"action\": \"accept\", or",
                     f"sign in once:  {ep.ssh_command()}"]
        else:
            v.why = ("Tailscale SSH does not allow you, as this user, on this machine. "
                     "Access is decided by your tailnet's policy, not on the machine")
            v.fix = ["in the Tailscale admin console → Access controls, add an ssh rule, e.g.",
                     '{"action": "accept", "src": ["autogroup:member"], '
                     f'"dst": ["autogroup:self"], "users": ["{ep.user or "autogroup:nonroot"}"]}}',
                     "or connect as a user the policy allows:  "
                     + readdress(f"ssh USER@{ep.target}")]
        return v
    provider = _provider(dev, snap)
    if provider in _PROVIDERS:
        v.why = "the rental does not have your public key"
        v.fix = [f"add it to your provider account -- {_PROVIDERS[provider]}"]
        v.fix += _key_lines(ep, windows=False, copy_id=False)[-2:]
        return v
    if "too many authentication failures" in text:
        v.why = "ssh offered too many keys before the right one, and the server gave up"
        v.fix = ["name the key to use:  "
                 + readdress(f"{ep.ssh_command()} -i ~/.ssh/THE_KEY")]
        return v
    v.why = "it is up, but does not accept your key" + (
        " -- it would take a password, which fleet never uses" if "password" in text else "")
    v.fix = _key_lines(ep, windows=windows)
    return v


def diagnose_result(dev: Device, res: ProbeResult, *, adding: bool = False) -> Verdict:
    """The verdict for a probe that just failed, without dialling again."""
    snap = res.snapshot.to_dict() if res.snapshot is not None else None
    return advise(dev, res.status.value, res.error_detail or res.stderr_tail or "", snap=snap,
                  used=res.endpoint_used, adding=adding)


def diagnose(dev: Device, *, state: dict | None, snap: dict | None) -> Verdict:
    """Look at the machine now: one probe, and if it is refused, one verbose attempt to
    hear what the server says it is."""
    from ..probe.runner import probe_endpoints

    # An address that is not in the list yet (no id) is fixed with `fleet add`: "if it
    # moved:  fleet edit x --ssh ..." named a machine fleet has never heard of.
    adding = not dev.id
    eps = inv.endpoints_of(dev)
    if not eps:
        from .identity import local_device_id

        if dev.id and dev.id == local_device_id():
            # `fleet doctor vm` on vm itself said "unreachable: no address recorded",
            # and to give it an ssh address: fleet reads this machine with no ssh at all.
            return Verdict(name=dev.name, status=Status.OK.value,
                           why="this machine: fleet reads it here, with no ssh")
        return advise(dev, "unreachable", "", snap=snap, adding=adding)
    # A fresh connection, not a multiplexed one: an open master from a minute ago would
    # answer for a machine that has since stopped letting us in.
    res = probe_endpoints(eps, mode=dev.probe_mode, timeout=20, multiplex=False)
    # "it answers, and lets us in" was said of a clone that took its address.
    from .rows import same_machine
    res = same_machine(dev.id, dev.name, res)
    if res.ok:
        return advise(dev, Status.OK.value, "", snap=snap, used=res.endpoint_used,
                      adding=adding)
    server, verbose = ("", "")
    if res.status is Status.AUTH_FAILED and (best := _route(dev, res.endpoint_used)):
        server, verbose = server_banner(best)
    return advise(dev, res.status.value, res.error_detail or "", snap=snap or (
        res.snapshot.to_dict() if res.snapshot else None), server=server, verbose=verbose,
        used=res.endpoint_used, adding=adding)
