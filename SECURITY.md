# Security

fleet logs in to your machines over SSH and reports what is on them to your agents, so a
flaw in it can matter. Thank you for reporting one.

## Reporting a problem

Please report it privately, not in a public issue:
**[open a private security advisory](https://github.com/sinolion/fleet/security/advisories/new)**.

Say what you found, which version (`fleet --version`) and OS, and how to reproduce it if
you can. You will get a reply within a week. Once a fix is released, the advisory is
published with credit to you, unless you would rather not be named.

## Supported versions

Fixes go into the latest release. Upgrade with `fleet update`, or
`uv tool upgrade agents-fleet`.

## What counts

For example:
- fleet writing to a machine anything but what [What fleet changes](docs/guides/what-fleet-changes.md)
  lists;
- a key, a password or a token reaching the agent, a log or a file -- a GPU job's
  command line is shown with anything that looks like a secret replaced, and a way
  round that counts;
- a machine's own `machine.yaml` (which whoever controls that machine writes) getting
  fleet to run something, to read a file it should not, or to pass anything but the
  checked fields on to an agent;
- the probe running something on a machine beyond what `src/fleet/probe/payload.sh`
  (`payload.ps1` on Windows) says it does.

Some things are outside what fleet decides, by design. fleet manages no access: who may
reach which machine is decided by your SSH setup -- your keys, your ssh config, your
Tailscale SSH policy, your rental provider -- and fleet connects exactly as you do. A
machine you can log in to, fleet can log in to, and so can an agent you let run `fleet
ssh`. How to reach each kind of machine, and what fleet will never do, is in
[Reaching your machines](docs/guides/reaching-machines.md).
