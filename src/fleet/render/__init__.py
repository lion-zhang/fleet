"""Turning fleet's state into something a person or an agent can read.

`view` is THE canonical serializer and `top` draws the terminal widgets. The rule that
governs both is stated in `view`, and it is the reason this package has a boundary at
all: every surface renders through `view`, and downstream layers -- `top`, the MCP
tools, `ls --json` -- may only *subset* what it returns. None may compute a field of its
own. That makes "the dashboard and the agent disagree" a test failure rather than a bug
you find at 2am.

Nothing here reads the network or writes anything. Given rows, it produces text.
"""
