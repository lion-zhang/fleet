# fleet

Personal compute inventory, service registry, and resource broker.

Answers, for you and for any coding agent: what machines do I have, which are online,
what are their resources, what is free right now, what is running on them, what services
do they serve, what is costing money, and how do I connect?

Onboarding a machine costs one pasted SSH command:

```bash
fleet add "ssh -p 58418 root@1.2.3.4"
fleet ls
```

Probe targets need **nothing installed** -- the probe is a POSIX `sh` script piped over
one SSH connection.

```bash
fleet edit blackwell --ssh "ssh -p 40001 root@1.2.3.4"   # rentals recycle addresses
fleet edit lin-xps --disk-path /workspace                # watch the volume that matters
fleet center --init                                      # this machine decides who reaches what
fleet center --enroll ds720                              # password typed once, then key auth
fleet access oracle --allow lin-xps                      # the center installs the key
fleet top                                                # live view, like htop for the fleet
fleet setup                                              # teach your coding agents to use it
```

Status: v0.4 -- inventory, probe, CLI, live `top`, agent setup (Claude Code,
Codex, Hermes), and a center that installs SSH keys rather than keeping passwords.
Nothing is stored that could be stolen: a password, where one is needed at all, is
typed once and spent on a single connection. See `docs/design/access.md`.
