"""End to end on one real machine: every fleet command, run the way a person runs it.

Run by .github/workflows/e2e.yml on Linux, macOS and Windows runners, after the
installer has made the runner a center. The runner is the center and, over ssh to
localhost, a target too: the probe, the enrolment and every authorized_keys edit run
through sshd and that OS's own shell, which is the code that differs per OS.

A second fleet state on the same runner (FLEET_CONFIG_DIR / FLEET_STATE_DIR) joins with
an invite, so invites, join, sync and grants have a member to work on.

    FLEET_BIN=$(command -v fleet) uv run --no-project --with mcp --with . \
        python tests/e2e/host.py --ssh-user NAME [--report FILE]

Exits 1 if any required check failed. Prints a table either way.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

WINDOWS = os.name == "nt"
# The Windows console is cp1252 by default; the report has ✅ in it.
sys.stdout.reconfigure(encoding="utf-8", errors="replace")
RESULTS: list[tuple[str, str, str]] = []          # (status, name, detail)
# The installed fleet, not one this script's own environment may carry.
FLEET = os.environ.get("FLEET_BIN") or shutil.which("fleet") or "fleet"


def run(args: list[str], *, env: dict | None = None, timeout: float = 180,
        input: str | None = None) -> tuple[int, str]:
    e = {**os.environ, "NO_COLOR": "1", "TERM": "dumb", "COLUMNS": "160", **(env or {})}
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout, env=e,
                           input=input, encoding="utf-8", errors="replace",
                           stdin=None if input is not None else subprocess.DEVNULL)
        return p.returncode, (p.stdout or "") + (p.stderr or "")
    except subprocess.TimeoutExpired as exc:
        out = exc.stdout or ""
        out = out.decode(errors="replace") if isinstance(out, bytes) else out
        return 124, out + "\n[timed out]"


def fleet(*args: str, **kw) -> tuple[int, str]:
    return run([FLEET, *args], **kw)


_LAST = [time.monotonic()]


def check(name: str, ok: bool, detail: str = "", *, required: bool = True) -> bool:
    status = "PASS" if ok else ("FAIL" if required else "WARN")
    took, _LAST[0] = time.monotonic() - _LAST[0], time.monotonic()
    RESULTS.append((status, name, detail.strip().replace("\n", " ⏎ ")[-300:]))
    print(f"{status}  {name}  ({took:.1f}s)" + ("" if ok else f"\n      {detail.strip()[-5000:]}"),
          flush=True)
    if took > 15:
        print(f"      slow: {detail.strip()[-400:]}", flush=True)
    return ok


def expect(name: str, args: list[str], pattern: str = ".", *, rc: int | None = 0,
           required: bool = True, **kw) -> tuple[bool, str]:
    code, out = fleet(*args, **kw)
    ok = (rc is None or code == rc) and re.search(pattern, out, re.M) is not None
    check(name, ok, f"exit {code}: {out}", required=required)
    return ok, out


def as_json(out: str):
    start = min([i for i in (out.find("{"), out.find("[")) if i >= 0], default=-1)
    # the first document: stderr notes are appended after it
    return json.JSONDecoder().raw_decode(out[start:])[0] if start >= 0 else None


def machines(out: str) -> list[dict]:
    d = as_json(out)
    return d if isinstance(d, list) else (d or {}).get("devices", [])


# -- the ssh target: this runner, as another login ------------------------------------

def place_key(user: str, pubkey: str) -> str:
    """Put the center's key where sshd on this OS reads it for `user`."""
    if WINDOWS:
        admin = run(["powershell", "-NoProfile", "-Command",
                     f"(New-Object Security.Principal.WindowsPrincipal("
                     f"[Security.Principal.WindowsIdentity]::GetCurrent())).IsInRole("
                     f"[Security.Principal.WindowsBuiltInRole]::Administrator)"])[1].strip()
        if user.lower() == os.environ.get("USERNAME", "").lower() and admin == "True":
            path = Path(os.environ["ProgramData"]) / "ssh" / "administrators_authorized_keys"
            acl = "icacls $f /inheritance:r /grant 'SYSTEM:F' 'Administrators:F'"
        else:
            path = Path(f"C:/Users/{user}/.ssh/authorized_keys")
            acl = f"icacls $f /inheritance:r /grant 'SYSTEM:F' 'Administrators:F' '{user}:F'"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="ascii") as fh:
            fh.write(pubkey.strip() + "\n")
        run(["powershell", "-NoProfile", "-Command", f"$f='{path}'; {acl}"])
        return str(path)
    home = run(["sh", "-c", f"eval echo ~{user}"])[1].strip()
    path = f"{home}/.ssh/authorized_keys"
    sudo = [] if user == os.environ.get("USER") else ["sudo", "-u", user]
    run([*sudo, "sh", "-c", f"umask 077; mkdir -p ~/.ssh; echo '{pubkey.strip()}' >> ~/.ssh/authorized_keys"])
    return path


