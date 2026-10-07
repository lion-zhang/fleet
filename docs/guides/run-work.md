# Running work

Once a machine is in the fleet, your agent can run things on it. You ask for the work;
it picks the machine and connects.

> **You:** run the test suite on the Linux box and tell me what failed

## Commands

```bash
fleet ssh gpu-box -- nvidia-smi          # run one command, print its output
fleet ssh gpu-box                        # an interactive shell, as with plain ssh
fleet ssh a -- 'cd ~/proj && make test'  # by alias; quote anything with && or |
```

`fleet ssh` finds the machine's address and the right key, so the agent needs neither
and never builds an `ssh` command by hand. It works on every
OS: on a Windows machine the command goes to its shell as it is, and the remote
command's exit code comes back in every case.

From the center it reaches every machine. From a member it reaches the machines that
member has been [granted](access.md).

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
longer.

## Code and data

fleet has no copy command. Get code onto a machine the way you would by hand: clone it
there (`fleet ssh gpu-box -- 'git clone …'`), or pull from wherever your data lives.
Machines you have [granted access](access.md) to one another can also copy between
themselves with `scp` or `rsync`.

## When a machine refuses

| What you see | What it means |
|---|---|
| `NAME rejected our key` | The machine is up but does not accept the key. Only the center can put one back: on the center, `fleet add "ssh …"` for it again, or `fleet sync` if this machine was granted access to it and the grant is still pending. |
| `No device named …` | Check the name with `fleet ls`; an alias works too. |
| `NAME has no endpoint recorded` | fleet knows the machine but not how to reach it, e.g. a former center. `fleet edit NAME --ssh "ssh user@host"` on the center. |
