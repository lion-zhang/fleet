# What fleet changes on your machines

fleet edits `authorized_keys`, so it is fair to ask exactly what it touches before you
let it near your machines. This page is the whole list: what it writes on each kind of
machine, what it sends over the network, how to check for yourself, and how to take all
of it back, with fleet or without it.

## In short

| Machine | What fleet puts there |
|---|---|
| **center** (the first install) | the `fleet` program; its own folder; a background service; a skill or MCP entry in each of your agents; marked blocks in `authorized_keys` only if you grant another machine access to it |
| **member** (another machine that runs fleet) | the same, minus the service; plus the center's key, in a marked block in `authorized_keys` |
| **a machine you add** (nothing installed) | a marked block in `authorized_keys`, and a key pair in a `fleet` folder. No program, no service, nothing running. |

And on the network: the center opens SSH connections to your machines, and listens on
TCP port 7373 so members can refresh. fleet never contacts anything else by itself: no
telemetry, no account, no update check.

## On the center

- **The program.** Installed with [uv](https://docs.astral.sh/uv) as `agents-fleet`
  (`uv tool list` shows it). The installer installs uv first if it is missing, and lets
  uv add its folder of tools to your `PATH`.
- **Its folder.** `~/.config/fleet` and `~/.local/state/fleet` on Linux,
  `~/Library/Application Support/fleet` on macOS, `%LOCALAPPDATA%\fleet` on Windows. In
  it: the list of machines, this machine's fleet key, the access list, and a cache of
  measurements. Readable by you alone. Every file is listed in
  [Configuration](../reference/configuration.md#files).
- **A background service**, run as you, never as root: a systemd user unit on Linux
  (with lingering on, so it runs without a login), a LaunchAgent on macOS, a scheduled
  task plus a firewall rule for port 7373 on Windows. Names and paths are in
  [Configuration](../reference/configuration.md#the-background-service).
- **Your agents.** A skill file (for example `~/.claude/skills/fleet/SKILL.md`), or an
  entry in an app's MCP settings. Only for agents found on this machine; `fleet setup`
  prints each file it writes, and keeps the rest of every settings file as it was.

## On a member

The same program, folder and agent entries as the center, but no background service: a
member runs fleet only when you or an agent use it. Joining also adds the center's key
to this machine's `authorized_keys`, in a block marked with the fleet's id. That is how
the center keeps the member's copy of the fleet current and applies access changes there.

## On a machine you add

fleet installs nothing on it, and nothing keeps running. It writes:

- **Marked blocks in `authorized_keys`**: `~/.ssh/authorized_keys`, or for a Windows
  administrator `C:\ProgramData\ssh\administrators_authorized_keys`. One block holds the
  center's key; another appears for each machine you grant access. Each looks like this:

  ```
  # fleet:7f3a9c:begin from=SHA256:... user=root
  ssh-ed25519 AAAA... fleet:7f3a9c:laptop
  # fleet:7f3a9c:end from=SHA256:...
  ```

  fleet only changes lines inside blocks marked with its own fleet's id. Your keys, your
  provider's and another fleet's block are never changed. The file is written beside the
  old one and then renamed, so a dropped connection leaves the old file in place.
- **A key pair for the machine itself**, `id_ed25519` and `id_ed25519.pub`, in
  `~/.config/fleet` (`~/Library/Application Support/fleet` on macOS,
  `%LOCALAPPDATA%\fleet` on Windows). Created once, if the machine has none. It is the
  key used when you grant this machine access to another one, and it reaches nothing
  until you do.
- **`device-id`** in that folder, and only on a machine cloned from another's image, so
  fleet can tell the two apart.

Measuring a machine runs one script over one SSH connection. The script reads what the
machine has and what is free, and writes nothing except a temporary copy of itself,
which it deletes when it finishes.

## On the network

| Connection | Why |
|---|---|
| center → every machine, over SSH | to measure it, and to place or remove keys |
| member → center, TCP 7373 | to refresh its copy of the fleet, and to join with an invite |
| granted machine → the machine it was granted, over SSH | only when you use the access you granted |

The center's port 7373 tells anyone who asks `{"service": "fleet"}` and nothing more.
Every other request must be signed by a machine whose key the center has pinned, or
bring an invite, which lasts minutes and works once. fleet uses only the
network routes you already have: it opens no tunnel and asks no third party to relay
anything.

fleet reaches the internet only when you install or update it: from GitHub, from PyPI,
and from astral.sh for uv.

## What fleet never does

- **Store a password.** A password you type for a machine that accepts nothing else is
  used for one connection, then forgotten.
- **Show a key or a password to your agent.** The agent runs `fleet ssh NAME`. It may
  see a machine's address.
- **Run as root**, or ask for sudo.
- **Touch a key it did not put there**, or another fleet's.
- **Delete files by pattern.** `fleet uninstall --purge` deletes fleet's files by name,
  so a folder you pointed fleet at keeps everything else in it.

## Check it yourself

On any machine, with or without fleet:

```bash
grep -n '^# fleet:' ~/.ssh/authorized_keys     # every fleet block, with the fleet id and user
ls ~/.config/fleet                              # macOS: ~/Library/Application\ Support/fleet
```

On the center:

```bash
fleet access          # who may reach what, and whether each change has landed yet
fleet paths           # where fleet keeps its files on this machine
fleet center          # the fleet id, the machines in it, and whether it is serving
```

## Take it back

| To… | Run, on the center |
|---|---|
| remove one machine | `fleet rm NAME`: its blocks come off it, and its key comes off every other machine |
| take back one grant | `fleet access TARGET --deny MACHINE` |
| end the whole fleet | `fleet center --dissolve`: every block comes off every machine |

To take fleet off a machine that runs it, run `fleet uninstall` there (`--purge` deletes
its files too). See [Removing fleet](install.md#removing-fleet).

`fleet rm` leaves the machine's own key pair in place, because another fleet may use
the same key on that machine. Once the machine is removed, that key opens nothing. To
delete it as well, remove the `fleet` folder on that machine.

**By hand, without fleet**, for example on a machine the center can no longer reach.
This deletes every fleet block and keeps a backup in `authorized_keys.bak`:

```bash
sed -i.bak '/^# fleet:[0-9a-f]*:begin /,/^# fleet:[0-9a-f]*:end /d' ~/.ssh/authorized_keys
```

To remove only one fleet's blocks, put its id, such as `7f3a9c`, in place of
`[0-9a-f]*`. On Windows, delete the lines from `# fleet:…:begin` to `# fleet:…:end`
in the file named above, in any text editor.

## What this does not protect against

The center holds the one key that reaches every machine, so whoever controls the center
controls the fleet. Revoking a key does not undo what was done with it before. And
anyone with root on a machine can add a key back. All of it is spelled out in the
[threat model](../design/access.md#threat-model-plainly).

Found a security problem? Please report it privately; see
[SECURITY.md](../../SECURITY.md).
