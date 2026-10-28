# Reaching your machines

**fleet manages no access.** It runs `ssh` exactly as you would — with your
`~/.ssh/config`, your ssh agent, your own keys, Tailscale SSH, the key on your rental
provider's account — and nothing else. It has no key of its own, writes no
`authorized_keys`, and keeps no list of who may reach what.

So the rule is simple: **a machine you can `ssh` into from here, fleet can watch from
here.** One you cannot, it cannot either, and the fix is the one you would make for
`ssh` anyway. This page is that fix, for each kind of machine.

Why it works this way: what lets someone into a machine is decided by the people who own
it — in its `authorized_keys`, in a tailnet policy, on a provider's account page. Those
are already the places you manage access. A second system beside them, one more copy of
"who may log in where", would only be one more thing to get wrong.

| The machine | What lets you in | Section |
|---|---|---|
| Linux, on your tailnet | your tailnet's SSH policy | [Linux on a tailnet](#linux-on-a-tailnet) |
| Windows, or a Mac | your key in its `authorized_keys`, once | [Windows or Mac](#windows-or-mac) |
| a GPU rental | the key on your provider account | [Rentals](#rentals) |
| on your LAN, or another VPN | your key, and a `Host` in `~/.ssh/config` if you like | [LAN or another VPN](#lan-or-another-vpn) |

Every machine running fleet is equal: each one looks at the machines *its own* ssh
reaches. There is no center to set up, and nothing is synced between them.

## Linux on a tailnet

With [Tailscale SSH](https://tailscale.com/kb/1193/tailscale-ssh) turned on for a
machine (`tailscale set --ssh` on it), Tailscale answers ssh there itself and decides who
may log in from your tailnet's policy — not from `authorized_keys`. There are no keys to
copy: if the policy lets you in, `ssh you@gpu-box` works, and so does fleet.

```bash
fleet ls --discover        # your tailnet's machines, among others
fleet add gpu-box          # by the name it lists
```

If it refuses, the policy is where to look: in the Tailscale admin console, under
*Access controls*, add an `ssh` rule for you and that machine. `fleet doctor gpu-box`
prints one to start from, such as:

```json
{"action": "accept", "src": ["autogroup:member"], "dst": ["autogroup:self"], "users": ["ubuntu"]}
```

**Use `accept`, not `check`, for machines your agents use.** A `check` rule makes you
sign in again in a browser every so often before ssh lets you through. You can do that;
an agent running fleet with no browser and no terminal never can, so to it the machine
simply refuses. `fleet doctor` recognises this and says so.

A discovered tailnet machine is added as `ssh NAME`, which logs in as your local user
name. If the account there is a different one, give it: `fleet add "ssh ubuntu@gpu-box"`.

## Windows or Mac

Tailscale's SSH server is not available on Windows, nor in the Mac app from the App
Store or Tailscale's own Mac app. Such machines answer ssh with their own OpenSSH server,
which needs your key in its `authorized_keys`. The tailnet still carries the connection:
you add them by their tailnet name or address, as any other machine.

Turn the server on first, if it is not:

| OS | How |
|---|---|
| macOS | System Settings → General → Sharing → **Remote Login** |
| Windows | in an administrator PowerShell: `Add-WindowsCapability -Online -Name OpenSSH.Server~~~~0.0.1.0; Start-Service sshd; Set-Service sshd -StartupType Automatic` |

Then put your public key on it, once. `fleet doctor NAME` prints these lines with your
key already in them: the `.pub` beside the key file the machine was added with (`-i`),
or else `~/.ssh/id_ed25519.pub`, `id_ecdsa.pub`, `id_rsa.pub` or any other `.pub` there. If
you have no key yet, `ssh-keygen -t ed25519` makes one.

**On a Mac** (or Linux), in a terminal on that machine:

```bash
umask 077; mkdir -p ~/.ssh && printf '%s\n' 'ssh-ed25519 AAAA... you@laptop' >> ~/.ssh/authorized_keys
```

or, from the machine you work on, typing that machine's password once:
`ssh-copy-id you@mac-mini`.

**On Windows**, in PowerShell on that machine, as the user you will log in as:

```powershell
New-Item -ItemType Directory -Force "$HOME\.ssh" | Out-Null; Add-Content -LiteralPath "$HOME\.ssh\authorized_keys" -Value 'ssh-ed25519 AAAA... you@laptop'
```

**If that user is an administrator** — most Windows logins are — Windows' sshd ignores
the file above and reads one shared file instead. In an administrator PowerShell:

```powershell
$f="$env:ProgramData\ssh\administrators_authorized_keys"; Add-Content -LiteralPath $f -Value 'ssh-ed25519 AAAA... you@laptop'; icacls $f /inheritance:r /grant 'SYSTEM:F' 'Administrators:F'
```

The `icacls` part matters: sshd silently ignores that file unless only SYSTEM and
Administrators can write it. Both mistakes — the wrong file, or the right file with the
wrong permissions — look the same from outside: the key is there and does not work. More
in [Windows](windows.md).

## Rentals

vast.ai, RunPod, AutoDL and the like put the public key from **your account** on every
instance they start. So add your key there once — in the provider's settings, under SSH
keys — and every rental you start afterwards lets you in:

| Provider | Where |
|---|---|
| vast.ai | Account → SSH Keys; then recreate the instance, or add it on the instance |
| RunPod | Settings → SSH Public Keys; pods started after that accept it |
| AutoDL | 控制台 → 设置 → SSH 公钥, or add it on the instance |

Then add the rental with the ssh command the provider shows for it:

```bash
fleet add "ssh -p 40001 root@1.2.3.4"
```

An instance started before you added the key does not have it. Put it on that one by hand
(the Linux line in [Windows or Mac](#windows-or-mac), run on the instance), or start a new
one. When fleet recognises the provider, `fleet doctor` names its page.
More about rentals — cost, idle alerts, moving addresses — is in
[Adding machines](add-machines.md#rentals).

## LAN or another VPN

A machine at home or in the office, or on ZeroTier, WireGuard, Netbird or a company VPN,
is reached as `ssh` reaches it. Put your key on it once (the lines above, or
`ssh-copy-id user@host`), and fleet adds it by any ssh command that works:

```bash
fleet add "ssh ubuntu@192.168.1.50"
```

A `Host` entry in `~/.ssh/config` is the tidy way to remember a user, a port, a key or a
jump host, and fleet reads it just as ssh does:

```text
Host nas
    HostName 192.168.1.50
    User admin
    IdentityFile ~/.ssh/id_ed25519
```

```bash
fleet add nas
```

fleet does not create routes. Machines behind different NATs need a network between them
first — a tailnet or another VPN — and then an address fleet can route to.

## Finding what you can already reach

```bash
fleet ls --discover          # or ask your agent: "what machines could I add?"
```

lists the machines you can probably reach but have not added yet, from two places:

- the `Host` entries in your `~/.ssh/config`, following `Include` (patterns such as
  `Host *` and `localhost` are skipped: they name no one machine);
- the machines on your tailnet, from `tailscale status --json` — only those running
  Linux, macOS, Windows or FreeBSD, since a phone has no sshd. It needs Tailscale
  running here, and no API token.

Each row says how to add it: `fleet add NAME`. Nothing listed is contacted until you add
it, and nothing is added by itself: a tailnet has phones and printers on it, and an ssh
config has hosts you have not used in years. An agent is told to offer, not to add them
all.

## When a machine refuses

`fleet add` records a machine only if it lets fleet in. If it does not, nothing is
recorded, and fleet prints **why** and the **fix**, then asks you to run the same
`fleet add` again once it lets you in. For a machine already added that stops letting
fleet in, `fleet ls` shows it as `auth_failed` — up, but refusing, which is not the same
as off.

```bash
fleet doctor gpu-box         # one machine, looked at now
fleet doctor                 # every machine whose last reading failed
fleet doctor gpu-box --json  # `why` and `fix`, for an agent
```

`fleet doctor` connects once, listens to what the server says it is, and names the one
thing to do:

| What it finds | The fix it gives |
|---|---|
| your key is not accepted | the line that puts your key on it — for Linux and macOS, for a Windows user, and for a Windows administrator — or `ssh-copy-id` |
| you have no key at all | `ssh-keygen -t ed25519`, then run `fleet doctor` again |
| a rental without your key | the provider's account page for keys |
| Tailscale SSH does not allow you | a policy rule to add, or a user it does allow |
| Tailscale SSH wants a browser sign-in (`check`) | make the rule `accept`, or sign in once |
| ssh offered too many keys and the server gave up | name the key: `fleet edit NAME --ssh "ssh ... -i ~/.ssh/THE_KEY"` |
| nothing listens on the ssh port | how to start sshd on Linux, macOS and Windows |
| the host key changed | `ssh-keygen -R HOST`, or `ssh-keygen -R '[HOST]:PORT'` on a port other than 22, once you know why it changed |
| nothing answered | check it is on and connected; `fleet edit NAME --ssh` if it moved |

It **changes nothing**: it only tells you. Applying the fix is up to you, because it is
a change to who may log in to that machine.

## Putting your key on it from here

At a terminal on Linux or macOS, when a machine refuses your key, `fleet add` offers to
run `ssh-copy-id` for you:

```text
gpu-box does not accept your key yet.
Put your key on it with `-p 2222 ubuntu@10.0.0.7`? ssh-copy-id asks for its password once [Y/n]:
```

`ssh-copy-id` is the standard OpenSSH tool. It asks for the machine's password itself,
in your terminal, and appends your normal public key to its `authorized_keys`. fleet
only starts it and then tries again; it never sees what you type. It is not offered to an
agent (there is no one to type a password), with `--json` or `--dry-run`, when you have
no key yet, or where there is no `ssh-copy-id` — on Windows, `fleet doctor` prints the
line to run instead.

## Passwords

**fleet never asks for a password, and never stores one.** It runs ssh in batch mode, so
ssh does not prompt either: a machine that only takes a password simply refuses. The one
time a password is typed is into `ssh-copy-id`, by you, to put your key on the machine —
after that, the key is what lets you in.

An agent is told never to type a password or accept one from you. If it needs a machine
that refuses, it runs `fleet doctor` and gives you the fix.

## Each machine keeps its own list

The machines you add by hand are kept on the machine you added them from, in
`machines.yaml` in fleet's configuration folder (`fleet paths` shows where). A second
machine running fleet has its own list, of the machines *its* ssh reaches.

To use the same list on several machines, share the file the way you share your other
settings — in your dotfiles, for example. It holds names, ssh commands, tags and costs,
and no keys or passwords. Each machine still connects with its own ssh, so a machine
that cannot reach one in the list shows it as not answering, and nothing worse.

What a machine says *about itself* — where it keeps models, its tags, its cost — lives on
that machine, in its `machine.yaml`, so every fleet that looks at it sees the same. See
[What is on each machine](resources.md#machineyaml).
