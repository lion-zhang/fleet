"""What fleet remembers, and how long each kind of memory is allowed to last.

Three files, and the line between them is the point of putting them together:

`inventory` is **authored truth** -- a couple of dozen records you edit, diff and sync,
kept as hand-editable YAML so it stays readable when the tool is broken and fixable in
vim. `store` is a **disposable cache**: delete it and the next probe rebuilds everything,
which is why "delete cache.db and re-probe" is always safe advice. Nothing durable may
live there. `access` is neither -- it is **the fleet itself**, the signed record of who
may reach what, and losing it orphans every key fleet has installed with no tooling left
able to remove them.

Each module states its own half of that rule at the top. They sit side by side here
because the distinction matters more in a directory called `state` than it did when they
were three files among twenty-two, not less: the wrong one of the three being treated
like another is how data gets lost.
"""
