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
| [Adding machines](guides/add-machines.md) | add a server, a rental, one from your ssh config or tailnet; names, tags, clones, removing |
| [Reaching your machines](guides/reaching-machines.md) | let fleet in: Tailscale SSH, Windows and Mac, rentals, LAN; what to do when a machine refuses |
| [Finding the right machine](guides/find-machines.md) | let the agent pick by GPU, memory or OS; tag machines; track costs and idle rentals |
| [What is on each machine](guides/resources.md) | see GPU jobs, served models, installed software, and where models and data live |
| [Running work](guides/run-work.md) | run commands and long jobs on another machine |
| [Waking machines](guides/wake.md) | wake a sleeping or switched-off machine, from home or away |
| [Agents](guides/agents.md) | add fleet to a specific agent or app — 35+ of them |
| [What fleet changes](guides/what-fleet-changes.md) | see exactly what fleet writes on each machine, and take it back |
| [Installing and upgrading](guides/install.md) | install by hand, upgrade, put fleet on more machines, remove it |
| [Upgrading from 0.5](guides/upgrading-from-0.5.md) | what changed in 0.6, and removing what 0.5 left on your machines |
| [Windows](guides/windows.md) | watch Windows machines, or run fleet on Windows |
| [Troubleshooting](guides/troubleshooting.md) | find out what a message means and what to do |

## Reference

Exact and complete; generated from the code where it can be.

- [Commands](reference/cli.md) — every command and option.
- [Configuration](reference/configuration.md) — files, settings and environment variables.
- [Facts, tags and alerts](reference/facts.md) — what fleet measures, and when it warns.

## Design

Why fleet works the way it does.

- [How fleet works](design/how-it-works.md) — what happens, step by step, when you install,
  add a machine, measure, diagnose a refusal and run a job; and which code does it.
- [Core, skill and MCP](design/layers.md) — one `fleet` per machine, shared by every agent.

## Contributing

[CONTRIBUTING.md](https://github.com/sinolion/fleet/blob/main/CONTRIBUTING.md) covers running the tests, the end-to-end checks on
each OS, and the files that are generated rather than written.
