# Upgrading from 0.5

fleet 0.6 removed fleet's whole access layer. There is no center any more, no members, no
invites, no access list, and no background service. fleet now **manages no access**: it
runs `ssh` exactly as you do, and watches the machines that lets in. This page says what
changed, what you need to do (usually nothing), and what 0.5 left behind that you may
tidy up by hand.

## What changed

| In 0.5 | In 0.6 |
|---|---|
| one machine was the **center**; others were **members** | every machine running fleet is equal, and probes the machines its own ssh reaches |
| `fleet center`, `fleet invite`, `fleet join`, `fleet sync`, `fleet access`, `fleet service` | gone. `fleet doctor` and `fleet ls --discover` are new |
| fleet had its own key, `id_ed25519` in its folder, and put it in every machine's `authorized_keys` | fleet has no key. It uses yours, your ssh config and agent, Tailscale SSH, your provider's key |
| fleet granted and revoked access between machines | access is yours to manage, where it already lives ([how](reaching-machines.md)) |
| the inventory, `inventory.yaml`, was synced from the center to every member | each machine keeps its own list, `machines.yaml`; share it with your dotfiles if you like |
| a listener on TCP 7373, kept running by a service | nothing listens, and nothing runs in the background |
| a machine that only took a password: fleet asked for it once | fleet never asks for a password; at a terminal it offers `ssh-copy-id`, which asks for it itself |
| the installer took `--join CODE` / `FLEET_JOIN` | not needed: the installer says so and ignores it |
| `FLEET_NO_AUTO_CENTER` | gone; `FLEET_NO_FIRST_RUN` skips the first-run setup instead |
| settings `sync_ttl_s`, `access_retry_s` | gone; fleet ignores them in `config.yaml` |

Why: fleet's access layer was a second copy of "who may log in where", beside the ones
you already keep — `authorized_keys`, a tailnet policy, a provider's account. Keeping the
two in step was most of fleet's code and most of its failure modes. Without it, fleet is
a viewer of what your own ssh reaches, and a refused machine is fixed the same way for
fleet as for `ssh`.

## What to do

