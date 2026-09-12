"""`fleet setup` writes into files fleet does not own.

The whole risk of this command is that it edits things outside the repo -- a global
CLAUDE-adjacent skill directory, and an AGENTS.md that may already carry the user's own
instructions. So the tests that matter are the ones proving we never clobber content we
did not write, and that running setup twice is not different from running it once.
"""

from __future__ import annotations

from fleet.setup import (
    BEGIN,
    END,
    agents_block,
    apply_block,
    detect_targets,
    fleet_command,
    hermes_skill_text,
    install,
    plan,
    remove_block,
    skill_text,
    uninstall,
)


# --------------------------------------------------------------- marker handling

def test_apply_block_appends_when_no_markers_present():
    existing = "# My own notes\n\nAlways use tabs.\n"
    out = apply_block(existing, "hello")
    assert existing in out, "user content must survive verbatim"
    assert BEGIN in out and END in out


def test_apply_block_replaces_between_markers_instead_of_appending():
    """The second run must not leave two copies of the block."""
    first = apply_block("# Mine\n", "version one")
    second = apply_block(first, "version two")
    assert second.count(BEGIN) == 1
    assert second.count(END) == 1
    assert "version one" not in second
    assert "version two" in second


def test_apply_block_is_idempotent_byte_for_byte():
    once = apply_block("# Mine\n", "body")
    twice = apply_block(once, "body")
    assert once == twice


def test_apply_block_preserves_content_written_after_the_block():
    """A user may append their own notes below our block; those must not be eaten."""
    seeded = apply_block("# Top\n", "ours") + "\n# Bottom notes\nmine, keep me\n"
    out = apply_block(seeded, "ours v2")
    assert "# Top" in out
    assert "# Bottom notes\nmine, keep me" in out
    assert "ours v2" in out
    assert out.count(BEGIN) == 1


def test_remove_block_leaves_only_foreign_content():
    seeded = apply_block("# Mine\n\nkeep this\n", "ours")
    out = remove_block(seeded)
    assert "ours" not in out
    assert BEGIN not in out and END not in out
    assert "keep this" in out


def test_remove_block_on_a_file_we_never_touched_is_a_no_op():
    existing = "# Someone else's file\n"
    assert remove_block(existing) == existing


# --------------------------------------------------------------- generated text

def test_skill_text_has_parseable_frontmatter_with_name_and_description():
    """Claude Code only loads a skill it can parse; a malformed header is a silent
    no-op, which is the worst possible failure for a setup command."""
    text = skill_text("fleet")
    assert text.startswith("---\n")
    _, frontmatter, _ = text.split("---\n", 2)
    assert "name: fleet" in frontmatter
    assert "description:" in frontmatter


def test_generated_text_embeds_the_resolved_command():
    """When fleet is not on PATH the skill must name the absolute binary, or every
    command it recommends fails with 'command not found'."""
    text = skill_text("/opt/fleet/bin/fleet")
    assert "/opt/fleet/bin/fleet ls --json" in text
    assert agents_block("/opt/fleet/bin/fleet").count("/opt/fleet/bin/fleet") >= 1


def test_generated_text_tells_the_agent_not_to_hand_build_ssh():
    """The credential boundary in view.connect_view only holds if agents route through
    `fleet ssh` instead of reconstructing an ssh invocation themselves."""
    assert "fleet ssh" in skill_text("fleet")


# --------------------------------------------------------------- command resolution

def _path_with(*dirs) -> str:
    import os
    return os.pathsep.join(str(d) for d in dirs)


def _fake_binary(d):
    d.mkdir(parents=True, exist_ok=True)
    exe = d / "fleet"
    exe.write_text("#!/bin/sh\n")
    exe.chmod(0o755)
    return exe


def test_fleet_command_uses_the_bare_name_when_a_real_install_is_on_path(tmp_path, monkeypatch):
    real = _fake_binary(tmp_path / "bin").parent
    monkeypatch.setenv("PATH", _path_with(real))
    assert fleet_command() == "fleet"


