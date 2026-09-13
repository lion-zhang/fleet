# fleet

Personal compute inventory, service registry, and resource broker.

Answers, for you and for any coding agent: what machines do I have, which are online,
what are their resources, what is free right now, what is running on them, what services
do they serve, what is costing money, and how do I connect?

**New here? Start with [docs/getting-started.md](docs/getting-started.md).**

A fleet is created by one machine, and that machine is the center: the one that decides
who may reach what, and the only one that installs or removes keys.

```bash
fleet center --init                         # this machine is now the center
fleet add "ssh -p 58418 root@1.2.3.4" --alias a   # probe, enrol, pin, record -- one step
fleet setup                                 # teach your coding agents to use it
```

After that you mostly talk to your agents, not to fleet.

```bash
fleet access machine_A --allow machine_B    # who may reach what; then `fleet sync`
fleet ls                                    # what is free right now
fleet ls --tag cuda --tag vram-24g          # by capability, not by remembering names
fleet top                                   # live view, like htop for the fleet
fleet edit machine_A --ssh "ssh -p 40001 root@1.2.3.4"   # rentals recycle addresses
fleet edit machine_B --disk-path /workspace              # watch the volume that matters
fleet update --all                          # deploy the newest fleet everywhere
```

Machines you only connect *to* need **nothing installed** -- the probe is one script
piped over one SSH connection, POSIX `sh` or PowerShell depending on what answers.

Status: v0.4 -- inventory, probe, CLI, live `top`, agent setup (Claude Code,
Codex, Hermes), and a center that installs SSH keys rather than keeping passwords.
Nothing is stored that could be stolen: a password, where one is needed at all, is
typed once and spent on a single connection. See `docs/design/access.md`.
