"""End to end on one real machine: every fleet command, run the way a person runs it.

Run by .github/workflows/e2e.yml on Linux, macOS and Windows runners, after the
installer has put fleet on the runner. The runner is where fleet runs and, over ssh to
localhost with the runner's own key, a target too: the probe, a machine's notes and
`fleet ssh` run through sshd and that OS's own shell, which is the code that differs.

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
    except FileNotFoundError as exc:              # the program itself is gone
        return 127, str(exc)


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

def own_key() -> str:
    """This runner's own ssh key -- the user's, which is all fleet ever uses -- made if
    there is none, so a plain `ssh user@localhost` gets in the way it would for you."""
    key = Path.home() / ".ssh" / "id_ed25519"
    if not key.exists():
        key.parent.mkdir(parents=True, exist_ok=True)
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(key)])
    return key.with_suffix(".pub").read_text(encoding="utf-8").strip()


def place_key(user: str, pubkey: str) -> str:
    """Put a key where sshd on this OS reads it for `user` -- as `fleet doctor` tells a
    person to, by hand."""
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
    expect("fleet --help lists every command", ["--help"], r"doctor")
    expect("fleet paths", ["paths"], r"machines")
    code, out = fleet("center", "--json")
    check("there is no center any more", code != 0, out)

    expect("ls shows this machine", ["ls"], platform.node().split(".")[0][:8] or ".")
    ok, out = expect("ls --json", ["ls", "--json"], r'"name"')
    expect("show (this machine)", ["show"], r".")
    expect("show --json", ["show", "--json"], r'"name"')
    expect("ls --discover --json", ["ls", "--discover", "--json"], r'"candidates"')

    # This machine over ssh, as you would reach it: your own key, put there by hand.
    # Before it is there, fleet says it was refused and names the fix, and records nothing.
    pub = own_key()
    if not WINDOWS:
        code, out = fleet("add", "--json", f"ssh -o StrictHostKeyChecking=accept-new "
                          f"nobody-{os.getpid()}@localhost")
        verdict = as_json(out) or {}
        check("add of a machine that refuses us records nothing and names the fix",
              code != 0 and bool(verdict.get("fix")), out)
    path = place_key(a.ssh_user, pub)
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
    expect("doctor NAME (it lets us in)", ["doctor", name, "--json"], r'"status": "ok"')
    expect("doctor (nothing failing)", ["doctor"], r".")
    # A runner cannot be put to sleep: only that wake refuses, or sends nothing, for one
    # that is up -- and reads this OS's cards on the way.
    expect("wake of a machine that is up sends nothing", ["wake", name, "--json"],
           r'"already_up": true|"woke": true|is this machine|network card', rc=None)
    expect("wake of an unknown machine", ["wake", "no-such-machine"], r"No device", rc=1)

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

    resource_checks(name)

    # top needs a terminal; without one it must say so rather than hang.
    code, out = fleet("top", "-i", "1", timeout=8)
    check("top without a terminal exits or explains", code != 124 or bool(out.strip()),
          f"exit {code}: {out}", required=False)

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
    mcp_timeout_check(name)

    expect("rm NAME --yes", ["rm", name, "--yes"], r"removed")
    expect("rm leaves the machine reachable by ssh", ["ls", "--json"], r".")

    # Last, because it replaces the installed fleet and stops anything running from it.
    local_update_check()
    migration_check()
    ok, out = expect("uninstall --purge", ["uninstall", "--yes", "--purge"],
                     r"uninstall agents-fleet|the way it was installed")
    check("uninstall: the agents no longer hear about fleet",
          not (Path.home() / ".agents/skills/fleet/SKILL.md").exists(), out)

    report(a.report)
    return 1 if any(s == "FAIL" for s, _, _ in RESULTS) else 0


def resource_checks(name: str) -> None:
    """What a machine has beyond its hardware, over ssh to this runner as a target: the
    folders its own machine.yaml names, its software, GPU jobs and model servers."""
    folder = tempfile.mkdtemp(prefix="fleet-models-")
    Path(folder, "weights.bin").write_bytes(b"x" * 200_000)
    ok, out = expect("edit --path writes the machine's own machine.yaml",
                     ["edit", name, "--path", f"models={folder}", "--json"], r'"ok": true')
    ok, out = expect("show --rescan --json", ["show", name, "--rescan", "--json"], r'"storage"')
    row = as_json(out) or {}
    models = (row.get("storage") or {}).get("models") or []
    check("  the folder is reported, and found", any(m.get("path") == folder and m.get("exists")
                                                    for m in models), str(row.get("storage")))
    check("  its software is reported", bool((row.get("software") or {}).get("system")),
          str(row.get("software")), required=not WINDOWS)
    for _ in range(10):                  # the size is worked out in the background
        time.sleep(1)
        ok, out = fleet("show", name, "--json")
        sized = [m for m in ((as_json(out) or {}).get("storage") or {}).get("models", [])
                 if m.get("size_gb") is not None]
        if sized:
            break
    check("  and its size, once worked out", bool(sized), out[-600:], required=not WINDOWS)
    expect("ps --json", ["ps", "--json"], r'"jobs"')
    expect("ps", ["ps"], r".")
    expect("edit --note and --tag land in the machine's own file",
           ["edit", name, "--note", "e2e box", "--tag", "e2e-own", "--json"], r'"e2e-own"')
    expect("edit --endpoint declares a server", ["edit", name, "--endpoint", "mine=9", "--json"],
           r'"mine"')
    ok, out = expect("show --rescan --wait", ["show", name, "--rescan", "--wait", "20", "--json"],
                     r'"observed_at"')
    expect("ls --json --fields", ["ls", "--json", "--fields", "free_vram_mib"], r'"free_vram_mib"')
    expect("ls --discover --hide", ["ls", "--discover", "--hide", "e2e-nothing", "--json"],
           r'"e2e-nothing"')
    expect("edit --endpoint NAME= removes it", ["edit", name, "--endpoint", "mine=", "--json"],
           r'"ok": true')
    expect("models --json", ["models", "--json"], r'"servers"')
    expect("models", ["models"], r".")
    expect("edit --path KIND= clears it", ["edit", name, "--path", "models=", "--json"],
           r'"ok": true')


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
    plain = Term(reporting_exit(["ssh", "-tt", "-i", str(Path.home() / ".ssh" / "id_ed25519"), "-o",
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
                for tool, args in [("list_machines", {}), ("list_gpu_jobs", {}),
                                   ("list_models", {}), ("discover_machines", {}),
                                   ("run_on_machine", {"machine": machine, "command": "echo via-mcp"})]:
                    res = await s.call_tool(tool, args)
                    text = res.content[0].text if res.content else ""
                    good = not getattr(res, "isError", False) and (tool != "run_on_machine" or "via-mcp" in text)
                    check(f"mcp: {tool}", good, text)

    try:
        asyncio.run(asyncio.wait_for(go(), 240))
    except Exception as exc:
        check("mcp: server runs", False, repr(exc))


def mcp_timeout_check(machine: str) -> None:
    """A tool call that runs out of time returns, and stops what it started.

    On Windows fleet.exe is a launcher for python, which runs ssh. A timeout used to
    kill only the launcher; the rest held the output pipes, and the call waited for
    the remote command to finish however long it took.
    """
    from fleet import mcpserver

    mcpserver._fleet = lambda: FLEET
    mcpserver.TIMEOUT_S = 8
    slow = "ping -n 120 127.0.0.1 >NUL" if WINDOWS else "sleep 120"
    t0 = time.monotonic()
    out = mcpserver._run(["ssh", machine, "--", slow])
    took = time.monotonic() - t0
    check("mcp: a command past the time limit returns, stopped",
          took < 60 and out.get("ok") is False and "exceeded" in str(out.get("error")),
          f"{took:.0f}s: {out}")


def local_update_check() -> None:
    """`fleet update` on the machine it runs on, through the installer's real path.

    From git, so the installer stops fleet and reinstalls it. On Windows that is the
    case that broke: the installer killed the `fleet update` that started it while its
    python held the files being replaced. There it now runs once fleet has exited.
    """
    from fleet.install import UPDATE_EXIT_MARK

    repo = Path(__file__).resolve().parents[2]
    run(["git", "-C", str(repo), "branch", "-f", "e2e-update", "HEAD"])
    code, out = fleet("update", "--repo", str(repo), "--ref", "e2e-update",
                      env={"COLUMNS": "1000"}, timeout=600)
    if not check("update this machine", code == 0, f"exit {code}: {out}"):
        found = re.search(r"in (\S+update-run\.log)", out)
        if found and Path(found.group(1)).exists():
            print("      runner said:", Path(found.group(1)).read_text(errors="replace")[-3000:])
        return
    if WINDOWS:
        found = re.search(r"the log is (.+?update\.log)", out)
        if not check("update: runs in the background on Windows", bool(found), out):
            return
        log = Path(found.group(1).strip())
        text = ""
        deadline = time.monotonic() + 600
        while time.monotonic() < deadline:
            text = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            if UPDATE_EXIT_MARK in text:
                break
            time.sleep(3)
        runner_log = log.with_name("update-run.log")
        said = runner_log.read_text(encoding="utf-8", errors="replace") if runner_log.exists() else ""
        check("update: the background install finished cleanly",
              f"{UPDATE_EXIT_MARK} 0" in text, f"{text[-3000:]}\n-- runner: {said[-2000:]}")
    expect("fleet works after the update", ["--version"], r"^fleet \d")
    expect("and reads its state", ["paths"], r".", rc=None)


def migration_check() -> None:
    """A machine still on `fleet-broker`, fleet's name before 0.5, with no checkout of
    fleet on it: the updater moves it to `agents-fleet` from PyPI.

    Found on a real fleet: such a machine fell through to "kept as it is" and stayed on
    the old version for good, because only a checkout gave the updater a way across the
    rename. The old tool is a stand-in that claims the `fleet` command as the real one
    did; what replaces it is the published release.
    """
    from fleet.install import install_script

    uv = shutil.which("uv") or "uv"
    stub = Path(tempfile.mkdtemp(prefix="fleet-broker-"))
    (stub / "fleet_broker_stub.py").write_text(
        "def main():\n    print('fleet 0.4.0')\n", encoding="utf-8")
    (stub / "pyproject.toml").write_text(
        '[project]\nname = "fleet-broker"\nversion = "0.4.0"\n'
        '[project.scripts]\nfleet = "fleet_broker_stub:main"\n'
        '[build-system]\nrequires = ["setuptools"]\nbuild-backend = "setuptools.build_meta"\n'
        '[tool.setuptools]\npy-modules = ["fleet_broker_stub"]\n', encoding="utf-8")
    run([uv, "tool", "uninstall", "agents-fleet"])
    code, out = run([uv, "tool", "install", "--quiet", str(stub)], timeout=300)
    if not check("migration: an old fleet-broker install to start from", code == 0
                 and "fleet 0.4.0" in fleet("--version")[1], out):
        return
    # No checkout: that is the case that used to be stranded.
    checkout = Path.home() / ".local" / "share" / "fleet"
    # git writes its objects read-only, and on Windows rmtree cannot delete a read-only
    # file: ignoring that left a half-deleted .git, which the updater then took for a
    # checkout to build from.
    def _writable(func, path, _exc):
        os.chmod(path, 0o700)
        func(path)
    if checkout.exists():
        shutil.rmtree(checkout, onexc=_writable) if sys.version_info >= (3, 12) else \
            shutil.rmtree(checkout, onerror=_writable)
    if not check("migration: no checkout of fleet left on the machine", not checkout.exists(),
                 str(checkout)):
        return
    script = install_script("https://github.com/sinolion/fleet", update_only=True,
                            platform="windows" if WINDOWS else "")
    if WINDOWS:
        ps1 = stub / "update.ps1"
        ps1.write_text(script, encoding="utf-8")
        code, out = run(["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                         str(ps1)], timeout=600)
    else:
        code, out = run(["sh", "-s"], input=script, timeout=600)
    _, tools = run([uv, "tool", "list"])
    _, version = fleet("--version")
    found = re.search(r"fleet (\d+)\.(\d+)\.(\d+)", version)
    check("migration: fleet update moves fleet-broker to agents-fleet",
          code == 0 and "kept as it is" not in out and "fleet-broker" not in tools
          and "agents-fleet" in tools and bool(found)
          and tuple(map(int, found.groups())) >= (0, 5, 1),
          f"exit {code}: {out[-1500:]}\n-- tools: {tools}\n-- version: {version}")


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