def test_fleet_command_rejects_a_binary_that_only_exists_in_the_active_venv(monkeypatch):
    """`which` finds the venv copy while that venv is active, but an agent shell without
    it activated gets 'command not found'."""
    import sys
    from pathlib import Path

    monkeypatch.setenv("PATH", _path_with(Path(sys.prefix) / "bin"))
    assert fleet_command().startswith("/")
    assert fleet_command() != "fleet"


def test_fleet_command_looks_past_a_venv_that_shadows_a_real_install(tmp_path, monkeypatch):
    """An active venv sorts first on PATH. Giving up at the first hit would name an
    absolute venv path even though the bare name works fine in any shell."""
    import sys
    from pathlib import Path

    real = _fake_binary(tmp_path / "bin").parent
    monkeypatch.setenv("PATH", _path_with(Path(sys.prefix) / "bin", real))
    assert fleet_command() == "fleet"


def test_fleet_command_falls_back_to_an_absolute_path_when_nothing_is_on_path(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    resolved = fleet_command()
    assert resolved.startswith("/"), "fallback must be absolute to be runnable anywhere"


# --------------------------------------------------------------- target detection

def test_detect_targets_skips_agents_that_are_not_installed(tmp_path):
    (tmp_path / ".claude").mkdir()
    assert detect_targets(tmp_path) == ["claude"]


def test_detect_targets_finds_both_when_both_are_present(tmp_path):
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".codex").mkdir()
    assert detect_targets(tmp_path) == ["claude", "codex"]


# --------------------------------------------------------------- install

def test_install_writes_a_skill_file_carrying_the_resolved_command(tmp_path):
    (tmp_path / ".claude").mkdir()
    install(tmp_path, ["claude"], "/opt/bin/fleet")
    skill = tmp_path / ".claude" / "skills" / "fleet" / "SKILL.md"
    assert skill.exists()
    assert "/opt/bin/fleet ls --json" in skill.read_text()


def test_installing_twice_changes_nothing_the_second_time(tmp_path):
    """Re-running setup after an upgrade must be safe and silent, not duplicative."""
    (tmp_path / ".claude").mkdir()
    install(tmp_path, ["claude"], "fleet")
    before = (tmp_path / ".claude" / "skills" / "fleet" / "SKILL.md").read_text()
    changes = install(tmp_path, ["claude"], "fleet")
    after = (tmp_path / ".claude" / "skills" / "fleet" / "SKILL.md").read_text()
    assert before == after
    assert [c.action for c in changes] == ["unchanged"]


def test_install_preserves_a_users_existing_agents_file(tmp_path):
    """AGENTS.md belongs to the user. We may add a region; we may not rewrite the file."""
    (tmp_path / ".codex").mkdir()
    agents = tmp_path / ".codex" / "AGENTS.md"
    agents.write_text("# My rules\n\nPrefer small commits.\n")
    install(tmp_path, ["codex"], "fleet")
    text = agents.read_text()
    assert "Prefer small commits." in text
    assert BEGIN in text


def test_dry_run_reports_the_change_without_touching_the_disk(tmp_path):
    (tmp_path / ".claude").mkdir()
    changes = install(tmp_path, ["claude"], "fleet", dry_run=True)
    assert [c.action for c in changes] == ["created"]
    assert not (tmp_path / ".claude" / "skills" / "fleet" / "SKILL.md").exists()


def test_project_scope_puts_the_codex_file_at_the_repo_root(tmp_path):
    """In a repo the convention is ./AGENTS.md, not a nested .codex directory."""
    paths = plan(tmp_path, project=True)
    assert paths["codex"] == tmp_path / "AGENTS.md"
    assert paths["claude"] == tmp_path / ".claude" / "skills" / "fleet" / "SKILL.md"


# --------------------------------------------------------------- uninstall

def test_uninstall_removes_our_skill_file(tmp_path):
    (tmp_path / ".claude").mkdir()
    install(tmp_path, ["claude"], "fleet")
    uninstall(tmp_path, ["claude"])
    assert not (tmp_path / ".claude" / "skills" / "fleet" / "SKILL.md").exists()


