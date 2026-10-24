# Windows

Windows works both ways: as a machine you watch, and as the machine you run fleet from.
Every command is run end to end on Windows in CI, from scripts as an agent runs them and
at a real terminal as you do.

## A Windows machine you watch

It needs **OpenSSH Server** and nothing else. Install it from *Settings → System →
Optional features*, or in an administrator PowerShell:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service sshd -StartupType Automatic
```

Tailscale's SSH server does not run on Windows, so even on a tailnet a Windows machine
is reached through its own OpenSSH server — by its tailnet name or address — and lets you
in with your key.

**Put your key on it once.** fleet connects with your key, as ssh does, and manages no
access itself; Windows' sshd reads keys from one of two files, depending on the account:

| The account you log in as | The file sshd reads |
|---|---|
| a normal user | `C:\Users\you\.ssh\authorized_keys` |
| an administrator (most Windows logins) | `C:\ProgramData\ssh\administrators_authorized_keys`, shared by every administrator, and ignored unless only SYSTEM and Administrators can write it |

Both mistakes — the key in the wrong file, or the right file with the wrong permissions —
fail silently: the key is there and does not work. `fleet doctor NAME` prints the exact
PowerShell line for each case, with your key in it; the lines are also in
[Reaching your machines](reaching-machines.md#windows-or-mac).

Then add it like any other machine: "add `ssh me@192.168.1.20`", or
`fleet add "ssh me@192.168.1.20"`.

fleet notices from the first probe that the machine runs Windows and adapts; you never
say so:

- It is measured with PowerShell instead of a POSIX shell, and a reading writes nothing there.
- `fleet ssh box -- dir` passes the command to the remote shell as it is, rather than
  wrapping it for a shell that is not there. The remote command's exit code comes back.
- `fleet edit box --path models=D:\models` writes the machine's notes to
  `%LOCALAPPDATA%\fleet\machine.yaml`; a machine-wide file can sit in
  `%ProgramData%\fleet\machine.yaml` ([more](resources.md#machineyaml)).
- NVIDIA cards and their jobs are read with `nvidia-smi`, as on Linux. AMD cards are not
  read on Windows yet.

## Running fleet on Windows

Install it from PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 | iex"
```

Open a new terminal afterwards so `fleet` is on your PATH. fleet uses Windows' own
OpenSSH client (`ssh.exe`), with your keys in `C:\Users\you\.ssh` and the ssh-agent
service if you run it.

- `fleet ssh NAME` gives an interactive session that owns the keyboard until you exit;
  Ctrl+C goes to the remote program, not to fleet.
- `fleet top` runs in Windows Terminal and the classic console; `q` quits.
- `fleet update` updates this machine in the background, once the command has exited:
  Windows cannot replace a program while it runs. It takes a minute; `fleet --version`
  shows the result, and `update.log` in fleet's state folder says how it went.
- Connections are not shared between commands, because Windows' OpenSSH has no
  `ControlMaster`; each reading pays for its own handshake.

**Quote what you send.** PowerShell expands `$HOME` inside double quotes and, from 7.4,
a leading `~` in any argument, before fleet sees it. Commands for another machine go in
single quotes: `fleet ssh gpu-box -- 'ls ~/proj'`. See [Running work](run-work.md#commands).

**No `ssh-copy-id`.** Windows has none, so `fleet add` does not offer to put your key on a
machine that refuses it. `fleet doctor NAME` prints the line to run on that machine
instead.

## If something does not work

| What you see | What to do |
|---|---|
| `fleet` is not found after installing | Open a new terminal. |
| `ssh: connect to host … port 22: Connection refused` | sshd is not running there: `Start-Service sshd`. |
| a Windows machine refuses a key you know you added | It is in the wrong file, or the administrators file has the wrong permissions. `fleet doctor NAME` prints the right line. |
| `! this command contains C:\Users\…, this machine's home folder` | PowerShell expanded `$HOME` or `~` before fleet saw the command. Put the remote command in single quotes. |
| a `fleet-center` scheduled task, or firewall rules named `fleet center` | Left by fleet 0.5. See [Upgrading from 0.5](upgrading-from-0.5.md#the-background-service). |

More in [Troubleshooting](troubleshooting.md).
