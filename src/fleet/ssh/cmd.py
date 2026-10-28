"""Turn a pasted ssh command into a connectable endpoint.

The whole "just give me the ssh command" onboarding promise rests on this module, and
its central decision is to NOT hand-roll ~/.ssh/config parsing. `ssh -G` prints the
*effective* configuration after applying aliases, Match blocks, Include directives and
built-in defaults -- it is the only source that cannot disagree with the real connection.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass, field


IS_WINDOWS = sys.platform == "win32"     # see local_platform(); kept for hot paths

# ssh flags that take a value; anything else single-letter is a boolean switch.
_VALUE_FLAGS = frozenset("bcDEeFIiJLlmOopQRSWw")


@dataclass(slots=True)
class ParsedSsh:
    target: str = ""                       # [user@]host as written
    user: str | None = None
    port: int | None = None
    identity: str | None = None
    jump: str | None = None
    options: list[str] = field(default_factory=list)   # raw -o k=v
    remote_command: str = ""


@dataclass(slots=True)
class Endpoint:
    """A resolved, connectable address. One device may have several."""

    target: str                            # hostname to dial
    user: str = ""
    port: int = 22
    identity: str = ""
    jump: str = ""
    name: str = "default"
    preference: int = 10
    via: str = ""                          # mesh | lan | public
    # What ssh config resolves `target` to, when `target` is a Host alias from your
    # ~/.ssh/config. fleet dials the alias, so ssh applies that Host block -- its
    # ProxyCommand, IdentityAgent, known_hosts file -- exactly as it does for you; this
    # is only for what needs an address rather than a name ssh knows (a model server's
    # URL, which kind of route it is).
    host: str = ""

    @property
    def address(self) -> str:
        """Where the machine actually is: the HostName behind an alias, or the target."""
        return self.host or self.target

    def ssh_command(self) -> str:
        bits = ["ssh"]
        if self.port and self.port != 22:
            bits += ["-p", str(self.port)]
        if self.identity:
            bits += ["-i", self.identity]
        if self.jump:
            bits += ["-J", self.jump]
        bits.append(f"{self.user}@{self.target}" if self.user else self.target)
        return " ".join(bits)


def split_command(cmd: str, *, windows: bool = os.name == "nt") -> list[str]:
    """Words of an ssh command as the shell you typed it in would split them.

    POSIX shlex reads every backslash as an escape, so on Windows `-i C:\\Users\\me\\key`
    came out as `C:Usersmekey`, a key that does not exist. There a path's backslashes
    are kept, and only the quotes around a word are taken off.
    """
    if not windows:
        return shlex.split(cmd)
    return [t[1:-1] if len(t) > 1 and t[0] == t[-1] and t[0] in "\"'" else t
            for t in shlex.split(cmd, posix=False)]


def parse_ssh_command(cmd: str) -> ParsedSsh:
    """Parse `ssh -p 58418 -i ~/.ssh/k root@1.2.3.4 [remote cmd]` or a bare alias."""
    tokens = split_command(cmd.strip())
    if tokens and os.path.basename(tokens[0]) in ("ssh", "ssh.exe"):
        tokens = tokens[1:]

    out = ParsedSsh()
    rest: list[str] = []
    i = 0
    # As ssh reads it: options before the host and straight after it (`ssh root@h -p 2222
    # -L 8080:localhost:8080`, as rental providers write it), then the first other word
    # starts the remote command, whose own flags are not ssh's. Reading on past it took
    # `nvidia-smi -i 0` for a key named "0".
    while i < len(tokens):
        tok = tokens[i]
        if tok == "--":
            rest += tokens[i + 1:]
            break
        if not (tok.startswith("-") and len(tok) > 1) or len(rest) > 1:
            rest.append(tok)
            if len(rest) > 1:
                rest += tokens[i + 1:]
                break
            i += 1
            continue
        j = 1
        while j < len(tok):                 # `-4A`, `-Ap 2222`: grouped switches
            flag = tok[j]
            if flag not in _VALUE_FLAGS:
                j += 1
                continue
            val = tok[j + 1:]
            if not val:
                i += 1
                val = tokens[i] if i < len(tokens) else ""
            if not val:
                raise ValueError(f"-{flag} needs a value")
            match flag:
                case "p":
                    # A typo here (`-p 22x`) used to dial port 22 and report "no ssh
                    # server is listening on that port" about the right machine.
                    if not val.isdigit() or not 0 < int(val) < 65536:
                        raise ValueError(f"-p {val!r} is not a port")
                    out.port = int(val)
                case "i":
                    out.identity = val
                case "J":
                    out.jump = val
                case "l":
                    out.user = val
                case "o":
                    out.options.append(val)
                    # `-o IdentityFile=~/.ssh/k` is the same key as `-i ~/.ssh/k`;
                    # without this it was used to resolve the address and then left
                    # out of the record, so fleet never offered it.
                    key, _, ident = val.replace("=", " ", 1).partition(" ")
                    if key.strip().lower() == "identityfile" and ident.strip():
                        out.identity = out.identity or ident.strip()
            break
        i += 1

    if rest:
        out.target = rest[0]
        out.remote_command = " ".join(rest[1:])
    if not out.target:
        raise ValueError(f"no address in {cmd.strip()!r}")
    for value in (out.target, out.jump or ""):
        if value.startswith("-"):
            raise ValueError(f"not an address: {value!r}")
    if "@" in out.target:
        user, _, host = out.target.rpartition("@")
        out.user = out.user or user
        out.target = host
    return out


def resolve(parsed: ParsedSsh, *, timeout: float = 10.0) -> Endpoint:
    """Ask ssh itself what this command actually resolves to."""
    for opt in parsed.options:
        key = opt.replace("=", " ", 1).split(" ", 1)[0].strip().lower()
        if key == "proxycommand":
            # What is recorded is an address, a user, a port, a key and a jump host. A
            # ProxyCommand was dropped without a word, and the machine `ssh` reached
            # through it read as "DNS lookup failed" or "off, asleep".
            raise ValueError(
                "fleet records an ssh command's address, user, port, key (-i) and jump "
                "host (-J), not -o ProxyCommand: put it in a Host block in ~/.ssh/config "
                "and give that Host's name instead")
    argv = ["ssh", "-G"]
    if parsed.port:
        argv += ["-p", str(parsed.port)]
    if parsed.identity:
        argv += ["-i", parsed.identity]
    if parsed.jump:
        argv += ["-J", parsed.jump]
    for opt in parsed.options:
        argv += ["-o", opt]
    argv.append(f"{parsed.user}@{parsed.target}" if parsed.user else parsed.target)

    proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8",
                          errors="replace", timeout=timeout)
    if proc.returncode != 0:
        raise ValueError(f"ssh -G failed for {parsed.target!r}: {proc.stderr.strip()[:200]}")

    cfg: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        key, _, val = line.partition(" ")
        cfg.setdefault(key.strip().lower(), val.strip())    # first wins, as ssh does

    # Only a key named with `-i` is the machine's own. ssh -G lists the five default
    # identity files whether or not they were asked for, and recording the first that
    # existed pinned every machine to ~/.ssh/id_ed25519 with IdentitiesOnly=yes: a key
    # that lives only in your agent (1Password, a hardware key) was then never offered,
    # and a machine `ssh` reached refused fleet. Anything else ssh picks itself.
    identity = ""
    if parsed.identity:
        given = os.path.expanduser(parsed.identity)
        if os.path.exists(given) and not is_default_identity(given):
            identity = given

    hostname = cfg.get("hostname") or parsed.target
    # A Host alias stays the target: dialled by its resolved HostName, ssh would no longer
    # match the alias's Host block, and its ProxyCommand and the rest were lost -- `ssh
    # box` worked while fleet said "DNS lookup failed". An address, or a name ssh config
    # leaves alone, is the same either way.
    alias = parsed.target if hostname.lower() != parsed.target.lower() else ""
    return Endpoint(
        target=alias or hostname,
        user=cfg.get("user", ""),
        port=int(cfg.get("port", "22") or 22),
        identity=identity,
        jump=cfg.get("proxyjump", "") if cfg.get("proxyjump", "none") != "none" else "",
        host=hostname if alias else "",
    )


# The keys ssh offers on its own, without -i, from ~/.ssh.
_DEFAULT_IDENTITIES = frozenset({"id_rsa", "id_ecdsa", "id_ecdsa_sk", "id_ed25519",
                                 "id_ed25519_sk", "id_xmss", "id_dsa"})


def is_default_identity(path: str) -> bool:
    """Whether ssh would offer this key anyway, so naming it adds only IdentitiesOnly --
    which shuts out every key in the agent. Older inventories name one for nearly every
    machine, from before `resolve` stopped recording them."""
    p = Path(os.path.expanduser(path or ""))
    return p.name in _DEFAULT_IDENTITIES and p.parent == Path.home() / ".ssh"



def run(argv: list[str], *, input: bytes | None = None, timeout: float | None = None,
        text: bool = False, env: dict | None = None) -> subprocess.CompletedProcess:
    """Run an ssh invocation and capture what it said.

    Every ssh fleet spawns goes through here, because on Windows `capture_output=True`
    does not work: **ssh.exe hangs when its stdin or its stdout is an anonymous pipe**,
    and hangs for good -- no timeout of its own, no output, no CPU.

    Measured on the Windows center rather than inferred, after it could not reach a
    single machine. The same command, same host, same key: stdout to a pipe timed out
    every time; stdout to a file returned in 0.2s with `debug1: Exit status 0`. stderr on
    a pipe is fine; stdin as a pipe hangs it too, so a 24KB envelope only crossed once
    both ends were files.

    That is why the center's sweep reached nothing while `fleet ls` on the same box
    looked healthy -- the reads came from cache, and every write was an ssh that never
    returned. POSIX keeps the ordinary pipes; it has no such problem and the temp files
    would only be litter.
    """
    if local_platform() != WINDOWS:
        return subprocess.run(argv, input=input, capture_output=True, timeout=timeout,
                              text=text, env=env)

    import tempfile

    with tempfile.TemporaryDirectory() as scratch:
        out_path = Path(scratch) / "stdout"
        err_path = Path(scratch) / "stderr"
        in_path = Path(scratch) / "stdin"
        if input is not None:
            in_path.write_bytes(input)
        stdin = in_path.open("rb") if input is not None else subprocess.DEVNULL
        try:
            with out_path.open("wb") as out, err_path.open("wb") as err:
                proc = subprocess.run(argv, stdin=stdin, stdout=out, stderr=err,
                                      timeout=timeout, env=env)
        finally:
            if input is not None:
                stdin.close()
        raw_out, raw_err = out_path.read_bytes(), err_path.read_bytes()

    if text:
        raw_out = raw_out.decode(errors="replace")
        raw_err = raw_err.decode(errors="replace")
    return subprocess.CompletedProcess(argv, proc.returncode, raw_out, raw_err)


def resolve_command(cmd: str, *, timeout: float = 10.0) -> Endpoint:
    return resolve(parse_ssh_command(cmd), timeout=timeout)


def control_dir() -> str | None:
    """ControlMaster socket directory, or None where multiplexing is unavailable.

    Must be short: AF_UNIX paths cap at 104 bytes and '%C' plus a long state dir
    silently overflows it on macOS.
    """
    if IS_WINDOWS:
        return None      # Windows OpenSSH has no ControlMaster
    d = f"/tmp/fleet-{os.getuid()}"
    try:
        os.makedirs(d, mode=0o700, exist_ok=True)
        os.chmod(d, 0o700)
    except OSError:
        return None
    return d


POSIX, WINDOWS = "posix", "windows"


def remote_platform(snapshot: dict | None) -> str:
    """Which shell the far side speaks, from the last probe.

    One function, one answer. Before this there were four places deciding it and they
    disagreed: `fleet ssh` read the snapshot, the reconciler compared `ssh_auth` against
    a value it never holds -- so every grant on a Windows host silently ran the POSIX
    script -- and handover and `--leave` simply assumed POSIX.

    Read from the probe rather than stored on the Device on purpose. A field would ride
    `inventory.merge`, where a peer with a fast clock could flip a host's platform and
    so choose which shell we hand it a command in. A machine nobody has probed reads
    POSIX, which is both the common case and the safe way to be wrong: a POSIX command
    on Windows fails loudly, where the reverse can appear to succeed.
    """
    # `uname_s` first: both payloads send it and it is a clean Linux/Darwin/Windows
    # token, where `os` is marketing text ("Microsoft Windows 11 Pro", "Ubuntu 26.04
    # LTS") that has to be sniffed. Snapshots taken before it was parsed have neither,
    # so the sniff stays as the fallback.
    if uname_s := str((snapshot or {}).get("uname_s") or ""):
        return WINDOWS if uname_s.lower() == "windows" else POSIX
    os_name = str((snapshot or {}).get("os") or "")
    return WINDOWS if os_name.lower().startswith(("windows", "microsoft windows")) else POSIX


def local_platform() -> str:
    """Which shell *this* machine speaks. The twin of `remote_platform`, same vocabulary.

    Worth a function rather than a scattered `sys.platform == "win32"`: there were three
    such tests and they had already drifted apart in shape, which is how the local probe
    kept running the POSIX payload on a Windows center. One name, one answer, and the
    same two constants the remote side uses -- so a caller choosing a shell asks the same
    question whichever end it is talking to.
    """
    return WINDOWS if sys.platform == "win32" else POSIX


def local_shell_argv() -> list[str]:
    """How to hand this machine a script on stdin, whatever it runs."""
    return (["powershell", "-NoProfile", "-Command", "-"] if local_platform() == WINDOWS
            else ["sh", "-s"])


def remote_command(args: list[str], *, windows: bool = False) -> str:
    """What to hand ssh when the caller asked to run something, rather than get a shell.

    A non-interactive ssh runs with a minimal PATH -- no ~/.local/bin -- so anything
    installed by uv, pipx or cargo is invisible, fleet included. That is why
    `fleet ssh box -- fleet ls` failed with "command not found" while `fleet sync`
    worked: sync already wrapped its command this way.

    The login shell covers hosts that set PATH from a profile, and the explicit export
    covers hosts that set it from .bashrc, which a login shell does not read.

    Arguments are joined with spaces and left for the far shell to parse, exactly as
    OpenSSH itself joins them. Quoting each word instead would be safer in the abstract
    and would break `fleet ssh box "fleet ls | head"`, which is how people actually use
    it.
    """
    if not args:
        return ""                           # no command: the user wants a login shell
    if windows:
        # Every word of the reasoning above is POSIX: there is no `sh`, no login shell
        # and no ~/.local/bin. cmd.exe hands `sh -lc '...'` straight back as "not
        # recognized", so the wrapper that makes this work everywhere else is exactly
        # what breaks it here. Windows sets PATH machine-wide, so passing the command
        # through is both simpler and correct.
        return " ".join(args)
    inner = 'export PATH="$HOME/.local/bin:$PATH"; ' + " ".join(args)
    return "sh -lc " + shlex.quote(inner)



def expanded_here(args: list[str], *, home: str, here_windows: bool,
                  there_windows: bool) -> str:
    """A warning when this machine's shell has already rewritten the remote command.

    fleet passes the command on untouched -- but the shell that ran fleet may not have.
    PowerShell expands `$HOME` and `${HOME}` inside double quotes, does not treat `\\$`
    as an escape, and since 7.4 replaces a leading `~` in a native command's argument
    with the local home. So `fleet ssh box -- ls ~/x` from Windows arrived on a Linux box
    as `ls C:\\Users\\me/x`, and failed with an error about a path nobody typed. The
    command is gone by the time fleet sees it, so the most fleet can do is notice this
    machine's home folder in a command bound for a machine that cannot have it.
    """
    if not (here_windows and not there_windows and home):
        return ""
    folded = home.lower()
    if not any(folded in a.lower() or folded.replace("\\", "/") in a.lower() for a in args):
        return ""
    return (f"this command contains {home}, this machine's home folder -- most likely "
            "your shell expanded $HOME or ~ before fleet saw it. Put the remote command "
            "in single quotes so the other machine expands it: "
            "fleet ssh NAME -- 'ls ~/x'")


# Overlay suffixes worth knowing by name. Deliberately a table, not logic: fleet is
# neutral about which overlay you run, and adding one must not mean touching a
# classifier. Address ranges cannot do this job -- 100.64.0.0/10 is shared by several
# overlays and others use ranges the user picked.
_OVERLAY_SUFFIXES = (".ts.net", ".netbird.cloud", ".zerotier.net")


def route_of(stored: str, target: str = "") -> str:
    """The route kind for one endpoint: what was recorded, else what the address says.

    One helper because two callers need the same answer. `inventory.endpoints_of` wants
    it to decide which address `edit` may overwrite; `view._reachability` wants it for
    the `public-ip`/`mesh`/`lan` facts. Reading the raw record in one place and the
    normalised value in the other is how the facts came back empty for a whole fleet
    whose endpoints all said `tailscale`.

    Classifying the target as a fallback backfills every record written before routes
    were classified at all -- no DNS, so this stays safe on a read path: a literal
    address or a known overlay suffix answers, anything else stays "".
    """
    stored = (stored or "").strip().lower()
    if stored == "tailscale":
        # The pre-rename spelling, still on disk. Reading one of those as direct would
        # let `edit` overwrite the one address that never moves.
        return "mesh"
    return stored or classify_route(target)


def classify_route(target: str) -> str:
    """`mesh`, `lan`, `public`, or "" when we genuinely cannot tell.

    This fills in the vocabulary `Endpoint.via` has documented all along: until now the
    only assignment anywhere set "mesh", and only for one vendor's suffix.

    "" is a real answer and must stay one. `target` here is whatever ssh config resolved
    to, usually a hostname, and guessing `public` for every name would mark a NAS on a
    LAN-only DNS name as reachable from the internet -- the exact claim someone would
    act on. Resolution is the caller's job, at add time, where a probe is already
    happening; a view must never block on DNS.
    """
    import ipaddress

    host = (target or "").strip().rstrip(".").lower()
    if not host:
        return ""
    if host.endswith(_OVERLAY_SUFFIXES):
        return "mesh"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return ""
    if ip.is_loopback or ip.is_link_local or ip.is_unspecified:
        return ""
    # CGNAT is what every overlay hands out, and a home ISP behind it is not somewhere
    # you can dial either way -- "not public" is the part that matters here.
    if ip in ipaddress.ip_network("100.64.0.0/10"):
        return "mesh"
    if ip.is_private:
        return "lan"
    return "public" if ip.is_global else ""


def build_argv(ep: Endpoint, *, connect_timeout: int = 8, multiplex: bool = True,
               remote: str = "sh -s", env: dict[str, str] | None = None) -> list[str]:
    argv = [
        "ssh",
        "-o", "BatchMode=yes",
        "-o", f"ConnectTimeout={connect_timeout}",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=2",
        # accept-new, never 'no': takes first-contact keys but still fails loudly on a
        # CHANGED key, which is the right call for recycled rental ports.
        "-o", "StrictHostKeyChecking=accept-new",
        # NOT LogLevel=ERROR: it silences the very diagnostics the failure taxonomy
        # needs (a recycled rental port then returns rc=255 with EMPTY stderr, which is
        # unclassifiable). Keep full stderr and scrub known-benign lines in Python.
        "-o", "LogLevel=INFO",
    ]
    # Otherwise ssh answers exactly as it would for you: your ~/.ssh/config, your agent,
    # your keys, Tailscale SSH. Only a machine recorded with a key of its own (a rental's
    # `ssh -i ~/.ssh/vast`) is pinned to it -- IdentitiesOnly, so the five default
    # identities do not trip MaxAuthTries before the right one is offered.
    if ep.identity:
        argv += ["-o", "IdentitiesOnly=yes", "-i", ep.identity]
    if ep.port and ep.port != 22:
        argv += ["-p", str(ep.port)]
    if ep.jump:
        argv += ["-J", ep.jump]
    if multiplex and (cdir := control_dir()):
        argv += ["-o", "ControlMaster=auto", "-o", f"ControlPath={cdir}/%C",
                 "-o", "ControlPersist=120"]
    argv += ["--", f"{ep.user}@{ep.target}" if ep.user else ep.target]
    prefix = " ".join(f"{k}={shlex.quote(v)}" for k, v in (env or {}).items())
    argv.append(f"{prefix} {remote}".strip())
    return argv
