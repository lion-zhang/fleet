# fleet documentation

fleet lets your coding agent use every machine you have. You install it once, on the
machine you work from, and then talk to your agent; these pages are for when you want to
know more.

**New here?** Start with [Getting started](getting-started.md): one machine added, and a
first job on it, in about ten minutes.

## Guides

Task by task: what to say to your agent, and the command it runs.

| Guide | For when you want to… |
|---|---|
| [Adding machines](guides/add-machines.md) | add a server, a rental, a password-only host, one behind NAT, or one that does not exist yet |
| [Finding the right machine](guides/find-machines.md) | let the agent pick by GPU, memory or OS; tag machines; track costs and idle rentals |
| [Running work](guides/run-work.md) | run commands and long jobs on another machine |
| [Access between machines](guides/access.md) | let one machine reach another, and take it back |
| [The center](guides/center.md) | understand what the center does, what happens when it is off, and move it |
| [Agents](guides/agents.md) | add fleet to a specific agent or app — 35+ of them |
| [Installing and upgrading](guides/install.md) | install by hand, upgrade, put fleet on more machines, remove it |
| [Windows](guides/windows.md) | use Windows machines, as members or as the center |
| [Troubleshooting](guides/troubleshooting.md) | find out what a message means and what to do |

## Reference

Exact and complete; generated from the code where it can be.

- [Commands](reference/cli.md) — every command and option.
- [Configuration](reference/configuration.md) — files, settings and environment variables.
- [Facts, tags and alerts](reference/facts.md) — what fleet measures, and when it warns.

## Design

Why fleet works the way it does.

- [Core, skill and MCP](design/layers.md) — one `fleet` per machine, shared by every agent.
- [Access](design/access.md) — how keys are placed, signed, handed over and revoked; the
  threat model.
- [Sync](design/sync.md) — how machines share the inventory, and why the center is allowed
  to be offline.

## Contributing

[CONTRIBUTING.md](../CONTRIBUTING.md) covers running the tests, the end-to-end checks on
each OS, and the files that are generated rather than written.
