"""`python -m fleet`, which exists so the center can serve without a console window.

Windows shows a console for any program built against the console subsystem, and the
`fleet` launcher uv writes is one. `pythonw.exe` is the same interpreter built against
the GUI subsystem, so a scheduled task that runs `pythonw -m fleet center --listen` gets
no window at all -- not a hidden one, and not one that flashes on the way past.

`-m` rather than a second console script: uv generates launchers from the entry points
in pyproject.toml, and asking it for a windowed variant means asking for a packaging
feature it does not have. The interpreter is already there next to the one in use.
"""

from .cli import main

main()