def test_uninstall_leaves_the_rest_of_a_shared_agents_file_intact(tmp_path):
    (tmp_path / ".codex").mkdir()
    agents = tmp_path / ".codex" / "AGENTS.md"
    agents.write_text("# My rules\n\nPrefer small commits.\n")
    install(tmp_path, ["codex"], "fleet")
    uninstall(tmp_path, ["codex"])
    text = agents.read_text()
    assert "Prefer small commits." in text
    assert BEGIN not in text and "fleet ls --json" not in text


# --------------------------------------------------------------- CLI wiring

def test_cli_setup_dry_run_reports_the_file_without_writing_it(tmp_path, monkeypatch):
    """The command a user runs first should be able to show its blast radius."""
    from pathlib import Path

    from typer.testing import CliRunner

    from fleet.cli import app

    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    result = CliRunner().invoke(app, ["setup", "--dry-run"])
    assert result.exit_code == 0, result.output
    assert "SKILL.md" in result.output
    assert not (tmp_path / ".claude" / "skills" / "fleet" / "SKILL.md").exists()


def test_cli_setup_installs_and_is_reported_as_unchanged_on_a_second_run(tmp_path, monkeypatch):
    from pathlib import Path

    from typer.testing import CliRunner

    from fleet.cli import app

    (tmp_path / ".claude").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    runner = CliRunner()
    assert runner.invoke(app, ["setup"]).exit_code == 0
    assert (tmp_path / ".claude" / "skills" / "fleet" / "SKILL.md").exists()
    second = runner.invoke(app, ["setup"])
    assert second.exit_code == 0
    assert "unchanged" in second.output


# --------------------------------------------------------------- hermes

def test_hermes_is_detected_when_it_is_installed(tmp_path):
    (tmp_path / ".hermes").mkdir()
    assert detect_targets(tmp_path) == ["hermes"]


def test_hermes_installs_as_a_skill_not_into_the_system_prompt(tmp_path):
    """SOUL.md is Hermes's system prompt -- a block there costs tokens in every
    conversation. Skills load only when a task needs them."""
    path = plan(tmp_path)["hermes"]
    assert path == tmp_path / ".hermes" / "skills" / "devops" / "fleet" / "SKILL.md"


def test_hermes_project_scope_uses_the_shared_agents_file(tmp_path):
    assert plan(tmp_path, project=True)["hermes"] == tmp_path / "AGENTS.md"


def test_the_hermes_skill_declares_every_field_hermes_requires():
    """Hermes requires five keys where Claude Code needs two. A skill missing one is
    not rejected loudly -- it simply never loads, which is the worst failure for a
    setup command."""
    import yaml

    text = hermes_skill_text("fleet")
    _, frontmatter, _ = text.split("---\n", 2)
    meta = yaml.safe_load(frontmatter)
    for required in ("name", "description", "version", "author", "license"):
        assert meta.get(required), f"missing required field {required!r}"


def test_the_hermes_skill_only_surfaces_where_a_shell_exists():
    """Every command it recommends is a shell command."""
    import yaml

    _, frontmatter, _ = hermes_skill_text("fleet").split("---\n", 2)
    meta = yaml.safe_load(frontmatter)
    assert "terminal" in meta["metadata"]["hermes"]["requires_tools"]


def test_the_hermes_skill_reports_a_real_version():
    """Hardcoding one guarantees it drifts from the package that is installed."""
    import yaml

    from fleet.setup import package_version

    _, frontmatter, _ = hermes_skill_text("fleet").split("---\n", 2)
    assert yaml.safe_load(frontmatter)["version"] == package_version()


def test_the_hermes_skill_embeds_the_resolved_command():
    assert "/opt/bin/fleet ls --json" in hermes_skill_text("/opt/bin/fleet")