def authorized_keys_text(path: str) -> str:
    if WINDOWS or os.access(path, os.R_OK):
        try:
            return Path(path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            pass
    return run(["sudo", "cat", path])[1]


def authorized_keys_edit(user: str, path: str, center_key: str) -> None:
    from fleet import reconcile as rec
    from fleet.ssh.authkeys import sync_command
    from fleet.ssh.cmd import Endpoint

    plat = "windows" if WINDOWS else "posix"
    ep = Endpoint(target="localhost", user=user)
    fid, src = "e2e000", "SHA256:e2e-grant"
    grant = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIE2eE2eE2eE2eE2eE2eE2eE2eE2eE2eE2eE2eE2eE2eE e2e"
    before = authorized_keys_text(path)
    ok, out = rec._remote(ep, sync_command(fid, src, user=user, pubkey=grant, platform=plat),
                          platform=plat)
    after = authorized_keys_text(path)
    check("authorized_keys grant over ssh", ok, out)
    check("  the grant's block is in the file", f"# fleet:{fid}:begin from={src}" in after
          and grant.split()[1] in after, after)
    check("  every key already there survived", center_key.split()[1] in after
          and all(line in after for line in before.splitlines() if line.strip()), after)
    ok, out = rec._remote(ep, sync_command(fid, src, user=user, pubkey=grant, platform=plat),
                          platform=plat)
    again = authorized_keys_text(path)
    check("  granting again adds no duplicate", ok and again.count(grant.split()[1]) == 1, again)
    ok, out = rec._remote(ep, sync_command(fid, src, user=user, pubkey=None, platform=plat),
                          platform=plat)
    final = authorized_keys_text(path)
    check("authorized_keys revoke over ssh", ok and grant.split()[1] not in final, out + final)
    check("  the file is as it was before the grant",
          [l for l in final.splitlines() if l.strip()]
          == [l for l in before.splitlines() if l.strip()], final)


# -- the checks ----------------------------------------------------------------------

def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ssh-user", required=True, help="login on this runner to reach over ssh")
    ap.add_argument("--report", default="")
    a = ap.parse_args()
    target = f"ssh -o StrictHostKeyChecking=accept-new {a.ssh_user}@localhost"
    print(f"== fleet e2e on {platform.platform()}  (fleet: {FLEET})", flush=True)

    # The core, as the installer left it.
    expect("fleet --version", ["--version"], r"^fleet \d")
    expect("fleet --help lists every command", ["--help"], r"access")
    expect("fleet paths", ["paths"], r"inventory")
    ok, out = expect("installer made this a center", ["center", "--json"], r'"role": "center"')
    expect("center (status)", ["center"], r"center")
    ok, pub = expect("center --pubkey", ["center", "--pubkey"], r"^ssh-ed25519 ")
    expect("center --export", ["center", "--export"], r".")
    _, svc = expect("service status", ["service", "status"], r".", required=False)
    # A container has no service manager at all; everywhere else the service must serve.
    no_manager = "unavailable" in svc

    # Is the center listening (the service the installer started)? If not, listen now.
    listener = None
    def answering() -> bool:
        try:
            urllib.request.urlopen("http://127.0.0.1:7373/", timeout=3)
            return True
        except Exception as exc:                  # an HTTP error still means it answered
            return hasattr(exc, "code")
    up = any(answering() or time.sleep(2) for _ in range(10))
    why = "nothing on 127.0.0.1:7373"
    if not up:
        from fleet import service
        log = service.LOG_PATH()
        why += f"; {log}: " + (log.read_text(errors="replace")[-1500:] if log.exists()
                              else "no log")
        if platform.system() == "Darwin":
            said = run(["launchctl", "print", f"gui/{os.getuid()}/io.fleet.center"])[1]
            pid = re.search(r"\bpid = (\d+)", said)
            if pid:
                # Where it is stuck: faulthandler (PYTHONFAULTHANDLER in the plist)
                # prints every thread's Python stack into the log on SIGABRT.
                why += "\nlsof: " + run(["lsof", "-nP", "-a", "-p", pid[1], "-i"])[1][-800:]
                run(["kill", "-ABRT", pid[1]])
                time.sleep(2)
                why += "\nstack: " + (log.read_text(errors="replace")[-3000:] if log.exists()
                                       else "no log")
    check("the service is listening on 7373", up, why, required=not no_manager)
    if not up:
        listener = subprocess.Popen([FLEET, "center", "--listen"], stdout=subprocess.DEVNULL,
                                    stderr=subprocess.DEVNULL, stdin=subprocess.DEVNULL)
        up = any(answering() or time.sleep(1) for _ in range(20))
        check("center --listen serves", up, "still nothing on 7373")

    expect("ls shows this machine", ["ls"], platform.node().split(".")[0][:8] or ".")
    ok, out = expect("ls --json", ["ls", "--json"], r'"name"')
    me = machines(out)[0]["name"] if ok and machines(out) else ""
    expect("show (this machine)", ["show"], r".")
    expect("show --json", ["show", "--json"], r'"name"')

    # This machine over ssh: the remote probe and shell of this OS.
    path = place_key(a.ssh_user, pub.strip().splitlines()[-1])
    expect("add --dry-run", ["add", "--dry-run", target], r".")
    ok, out = expect("add over ssh (this OS as a target)",
                     ["add", target, "--name", "loop", "--tag", "e2e", "--json"], r'"action"')
    rec = as_json(out) or {}
    name = rec.get("name") or "loop"
    check("ssh target recognised as this machine (one device, two endpoints)",
          rec.get("action") in ("endpoint_added", "unchanged", "added"), str(rec), required=False)
    expect("ssh NAME -- command", ["ssh", name, "--", "echo", "fleet-e2e-ok"], r"fleet-e2e-ok")
    code, out = fleet("ssh", name, "--", "exit", "3")
    check("ssh passes the exit code", code == 3, f"exit {code}: {out}")
    expect("ls --tag e2e", ["ls", "--tag", "e2e", "--json"], re.escape(f'"{name}"'))
    expect("ls --online", ["ls", "--online"], r".")
    expect("ls -r", ["ls", "-r"], r".")

    # edit, each flag
    expect("edit --cost", ["edit", name, "--cost", "1.5"], r".")
    expect("edit --tag", ["edit", name, "--tag", "big"], r".")
    expect("edit --untag", ["edit", name, "--untag", "big"], r".")
    expect("edit --alias", ["edit", name, "--alias", "lp"], r".")
    expect("show by alias", ["show", "lp", "--no-refresh"], r".")
    expect("edit --disk-path", ["edit", name, "--disk-path", "C:\\" if WINDOWS else "/"], r".")
    expect("edit --clear-disk-paths", ["edit", name, "--clear-disk-paths"], r".")
    expect("edit --name (rename)", ["edit", name, "--name", "loop2"], r".")
    expect("rename shows in ls", ["ls"], r"loop2")
    expect("edit --name (back)", ["edit", "loop2", "--name", name], r".")
    expect("edit --json", ["edit", name, "--cost", "0", "--json"], r"\{")

    # top needs a terminal; without one it must say so rather than hang.
    code, out = fleet("top", "-i", "1", timeout=8)
    check("top without a terminal exits or explains", code != 124 or bool(out.strip()),
          f"exit {code}: {out}", required=False)

    # invites, and a second fleet state on this runner joining with one
    ok, out = expect("invite --json", ["invite", "member", "--json"], r'"code"')
    inv = as_json(out) or {}
    expect("invite --list", ["invite", "--list"], r"member")
    ok, out = expect("invite --ttl", ["invite", "spare", "--ttl", "1h", "--json"], r'"id"')
    expect("invite --revoke", ["invite", "--revoke", (as_json(out) or {}).get("id", "x")],
           r"(?i)revok|withdr")

    # A second fleet state on this runner is the same machine (same machine-id, same
    # hostname, another key): the center must refuse it, not merge or duplicate it.
    other = Path(tempfile.mkdtemp(prefix="fleet-member-"))
    menv = {"FLEET_CONFIG_DIR": str(other / "config"), "FLEET_STATE_DIR": str(other / "state"),
            "FLEET_NO_AUTO_CENTER": "1"}
    code, out = fleet("join", inv.get("code", "fleet1:none"), "--name", "member",
                      "--ssh", target, env=menv)
    check("join from a second state on the same machine is refused, saying why",
          code != 0 and "already has a different key pinned" in out, f"exit {code}: {out}")
    expect("sync on the center", ["sync"], r".", rc=None)
    expect("access (whole fleet)", ["access"], r".")
    expect("access --json", ["access", "--json"], r"\{")

    # The one edit that can lock someone out, through this OS's sshd and shell: fleet's
    # own remote authorized_keys edit (what every grant and revoke runs), as the center
    # runs it. A single runner has no second machine to grant, so it is called directly.
    authorized_keys_edit(a.ssh_user, path, pub.strip().splitlines()[-1])
    expect("ssh still works after the edits", ["ssh", name, "--", "echo", "still-in"],
           r"still-in")

    interactive_checks(name, a.ssh_user)

    # agents
    expect("setup --dry-run --target all", ["setup", "--dry-run", "--target", "all"], r".")
    home = Path.home()
    (home / ".claude").mkdir(exist_ok=True)
    expect("setup --target claude", ["setup", "--target", "claude"], r".")
    check("claude skill written", (home / ".claude/skills/fleet/SKILL.md").is_file())
    (home / ".codex").mkdir(exist_ok=True)
    expect("setup --target codex", ["setup", "--target", "codex"], r".")
    check("shared skill written", (home / ".agents/skills/fleet/SKILL.md").is_file())
    expect("setup --refresh", ["setup", "--refresh"], r".", rc=None)
    expect("setup --uninstall --target claude", ["setup", "--uninstall", "--target", "claude"], r".")
    check("claude skill removed", not (home / ".claude/skills/fleet/SKILL.md").exists())
    mcp_checks(name)

    # take the fleet down
    expect("center --dissolve --force", ["center", "--dissolve", "--force"], r".", rc=None)
    if listener:
        listener.terminate()

    report(a.report)
    return 1 if any(s == "FAIL" for s, _, _ in RESULTS) else 0


def interactive_checks(machine: str, user: str) -> None:
    """`fleet top` and `fleet ssh` at a real terminal, typed into as a person types.

    Everything above runs fleet the way an agent does: no terminal. That never reached
    the code a person uses, and on Windows both were broken there -- top died on "No
    module named 'termios'", and ssh shared the keyboard with the shell it returned to.
    """
    sys.path.insert(0, str(Path(__file__).parent))
    from term import Term, reporting_exit

    env = {**os.environ, "TERM": "xterm-256color"}
    env.pop("NO_COLOR", None)
    env.pop("COLUMNS", None)
    # The remote shell is this OS's: cmd.exe on Windows. 4^2 and $((40+2)) both print
    # 42, so the typed command echoing back can never pass for its output.
    sixty = "echo fleet-ok-4^2" if WINDOWS else 'echo fleet-ok-$((40+2))'
    long_wait = "ping -n 30 127.0.0.1" if WINDOWS else "sleep 30"

    if WINDOWS:
        # The harness first: an exit code it misreads would fail fleet for nothing.
        h = Term(reporting_exit(["cmd", "/c", "exit 5"]), env=env)
        try:
            got = h.wait(20)
        finally:
            h.close()
        check("terminal harness reads exit codes", got == 5, f"cmd /c exit 5 -> {got}")

    t = Term(reporting_exit([FLEET, "top", "-i", "1"]), env=env)
    try:
        drew = t.expect(re.escape(machine) + "|online", 30)
        check("top: draws in a terminal", drew, t.tail())
        t.send("q")
        code = t.wait(15)
        check("top: q quits it", code == 0, f"exit {code}: {t.tail()}")
    finally:
        t.close()

    t = Term(reporting_exit([FLEET, "ssh", machine]), env=env)
    try:
        time.sleep(4)                                  # login and the first prompt
        check("ssh: an interactive session stays open", t.alive(), t.tail())
        at = t.mark()
        t.send(sixty + "\r", per_key=0.03)
        check("ssh: what is typed runs there", t.expect(OUTPUT_42, 20, since=at), t.tail())
        at = t.mark()
        typed = "echo the-quick-brown-fox-jumps-0123456789.end"
        t.send(typed + "\r", per_key=0.02)
        # The far side's echo of it, alone on its line: a key lost, doubled or reordered
        # on the way would make it anything else.
        # ".end" closes it: on Windows the next prompt follows with no line break
        whole = t.expect(r"(?<!echo )the-quick-brown-fox-jumps-0123456789\.end(?!d)", 20,
                         since=at)
        check("ssh: every key arrives, in order, once", whole,
              clean_text(t.text[at:])[-800:])
        t.send(long_wait + "\r")
        time.sleep(3)
        t.send("\x03")                                  # Ctrl+C, for the remote command
        time.sleep(2)
        at = t.mark()
        t.send(sixty + "\r", per_key=0.03)
        check("ssh: Ctrl+C stops the remote command, not the session",
              t.alive() and t.expect(OUTPUT_42, 20, since=at), t.tail())
        t.send("exit 7\r")
        code = t.wait(20)
    finally:
        t.close()

    # What plain ssh returns for the same session is the bar: fleet must pass on exactly
    # that. On POSIX it is the shell's 7.
    from fleet.config import FLEET_KEY
    plain = Term(reporting_exit(["ssh", "-tt", "-i", str(FLEET_KEY), "-o",
                                 "StrictHostKeyChecking=accept-new", f"{user}@localhost"]),
                 env=env)
    try:
        time.sleep(4)
        plain.send("exit 7\r")
        bar = plain.wait(20)
    finally:
        plain.close()
    check("ssh: the session's exit code is plain ssh's", code == bar and code is not None,
          f"fleet ssh exit {code}, plain ssh exit {bar}")
    if not WINDOWS:
        check("ssh: and it is the shell's own", code == 7, f"exit {code}")


# The far side's output of `echo fleet-ok-4^2` / `$((40+2))`: 42, which the typed line
# (4^2, $((40+2))) never contains. Not anchored to a line: a ConPTY draws lines with
# cursor moves, so after cleaning an output can run straight into the next prompt.
OUTPUT_42 = r"fleet-ok-42(?!\d)"


def clean_text(text: str) -> str:
    sys.path.insert(0, str(Path(__file__).parent))
    from term import clean

    return clean(text)


def mcp_checks(machine: str) -> None:
    try:
        from mcp import ClientSession, StdioServerParameters
        from mcp.client.stdio import stdio_client
    except ImportError as exc:
        check("mcp client available", False, str(exc))
        return

    async def go():
        params = StdioServerParameters(command=FLEET, args=["mcp"], env=dict(os.environ))
        async with stdio_client(params) as (r, w):
            async with ClientSession(r, w) as s:
                await s.initialize()
                tools = sorted(t.name for t in (await s.list_tools()).tools)
                check("mcp: tools listed", "list_machines" in tools, str(tools))
                for tool, args in [("center_status", {}), ("list_machines", {}),
                                   ("show_access", {}),
                                   ("run_on_machine", {"machine": machine, "command": "echo via-mcp"})]:
                    res = await s.call_tool(tool, args)
                    text = res.content[0].text if res.content else ""
                    good = not getattr(res, "isError", False) and (tool != "run_on_machine" or "via-mcp" in text)
                    check(f"mcp: {tool}", good, text)

    try:
        asyncio.run(asyncio.wait_for(go(), 240))
    except Exception as exc:
        check("mcp: server runs", False, repr(exc))


def report(path: str) -> None:
    passed = sum(s == "PASS" for s, _, _ in RESULTS)
    failed = sum(s == "FAIL" for s, _, _ in RESULTS)
    warned = sum(s == "WARN" for s, _, _ in RESULTS)
    lines = [f"### fleet e2e: {platform.system()} {platform.release()}",
             f"{passed} passed, {failed} failed, {warned} warnings", "",
             "| | check | detail |", "|---|---|---|"]
    icon = {"PASS": "✅", "FAIL": "❌", "WARN": "⚠️"}
    for s, n, d in RESULTS:
        lines.append(f"| {icon[s]} | {n} | {'' if s == 'PASS' else d.replace('|', '/')[:200]} |")
    text = "\n".join(lines) + "\n"
    print("\n" + text)
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(text)


if __name__ == "__main__":
    sys.exit(main())
