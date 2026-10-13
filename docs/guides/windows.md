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
- Every administrator account on a Windows machine shares that one file, so sshd lets a
  key in it log in as any of them: a grant to one administrator account is in effect a
  grant to all of them, and revoking one revokes it for all. Grant a non-administrator
  account when that difference matters.
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
  window, with no time limit, on battery too, and restarted if it stops. What it does
  goes to `center-service.log` in fleet's state folder.
- `fleet update` updates this machine in the background, once the command has exited:
  Windows cannot replace a program while it runs. It takes a minute; `fleet --version`
  shows the result, and `update.log` in fleet's state folder says how it went.

**Quote what you send.** PowerShell expands `$HOME` inside double quotes and, from 7.4,
a leading `~` in any argument, before fleet sees it. Commands for another machine go in
single quotes: `fleet ssh gpu-box -- 'ls ~/proj'`. See [Running work](run-work.md#commands).

**The background service is the one step that can need an administrator.** It is a
scheduled task registered as you, and most accounts may create one. Some may not -- a
managed machine's policy, or a `fleet-center` task left by an earlier install that was
registered to run elevated and can now be neither deleted nor replaced. fleet then says
which it is, with ✗, and prints the fix: in an administrator PowerShell,
`schtasks /delete /tn fleet-center /f`, then as yourself `fleet service install`. Until
then the center works while `fleet center --listen` runs in a terminal you leave open.
`fleet service status` says whether the task or something else is holding port 7373.

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
| `✗ service: a scheduled task named fleet-center is already here …` or `could not register the scheduled task` | See the service note above: one command in an administrator shell, then `fleet service install`. |
| `! this command contains C:\Users\…, this machine's home folder` | PowerShell expanded `$HOME` or `~` before fleet saw the command. Put the remote command in single quotes. |

More in [Troubleshooting](troubleshooting.md).