def test_installing_for_hermes_writes_the_skill(tmp_path):
    (tmp_path / ".hermes").mkdir()
    install(tmp_path, ["hermes"], "fleet")
    assert (tmp_path / ".hermes" / "skills" / "devops" / "fleet" / "SKILL.md").exists()


def test_hermes_never_touches_the_system_prompt(tmp_path):
    """Belt and braces on the decision above: SOUL.md must come back untouched."""
    (tmp_path / ".hermes").mkdir()
    soul = tmp_path / ".hermes" / "SOUL.md"
    soul.write_text("You are Hermes Agent.\n")
    install(tmp_path, ["hermes"], "fleet")
    assert soul.read_text() == "You are Hermes Agent.\n"


def test_codex_and_hermes_share_one_file_in_a_project_and_are_reported_once(tmp_path):
    """Both read ./AGENTS.md. Writing it twice is harmless because the block is
    idempotent, but reporting two changes for one file is a lie about what happened."""
    changes = install(tmp_path, ["codex", "hermes"], "fleet", project=True)
    assert len(changes) == 1
    assert changes[0].path == tmp_path / "AGENTS.md"


def test_uninstalling_a_shared_project_file_also_happens_once(tmp_path):
    install(tmp_path, ["codex", "hermes"], "fleet", project=True)
    changes = uninstall(tmp_path, ["codex", "hermes"], project=True)
    assert len(changes) == 1


def test_the_cli_accepts_every_target_the_setup_module_supports(tmp_path, monkeypatch):
    """cli.py used to hardcode its own list, so adding a target to setup.py left the
    command rejecting it. The list now comes from one place."""
    from pathlib import Path

    from typer.testing import CliRunner

    from fleet.cli import app
    from fleet.setup import TARGETS

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    for target in TARGETS:
        result = CliRunner().invoke(app, ["setup", "--target", target, "--dry-run"])
        assert result.exit_code == 0, f"--target {target}: {result.output}"


def test_target_all_covers_every_supported_agent(tmp_path, monkeypatch):
    from pathlib import Path

    from typer.testing import CliRunner

    from fleet.cli import app
    from fleet.setup import TARGETS

    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    out = CliRunner().invoke(app, ["setup", "--target", "all", "--dry-run"]).output
    assert len(out.strip().splitlines()) >= len(TARGETS)


# ------------------------------------------- what the agent is told it may do

def test_the_instructions_separate_what_needs_the_center():
    """An agent that does not know the difference either avoids things that would work,
    or tries things that cannot and reports a confusing refusal as a fault."""
    from fleet.setup import skill_text

    text = skill_text("fleet")
    assert "Only on the center" in text
    assert "is_center" in text, "and says how to find out, rather than guessing"
    for center_only in ("--allow", "--deny", "--enroll", "fleet sync"):
        assert center_only in text


def test_every_command_an_agent_can_use_is_listed():
    """Adding a command and not telling the agents is how it stays unused."""
    from fleet.cli import app
    from fleet.setup import skill_text

    text = skill_text("fleet")
    # top needs a terminal and add/edit/install/probe/paths/setup are human-facing
    expected = {"ls", "show", "ssh", "access", "center", "sync", "update", "rm", "top"}
    listed = {c.name for c in app.registered_commands} & expected
    for name in listed:
        assert f"fleet {name}" in text, f"agents are not told about `fleet {name}`"


def test_the_agent_is_told_never_to_handle_a_password():
    """The only command that asks for one needs a human, and an agent offering to type
    it would put a credential in a transcript that is replayed forever."""
    from fleet.setup import skill_text

    text = skill_text("fleet")
    assert "Never type a password" in text
    assert "human must run it" in text


def test_the_agent_is_told_an_absent_center_is_normal():
    """It is usually a laptop. Reporting a closed lid as a fault would be noise."""
    from fleet.setup import skill_text

    text = skill_text("fleet")
    assert "expected to be offline" in text
    assert "not an error" in text


def test_the_agent_is_told_relayed_telemetry_is_second_hand():
    from fleet.setup import skill_text

    text = skill_text("fleet")
    assert "broadcast" in text and "do not present it as live" in text