**Nothing is required.** Upgrade fleet the usual way — the
[installer](install.md#upgrading), or `fleet update` — and carry on.

- **Your machines list starts empty.** 0.6 does not read 0.5's `inventory.yaml`. The
  first command records this machine; add the others again with `fleet add "ssh ..."`,
  or pick them from `fleet ls --discover`, which lists the hosts in your
  `~/.ssh/config` and on your tailnet. `inventory.yaml` is left where it was, so you can
  read the addresses from it.
- **Machines that let 0.5 in only with fleet's key will refuse 0.6.** 0.5 connected with
  its own key; 0.6 connects with yours. `fleet add` says why and what to do, and
  `fleet doctor NAME` prints the line that puts your key on it. While fleet's old key is
  still on such a machine, you can use it to put yours there (see
  [below](#the-fleet-key)).
- **The center's service goes away by itself** when you upgrade with the one-line
  installer, or when a machine already on 0.6 runs `fleet update NAME` for it: on Linux
  and macOS the old fleet's `fleet service remove` is run, on Windows the
  `fleet-center` scheduled task is deleted. Anything else — `uv tool upgrade`,
  `pipx upgrade`, a source checkout, or 0.5's own `fleet update` — leaves it in place,
  and it fails each time it starts, since 0.6 has no `fleet center`. Remove it by hand
  ([below](#the-background-service)).

The installer and `fleet update` also rewrite what your agents read about fleet. After
`uv tool upgrade` or `pipx upgrade`, run `fleet setup --refresh` yourself; `fleet ls`
reminds you while an agent still reads the 0.5 description.

## What 0.5 left behind

None of this does anything in 0.6, and none of it is in the way. Remove it when you
like. fleet 0.6 never deletes any of it itself: it is your machines' access, and
removing it is your decision.

### Blocks in `authorized_keys`

On each machine 0.5 reached — center, members and plain machines alike — there are blocks
like this in `~/.ssh/authorized_keys`, and on Windows, for an administrator, in
`C:\ProgramData\ssh\administrators_authorized_keys`:

```text
# fleet:7f3a9c:begin from=SHA256:... user=root
ssh-ed25519 AAAA... fleet:7f3a9c:laptop
# fleet:7f3a9c:end from=SHA256:...
```

Each holds one fleet key: the center's, or another machine's that you granted. 0.6 uses
none of them. **Before removing them, make sure the machine accepts your own key**:
`ssh` to it with your key (or `fleet ls NAME -r` shows `ok`), so that the fleet key was
not your only way in.

To see what is there:

```bash
grep -n '^# fleet:' ~/.ssh/authorized_keys
```

To remove every fleet block, with a backup kept beside the file — on Linux or macOS, on
that machine. It stops and changes nothing if it finds a block that is not the three
lines 0.5 wrote:

```bash
f=~/.ssh/authorized_keys
cp -p "$f" "$f.before-fleet-0.6" &&
( umask 077
  awk '/^# fleet:[^ ]*:begin /{s=1; n=0; next}
       /^# fleet:[^ ]*:end /{s=0; next}
       s {if (++n > 1) {print "unclosed fleet block: nothing changed" > "/dev/stderr"; exit 1}; next}
       {print}' "$f" > "$f.new" ) &&
mv "$f.new" "$f" || rm -f "$f.new"
diff "$f.before-fleet-0.6" "$f"        # what was removed
```

Keep the backup until you have logged in again with a fresh ssh connection. If the
check stops on an unclosed block, open the file in an editor and delete the lines from
`# fleet:…:begin` to the next `# fleet:` line by hand.

To run it on another machine from the one you work on, save it as
`remove-fleet-blocks.sh` and send it: `ssh user@host 'sh -s' < remove-fleet-blocks.sh`.

On Windows, in PowerShell on that machine — as the user for `~\.ssh\authorized_keys`, or
as administrator for the shared file (the `icacls` line keeps sshd reading it):

```powershell
$f = "$env:ProgramData\ssh\administrators_authorized_keys"    # or "$HOME\.ssh\authorized_keys"
Copy-Item -LiteralPath $f "$f.before-fleet-0.6"
$keep = @(); $skip = $false
foreach ($l in Get-Content -LiteralPath $f) {
  if ($l -match '^# fleet:\S+:begin ') { $skip = $true; continue }
  if ($l -match '^# fleet:\S+:end ')   { $skip = $false; continue }
  if ($skip -and $l.StartsWith('# fleet:')) { $skip = $false }
  if (-not $skip) { $keep += $l }
}
Set-Content -LiteralPath $f -Value $keep -Encoding ascii
icacls $f /inheritance:r /grant 'SYSTEM:F' 'Administrators:F'    # administrators file only
```

### The fleet key

Every machine 0.5 ran on or added has a key pair of fleet's own, `id_ed25519` and
`id_ed25519.pub`, in fleet's configuration folder there:

| OS | Folder |
|---|---|
| Linux | `~/.config/fleet` |
| macOS | `~/Library/Application Support/fleet` |
| Windows | `%LOCALAPPDATA%\fleet` |

**Before deleting the one on the machine you work from**, it may still be the key that
gets you into machines that do not have yours yet. Use it once to put your own key on
each of them, while its block is still there:

```bash
ssh-copy-id -i ~/.ssh/id_ed25519.pub -o IdentityFile=~/.config/fleet/id_ed25519 user@host
```

(macOS: `-o "IdentityFile=$HOME/Library/Application Support/fleet/id_ed25519"`.) Then
remove the blocks as above, and the key pair:

```bash
rm ~/.config/fleet/id_ed25519 ~/.config/fleet/id_ed25519.pub
```

Leave the other files in that folder on the machines you watch: `device-id` (on a
machine cloned from another's image) and `machine.yaml` are used by 0.6.

### Files in fleet's folders

On a machine that ran fleet 0.5, these files in its configuration and state folders are
0.5's and unused (`fleet paths` shows where the folders are; on macOS and Windows both
are one folder):

| Folder | 0.5's files |
|---|---|
| configuration | `inventory.yaml`, `access.yaml`, `access.retired.yaml`, `id_ed25519`, `id_ed25519.pub`; from before 0.5, `identity.age`, `secrets.age` |
| state | `access-ledger.yaml`, `access-cache.yaml`, `access-outbox.yaml`, `access-chain.yaml`, `access-handover-in.yaml`, `access-handover-out.yaml`, `access-invites.yaml`, `clock.yaml`, `center-service.log`, `center-service.log.1` |

Beside some of them is a folder `.NAME.queue` (such as `.inventory.yaml.queue`) and in
the state folder `.refresh.queue`; they are 0.5's write queue. Keep `inventory.yaml`
until you have added your machines again, if you want to read their addresses from it.

Deleted by name, never by pattern, since the two folders can be one:

```bash
# Linux; on macOS set both to "$HOME/Library/Application Support/fleet"
cfg=~/.config/fleet; st=~/.local/state/fleet
for f in inventory.yaml access.yaml access.retired.yaml id_ed25519 id_ed25519.pub identity.age secrets.age; do
  rm -rf "$cfg/$f" "$cfg/.$f.queue"; done
for f in access-ledger.yaml access-cache.yaml access-outbox.yaml access-chain.yaml \
         access-handover-in.yaml access-handover-out.yaml access-invites.yaml clock.yaml \
         center-service.log center-service.log.1; do
  rm -rf "$st/$f" "$st/.$f.queue"; done
rm -rf "$st/.refresh.queue"
```

```powershell
$d = "$env:LOCALAPPDATA\fleet"
foreach ($f in 'inventory.yaml','access.yaml','access.retired.yaml','id_ed25519','id_ed25519.pub',
               'identity.age','secrets.age','access-ledger.yaml','access-cache.yaml',
               'access-outbox.yaml','access-chain.yaml','access-handover-in.yaml',
               'access-handover-out.yaml','access-invites.yaml','clock.yaml',
               'center-service.log','center-service.log.1') {
  Remove-Item -LiteralPath "$d\$f", "$d\.$f.queue" -Recurse -Force -ErrorAction SilentlyContinue }
Remove-Item -LiteralPath "$d\.refresh.queue" -Recurse -Force -ErrorAction SilentlyContinue
```

`machines.yaml`, `config.yaml`, `device-id` and `cache.db` are 0.6's: leave them.

### The background service

Only on a machine that was a center, and only if the upgrade did not already remove it.
To check, and remove:

**Linux** — a systemd user unit:

```bash
systemctl --user status fleet-center          # "could not be found" means it is gone
systemctl --user disable --now fleet-center
rm -f ~/.config/systemd/user/fleet-center.service
systemctl --user daemon-reload
```

0.5 also turned on lingering for your user (`loginctl enable-linger`), so the service ran
without a login. If nothing else of yours needs it: `loginctl disable-linger`.

**macOS** — a LaunchAgent:

```bash
launchctl list io.fleet.center                # an error means it is gone
launchctl bootout "gui/$(id -u)" ~/Library/LaunchAgents/io.fleet.center.plist
rm -f ~/Library/LaunchAgents/io.fleet.center.plist
```

**Windows** — a scheduled task, and two firewall rules for port 7373, which the
installer does not remove. In an administrator PowerShell:

```powershell
schtasks /query /tn fleet-center                # "cannot find" means it is gone
schtasks /end /tn fleet-center; schtasks /delete /tn fleet-center /f
netsh advfirewall firewall delete rule name="fleet center"
netsh advfirewall firewall delete rule name="fleet center app"
```

### A cron entry, from before 0.5

Very old installs ran `fleet sync` from cron, marked `# fleet-sync`. `crontab -l | grep
fleet-sync` shows whether one is left; `fleet install` and `fleet update` remove it when
they reinstall fleet on a machine. By hand, reading the table before writing it:

```bash
rest=$(crontab -l | grep -v '# fleet-sync'); printf '%s\n' "$rest" | crontab -
```

## Asking an agent to do it

Everything above can be done by an agent on that machine. Ask, for example:

> **You:** remove what fleet 0.5 left on this machine — follow
> docs/guides/upgrading-from-0.5.md, and show me the `authorized_keys` diff

It should check that your own key works before it removes any block, and keep the
backups.
