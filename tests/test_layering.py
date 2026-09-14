"""The dependency rules, asserted rather than remembered.

Every one of these held when it was written and none of them is self-enforcing. The
reason to spend a test on each is that violating one costs nothing at the time and is
expensive later: fleet already had a cli <-> serve cycle, arrived at by one function
reaching for a helper that happened to live in the argument parser.
"""

from __future__ import annotations

import ast
import pathlib

import pytest

SRC = pathlib.Path(__file__).resolve().parent.parent / "src" / "fleet"
SURFACES = {"cli", "serve", "mcpserver"}


def _modules():
    for path in sorted(SRC.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path, ast.parse(path.read_text())


def _fleet_imports(tree, path):
    """Every fleet module this one imports, as dotted names relative to `fleet`."""
    # Kept with the trailing "__init__" so the arithmetic below is uniform: inside a
    # package's __init__, `from .x` means the package itself, and inside a module it
    # means the module's parent. Both are "drop `level` components".
    here = path.relative_to(SRC).with_suffix("").parts
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.level:
            base = list(here[: len(here) - node.level])
            mod = (node.module or "").split(".") if node.module else []
            out.add(".".join(base + mod).strip("."))
            for alias in node.names:
                out.add(".".join(base + mod + [alias.name]).strip("."))
        elif isinstance(node, ast.ImportFrom) and (node.module or "").startswith("fleet"):
            out.add(node.module[len("fleet."):])
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("fleet."):
                    out.add(alias.name[len("fleet."):])
    return {m for m in out if m}


def test_nothing_imports_the_argument_parser():
    """cli.py is a surface. A module reaching into it is the shape of the cycle fleet
    already had -- serve.py importing two helpers that happened to live there."""
    for path, tree in _modules():
        if path.name == "cli.py":
            continue
        imports = _fleet_imports(tree, path)
        assert not {i for i in imports if i == "cli" or i.startswith("cli.")}, \
            f"{path.name} imports cli"


def test_operations_do_not_import_a_surface():
    """ops/ exists so an operation can be called by the CLI, the HTTP center and MCP
    alike. One that imports a surface can only be called from that surface."""
    for path, tree in _modules():
        if path.parent.name != "ops":
            continue
        bad = {i for i in _fleet_imports(tree, path) if i.split(".")[0] in SURFACES}
        assert not bad, f"ops/{path.name} imports {sorted(bad)}"


def test_operations_do_not_import_typer():
    """`typer.Exit` carries an exit code, which is a fact about a terminal. An operation
    that raises one cannot be called from the HTTP center or from MCP -- which is the
    whole reason FleetError exists."""
    for path, tree in _modules():
        if path.parent.name != "ops":
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] == "typer" for a in node.names), path
            if isinstance(node, ast.ImportFrom):
                assert (node.module or "").split(".")[0] != "typer", path


def test_ui_is_a_leaf():
    """Operations print as they go, so almost everything imports ui. It may import
    nothing from fleet in return, or that convenience becomes a cycle."""
    tree = ast.parse((SRC / "ui.py").read_text())
    assert not _fleet_imports(tree, SRC / "ui.py")


def test_the_package_has_no_import_cycles():
    graph = {}
    for path, tree in _modules():
        name = ".".join(path.relative_to(SRC).with_suffix("").parts)
        name = name[: -len(".__init__")] if name.endswith("__init__") else name
        graph[name] = _fleet_imports(tree, path)

    known = set(graph)
    state = {}
    stack = []

    def walk(node):
        if state.get(node) == "done":
            return
        if state.get(node) == "open":
            cycle = stack[stack.index(node):] + [node]
            pytest.fail("import cycle: " + " -> ".join(cycle))
        state[node] = "open"
        stack.append(node)
        for dep in sorted(graph.get(node, ())):
            if dep in known:
                walk(dep)
            elif dep.rsplit(".", 1)[0] in known:   # `from .x import name`
                walk(dep.rsplit(".", 1)[0])
        stack.pop()
        state[node] = "done"

    for node in sorted(graph):
        walk(node)
