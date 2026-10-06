"""Several agents on one machine change fleet's state at the same moment, and nothing is
lost: the race reproduced before the write queue existed (twelve simultaneous grants, two
kept, two crashed), now a test. See docs/design/layers.md."""

from __future__ import annotations

import multiprocessing as mp
import subprocess
from pathlib import Path

N = 12


def _key(tmp: Path, i: int) -> str:
    k = tmp / f"k{i}"
    subprocess.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-f", str(k)], check=True)
    return k.with_suffix(".pub").read_text().strip()


def _fleet(tmp: Path):
    from fleet.state import access as acl

    path = tmp / "access.yaml"
    acc = acl.bootstrap("center", _key(tmp, 0), path=path)
    fps = [acl.enroll(acc, f"m{i}", _key(tmp, i)) for i in range(1, N + 1)]
    acl.save(acc, path)
    return path, fps


def _grant(path: str, src: str, dst: str, start) -> None:
    from fleet.state import access as acl

    start.wait()
    acl.update(lambda acc: acl.grant(acc, src, dst), Path(path))


def _revoke(path: str, src: str, dst: str, start) -> None:
    from fleet.state import access as acl

    start.wait()
    acl.update(lambda acc: acl.revoke(acc, src, dst), Path(path))


def _run_all(target, argsets):
    ctx = mp.get_context("spawn")
    start = ctx.Event()
    procs = [ctx.Process(target=target, args=(*a, start)) for a in argsets]
    for p in procs:
        p.start()
    start.set()
    for p in procs:
        p.join(120)
        assert p.exitcode == 0, "a writer crashed"


def test_simultaneous_grants_all_land(tmp_path):
    from fleet.state import access as acl

    path, fps = _fleet(tmp_path)
    _run_all(_grant, [(str(path), fps[i], fps[(i + 1) % N]) for i in range(N)])
    assert len(acl.load(path).allow) == N


def test_simultaneous_revokes_all_land(tmp_path):
    """The dangerous half: a lost revoke left the edge in the list, and the next sweep
    put the revoked key back."""
    from fleet.state import access as acl

    path, fps = _fleet(tmp_path)
    acl.update(lambda acc: [acl.grant(acc, fps[i], fps[(i + 1) % N]) for i in range(N)], path)
    _run_all(_revoke, [(str(path), fps[i], fps[(i + 1) % N]) for i in range(N)])
    assert acl.load(path).allow == []


def _note(cache: str, which: int, start) -> None:
    from fleet.state import access as acl

    start.wait()
    for _ in range(10):
        if which == 0:
            acl.note_center_seen(Path(cache))
        elif which == 1:
            acl.note_center_url("http://hub:7373/sync", Path(cache))
        else:
            acl.note_center_unanswered(Path(cache))


def test_a_member_never_forgets_its_center_while_agents_refresh_at_once(tmp_path):
    """Several `fleet ls` on a member each note what the center said. They used to crash
    on a shared temp file, and a torn file read as "no center trusted" -- after which the
    next sync would trust whoever answered."""
    from fleet.state import access as acl

    cache = tmp_path / "access-cache.yaml"
    acl.pin_center_pubkey("ssh-ed25519 AAAA center", cache)
    _run_all(_note, [(str(cache), i % 3) for i in range(9)])
    assert acl.trusted_center_pubkey(cache) == "ssh-ed25519 AAAA center"
    assert acl.center_url(cache) == "http://hub:7373/sync"


def test_two_fleets_are_never_started_at_once(tmp_path):
    """Two agents starting fleet on a fresh machine: one fleet, and the second is told."""
    import pytest

    from fleet.state import access as acl

    path = tmp_path / "access.yaml"
    acl.bootstrap("a", _key(tmp_path, 1), path=path)
    with pytest.raises(acl.AccessError):
        acl.bootstrap("b", _key(tmp_path, 2), path=path)


def test_a_revoke_made_while_an_install_is_in_flight_wins(tmp_path, monkeypatch):
    """An agent grants; while that install is on its way to the machine, another agent
    revokes. Whatever order the two edits reach the machine in, it must end without the
    key -- the list's newest word -- not with the stale install landing last."""
    from fleet import reconcile as rec
    from fleet.ssh.cmd import Endpoint
    from fleet.state import access as acl

    path, fps = _fleet(tmp_path)
    edge = (fps[0], fps[1], "root")
    acl.update(lambda acc: acl.grant(acc, edge[0], edge[1]), path)
    on_machine = []

    def apply_edge(acc, e, ep, *, install, platform="posix"):
        on_machine.append(install)
        if len(on_machine) == 1:            # the revoke arrives during the install
            acl.update(lambda a: acl.revoke(a, e[0], e[1]), path)
        return True, ""

    monkeypatch.setattr(rec, "apply_edge", apply_edge)
    ok, _, installed = rec.converge_edge(acl.load(path), edge, Endpoint(target="m1"),
                                         install=True, access_path=path)
    assert ok and on_machine == [True, False] and installed is False
