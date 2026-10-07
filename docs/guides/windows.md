# Windows

Windows works both ways: as a machine in your fleet, and as the machine you run fleet
from, center included. Every command is run end to end on Windows in CI, from scripts as
an agent runs them and at a real terminal as you do.

## A Windows machine in your fleet

It needs **OpenSSH Server** and nothing else. Install it from *Settings → System →
Optional features*, or in an administrator PowerShell:

```powershell
Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0
Start-Service sshd
Set-Service sshd -StartupType Automatic
```

Then add it like any other machine: "add `ssh me@192.168.1.20`", or
`fleet add "ssh me@192.168.1.20"`.

fleet notices from the first probe that the machine runs Windows and adapts; you never
say so:

- It is measured with PowerShell instead of a POSIX shell.
- Keys go where Windows' sshd actually reads them. For an administrator account that is
  `C:\ProgramData\ssh\administrators_authorized_keys`, with the strict permissions sshd
  insists on (it silently ignores the file otherwise); for other accounts it is
  `~\.ssh\authorized_keys`. `fleet rm` on the center removes the fleet's keys from the
file it placed them in; `fleet center --leave` on the machine itself cleans both.
- `fleet ssh box -- dir` passes the command to the remote shell as it is, rather than
  wrapping it for a shell that is not there. The remote command's exit code comes back.

## Running fleet on Windows

Install it from PowerShell:

```powershell
powershell -ExecutionPolicy ByPass -c "irm https://raw.githubusercontent.com/lion-zhang/fleet/main/install.ps1 | iex"
```

Open a new terminal afterwards so `fleet` is on your PATH.

- `fleet ssh NAME` gives an interactive session that owns the keyboard until you exit;
  Ctrl+C goes to the remote program, not to fleet.
- `fleet top` runs in Windows Terminal and the classic console; `q` quits.
- The center's background service is a scheduled task that runs without a console
  window.
- `fleet update` updates this machine in the background, once the command has exited:
  Windows cannot replace a program while it runs. It takes a minute; `fleet --version`
  shows the result, and `update.log` in fleet's state folder says how it went.

**One limit: a Windows machine cannot type a password for you**, because Windows has no
pseudo-terminal for ssh to prompt on. So a Windows center adds only machines that already
accept a key — yours, or the fleet's. For a password-only host, put the fleet's key there
first (`fleet center --pubkey` on the center prints it), or use an [invite](add-machines.md#let-the-machine-join-by-itself).

## If something does not work

| What you see | What to do |
|---|---|
| `fleet` is not found after installing | Open a new terminal. |
| `ssh: connect to host … port 22: Connection refused` | sshd is not running there: `Start-Service sshd`. |
| `… accepts no key of ours, and this machine cannot type a password` | Expected on a Windows center; see the limit above. |

More in [Troubleshooting](troubleshooting.md).
