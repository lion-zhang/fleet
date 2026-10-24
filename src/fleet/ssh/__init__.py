"""Everything fleet knows about reaching a machine over SSH.

The layer where the operating systems stop being interchangeable. `cmd` decides what
argv to build and which shell will read it; `keys` says how to put your key on a machine
that does not take it yet. fleet writes no keys itself: it connects as you do.
"""
