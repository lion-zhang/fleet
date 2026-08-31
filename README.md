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

See `docs/` for the design. Status: v0.1 (core inventory + probe + CLI).
