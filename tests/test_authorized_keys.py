"""Editing someone else's authorized_keys without locking them out of their machine.

The commands are strings, so they are run here against a real `sh` with a temp HOME --
the same approach test_keys.py already takes. Getting this wrong does not fail a test in
production, it removes the only way into a box.
"""

from __future__ import annotations

import stat
import subprocess
import sys

import pytest

from fleet.authkeys import block, posix_sync_command, powershell_sync_command

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX sh")

FID = "7f3a9c"
SRC = "linux:machine-id:abc"
KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5 fleet:lin-xps"

FOREIGN = "ssh-ed25519 AAAAOTHER someone@laptop"
OTHER_FLEET = ("# fleet:beef99:begin from=linux:machine-id:zzz user=root\n"
               "ssh-ed25519 AAAAOTHERFLEET fleet:beef99:their-box\n"
               "# fleet:beef99:end from=linux:machine-id:zzz")


def run(home, cmd):
    p = subprocess.run(["sh", "-c", cmd], env={"HOME": str(home), "PATH": "/usr/bin:/bin"},
                       capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    return (home / ".ssh" / "authorized_keys").read_text()


def grant(**kw):
    return posix_sync_command(FID, SRC, user="root", pubkey=KEY, **kw)


def revoke(**kw):
    return posix_sync_command(FID, SRC, pubkey=None, **kw)


def test_a_grant_installs_the_key(tmp_path):
    assert KEY in run(tmp_path, grant())


def test_granting_twice_does_not_duplicate(tmp_path):
    run(tmp_path, grant())
    out = run(tmp_path, grant())
    assert out.count(KEY) == 1, "drop-then-append, not append-if-absent"


def test_a_revoke_removes_it_completely(tmp_path):
    run(tmp_path, grant())
    out = run(tmp_path, revoke())
    assert KEY not in out
    assert "fleet:" not in out, "the markers go too, or the next grant nests"


def test_a_key_we_did_not_write_survives(tmp_path):
    """The user's own key, or one a provider injected at boot. Removing it is a lockout,
    and this test must never be deleted."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text(FOREIGN + "\n")
    assert FOREIGN in run(tmp_path, grant())
    assert FOREIGN in run(tmp_path, revoke())


def test_another_fleets_block_survives(tmp_path):
    """Two fleets may manage one machine. Erasing each other's grants would make the
    tool unusable in exactly the setup it claims to support."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text(OTHER_FLEET + "\n")
    assert "beef99" in run(tmp_path, grant())
    out = run(tmp_path, revoke())
    assert "AAAAOTHERFLEET" in out and "beef99" in out


def test_a_block_missing_its_end_marker_does_not_eat_the_rest_of_the_file(tmp_path):
    """Hand-edited, or a write interrupted before the rename. Skipping to EOF here would
    delete every key below ours -- including the user's own."""
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text(
        f"# fleet:{FID}:begin from={SRC} user=root\n{KEY}\n"     # no end marker
        f"{OTHER_FLEET}\n{FOREIGN}\n")
    out = run(tmp_path, revoke())
    assert FOREIGN in out, "the user's own key survives a corrupted block"
    assert "AAAAOTHERFLEET" in out, "and so does another fleet's"


def test_a_file_with_no_block_is_not_truncated(tmp_path):
    ssh = tmp_path / ".ssh"
    ssh.mkdir()
    (ssh / "authorized_keys").write_text(FOREIGN + "\n")
    assert run(tmp_path, revoke()).strip() == FOREIGN


def test_it_works_when_nothing_exists_yet(tmp_path):
    assert KEY in run(tmp_path, grant())


def test_the_ssh_dir_is_not_readable_by_other_users(tmp_path):
    """sshd silently ignores a group-writable ~/.ssh, which looks exactly like a bad key."""
    run(tmp_path, grant())
    assert stat.S_IMODE((tmp_path / ".ssh").stat().st_mode) & 0o077 == 0


def test_the_file_is_never_momentarily_empty(tmp_path):
    """Written beside and renamed, so a dropped connection leaves the old file intact."""
    cmd = grant()
    assert ".fleet." in cmd and 'mv "$t" "$f"' in cmd
    assert f'> "$t"' in cmd, "the rewrite goes to the temp file, never to $f"


def test_a_hostile_key_string_cannot_execute_anything(tmp_path):
    hostile = f"{KEY}'; touch {tmp_path}/pwned; '"
    run(tmp_path, posix_sync_command(FID, SRC, user="root", pubkey=hostile))
    assert not (tmp_path / "pwned").exists()


def test_home_is_expanded_rather_than_taken_literally(tmp_path):
    """Quoting the default path would create a directory named $HOME next to wherever
    the shell happened to start."""
    run(tmp_path, grant())
    assert (tmp_path / ".ssh" / "authorized_keys").exists()


def test_an_explicit_path_is_honoured(tmp_path):
    target = tmp_path / "elsewhere" / "keys"
    cmd = posix_sync_command(FID, SRC, user="root", pubkey=KEY, path=str(target))
    p = subprocess.run(["sh", "-c", cmd], capture_output=True, text=True)
    assert p.returncode == 0, p.stderr
    assert KEY in target.read_text()


# ----------------------------------------------------------------- the Windows twin

def test_windows_resolves_admins_to_the_programdata_file():
    """The trap: Windows OpenSSH has a `Match Group administrators` rule, so appending
    to ~/.ssh/authorized_keys for an admin succeeds and is then ignored by sshd."""
    cmd = powershell_sync_command(FID, SRC, user="lin", pubkey=KEY)
    assert "administrators_authorized_keys" in cmd
    assert "$env:ProgramData" in cmd
    assert "IsInRole" in cmd, "group membership, not an elevation check"
    assert "$env:USERPROFILE" in cmd, "non-admins still use their own profile"


def test_windows_resets_the_acl():
    """sshd refuses a key file writable by anyone but SYSTEM and Administrators, and
    refuses it silently."""
    cmd = powershell_sync_command(FID, SRC, user="lin", pubkey=KEY)
    assert "/inheritance:r" in cmd, "inherited ACEs survive otherwise"
    assert "SYSTEM:F" in cmd and "Administrators:F" in cmd


def test_windows_keeps_foreign_lines_and_renames_into_place():
    cmd = powershell_sync_command(FID, SRC, user="lin", pubkey=KEY)
    assert "StartsWith($b)" in cmd and "StartsWith($e)" in cmd
    assert "Move-Item" in cmd
    assert "beef99" not in cmd


def test_windows_revoke_appends_nothing():
    assert "$keep +=" not in powershell_sync_command(FID, SRC, pubkey=None)


def test_both_shells_agree_on_the_markers():
    """A block written by one and removed by the other must match."""
    b = block(FID, SRC, "root", KEY)
    for cmd in (posix_sync_command(FID, SRC, user="root", pubkey=KEY),
                powershell_sync_command(FID, SRC, user="root", pubkey=KEY)):
        assert f"# fleet:{FID}:begin from={SRC}" in cmd
        assert f"# fleet:{FID}:end from={SRC}" in cmd
    assert b.splitlines()[0].startswith(f"# fleet:{FID}:begin")


def test_windows_appends_one_line_at_a_time():
    """Not one multi-line literal split on newlines. `powershell -Command -` evaluates
    piped input statement by statement, so a literal spanning newlines arrives as
    several broken statements -- and it failed *silently*: the script exited 0 and
    appended nothing, which on a real host means a grant that reports success and never
    works. Found by running this against Windows rather than by reading it."""
    cmd = powershell_sync_command(FID, SRC, user="lin", pubkey=KEY)
    appends = [l for l in cmd.splitlines() if "$keep +=" in l]
    assert len(appends) == 3, "begin marker, key, end marker -- one statement each"
    assert not any("\n" in l for l in appends)
    assert 'Split(' not in cmd


def test_windows_takes_an_explicit_path_for_testing():
    """So the block logic -- the half that can lock someone out -- can be exercised
    against a scratch file. The real run resolves admin vs user, and resolving that
    wrongly is the silent failure the twin exists for, so it keeps the ACL reset."""
    scratch = powershell_sync_command(FID, SRC, user="lin", pubkey=KEY, path=r"C:\tmp\ak")
    assert r"$f='C:\tmp\ak'" in scratch
    assert "icacls" not in scratch, "no ACL reset on a scratch file"

    real = powershell_sync_command(FID, SRC, user="lin", pubkey=KEY)
    assert "administrators_authorized_keys" in real
    assert "icacls" in real
