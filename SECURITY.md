# Security

fleet places SSH keys on your machines, so a flaw in it can matter. Thank you for
reporting one.

## Reporting a problem

Please report it privately, not in a public issue:
**[open a private security advisory](https://github.com/lion-zhang/fleet/security/advisories/new)**.

Say what you found, which version (`fleet --version`) and OS, and how to reproduce it if
you can. You will get a reply within a week. Once a fix is released, the advisory is
published with credit to you, unless you would rather not be named.

## Supported versions

Fixes go into the latest release. Upgrade with `fleet update`, or
`uv tool upgrade agents-fleet`.

## What counts

For example:
- a way for one machine to reach another it was never granted;
- a way for a revoke to report success while the key stays in place;
- a way for anyone but the center to get a key into `authorized_keys`;
- a key or password reaching the agent, a log or a file;
- fleet changing a line in `authorized_keys` it did not write.

Some things are known limits of the design, not flaws: whoever controls the center
controls the fleet, and anyone with root on a machine can add a key back. They are
listed in the [threat model](docs/design/access.md#threat-model-plainly). Everything fleet
writes, and how to remove it, is in
[What fleet changes](docs/guides/what-fleet-changes.md).
