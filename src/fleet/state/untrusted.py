"""YAML that came from another machine, read without letting it cost what it likes.

The center reads a request's YAML before it knows who sent it -- to find out who claims
to have sent it. Read with PyYAML's pure-Python loader, a 2 MB body cost 28 s of CPU,
and a few hundred bytes of nested aliases (`a1: &a1 [*a0, *a0, ...]`) expand ninefold
per level when a value is turned into a string: minutes and gigabytes, from anyone who
can reach the port, while the interpreter lock stalls every other request.

So: the C loader where it exists, and no anchors or aliases at all -- nothing fleet
writes uses them (`dump` below never emits them), so refusing them refuses only
payloads built to abuse them.
"""

from __future__ import annotations

import yaml

_Loader = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
_BaseDumper = getattr(yaml, "CSafeDumper", yaml.SafeDumper)


class _NoAliasDumper(_BaseDumper):  # type: ignore[misc,valid-type]
    def ignore_aliases(self, data):  # noqa: D401 - PyYAML's hook
        return True


def load(text: str):
    """Parse YAML from the network. Raises yaml.YAMLError on anchors or aliases."""
    for token in yaml.scan(text, Loader=_Loader):
        if isinstance(token, (yaml.AliasToken, yaml.AnchorToken)):
            raise yaml.YAMLError("anchors and aliases are not accepted")
    return yaml.load(text, Loader=_Loader)


def dump(data, **kw) -> str:
    """`yaml.safe_dump`, never writing an anchor -- so `load` reads it back."""
    return yaml.dump(data, Dumper=_NoAliasDumper, **kw)
