# Running work

Once you have added a machine, your agent can run things on it. You ask for the work;
it picks the machine and connects.

> **You:** run the test suite on the Linux box and tell me what failed

## Commands

```bash
fleet ssh gpu-box -- nvidia-smi          # run one command, print its output
fleet ssh gpu-box                        # an interactive shell, as with plain ssh
fleet ssh a -- 'cd ~/proj && make test'  # by alias; quote anything with && or |
```

**Put the remote command in single quotes.** Your own shell reads the command before
fleet does, and expands `$HOME`, `${VAR}` and a leading `~` with *this* machine's values.
PowerShell does it even inside double quotes, so from Windows `fleet ssh gpu-box -- ls
"$HOME"` asks a Linux box about `C:\Users\you`. In single quotes the command reaches the
machine as you typed it, and that machine expands it. fleet warns when a command bound
for a Linux or macOS machine contains your Windows home folder.

`fleet ssh` finds the machine's address, user, port and key file, and runs your own
`ssh` with them, so the agent needs none of them and never builds an `ssh` command by
hand. It connects exactly as `ssh` does from this machine — your ssh config, keys and
agent, Tailscale SSH — and with keys only: it never prompts for a password. It works on
every OS: on a Windows machine the command goes to its shell as it is, and the remote
command's exit code comes back in every case.

## Long jobs

Training runs and other long jobs should not depend on the connection staying open.
Start them detached, with their output in a file:

> **You:** start the training on gpu-box in the background and log to train.log

which the agent turns into something like

```bash
fleet ssh gpu-box -- 'cd ~/proj && nohup python train.py > train.log 2>&1 &'
fleet ssh gpu-box -- 'tail -n 20 ~/proj/train.log'     # later: how is it going
```

A `tmux` or `screen` session works as well. Apps that use fleet through MCP give each
command two minutes, so for them starting detached is the only way to run anything
longer. Past two minutes fleet stops waiting, not the command: one that prints nothing
may still be running there, so check before starting it again.

## Code and data

fleet has no copy command. Get code onto a machine the way you would by hand: clone it
there (`fleet ssh gpu-box -- 'git clone …'`), or pull from wherever your data lives.
`scp` and `rsync` work as they always do, given the machine's address: `fleet show NAME`
prints the ssh command fleet uses under `connect` (`ssh -p 40001 root@1.2.3.4`), and
its user, address and port are what they take (`scp -P 40001 file root@1.2.3.4:`). A
machine you added by a `Host` name from `~/.ssh/config` is reached by that name.

## When a machine refuses

| What you see | What it means |
|---|---|
| `NAME refused us a moment ago` | The machine is up but did not let fleet in on the last reading. `fleet doctor NAME` says why and gives the fix — usually your key on the machine ([more](reaching-machines.md)). |
| `Permission denied (publickey)` | The same, from ssh itself: `fleet doctor NAME`. |
| `No device named …` | Check the name with `fleet ls`; an alias works too. |
| `NAME has no endpoint recorded` | fleet knows the machine but not how to reach it. `fleet edit NAME --ssh "ssh user@host"`. |
| `NAME is this machine` | Run the command here, without `fleet ssh`. |
