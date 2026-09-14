"""Everything fleet knows about reaching a machine over SSH.

Four modules that were four of the flattest files in the package, and the grouping is
not filing for its own sake -- they are the layer where the two operating systems stop
being interchangeable. `cmd` decides what argv to build and which shell will read it,
`keys` makes and installs the keypair, `authkeys` edits the file at the other end, and
`auth` reads what the server says about itself.

`cmd` has the highest in-degree in fleet: sixteen modules ask it how to reach something.
That is the real core, and it is worth being able to see it as one thing.

**The danger lives here.** `authkeys` rewrites a file that, written wrong, locks every
key out of a machine fleet may be the only route to. Its rules are stated at the top of
that module and they are not style preferences.
"""
