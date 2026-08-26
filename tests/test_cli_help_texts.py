"""Pins for the rendered --help text a first-time user actually reads.

Background: a help-text audit found the CLI never defined its central
"context" concept in any rendered help, used five different phrasings for
the same --env option, and pointed users at the deprecated hidden
`config set` command. These tests pin the fixes at the rendered-output
level (CliRunner --help), so a future edit that reintroduces the drift
fails here instead of in a new operator's onboarding session.
"""
import pytest
from typer.testing import CliRunner

from g3dt.cli.main import app

runner = CliRunner()


def _rendered(result) -> str:
    """Collapse rich's wrapping for substring assertions.

    Rich wraps option help inside a box; a phrase can be split across lines
    with `│` border characters in between, so raw substring checks fail on
    text that IS rendered. ANSI escape codes are stripped too: rich
    force-enables color under GITHUB_ACTIONS, and codes landing inside a
    phrase would break the match (conftest sets NO_COLOR, this is the
    belt-and-braces). Strip both, then collapse whitespace.
    """
    import re

    plain = re.sub(r"\x1b\[[0-9;]*[A-Za-z]", "", result.output)
    return " ".join(plain.replace("│", " ").split())


@pytest.fixture(autouse=True)
def _hermetic_marker(tmp_path, monkeypatch):
    """--help still triggers the context banner machinery; keep it off the
    developer's real marker."""
    from g3dt import config, contexts

    marker = tmp_path / "g3dt.yaml"
    marker.write_text("project: etl\nregion: ap-southeast-2\n")
    monkeypatch.setenv("G3DT_MARKER", str(marker))
    config._load_yaml_cached.cache_clear()
    contexts.reset()
    yield
    config._load_yaml_cached.cache_clear()
    contexts.reset()


def test_config_help_defines_what_a_context_is():
    """
    Input:    g3dt config --help
    Expected: the group help defines the concept — a named
              (project, env, AWS profile, region) tuple — instead of just
              naming it. This is the first place a new operator looks.
    """
    result = runner.invoke(app, ["config", "--help"])
    assert result.exit_code == 0
    assert "project, env, AWS profile, region" in result.output


def test_env_option_reads_identically_across_command_groups():
    """
    Input:    --help of two commands in different groups.
    Expected: the exact same --env phrasing (the shared ENV_OPT constant).
              Before this change --env had five phrasings, so the same
              option read differently depending on where you met it.
    """
    from g3dt.cli._internal.helptext import ENV_OPT

    probe = " ".join(ENV_OPT.split())[:40]
    for cmd in (["dict", "deploy", "--help"], ["k8s", "restart-etl", "--help"]):
        result = runner.invoke(app, cmd)
        assert result.exit_code == 0
        assert probe in _rendered(result), f"{cmd}: --env phrasing drifted"


def test_synth_help_points_at_set_key_not_deprecated_config_set():
    """
    Input:    g3dt synth generate --help
    Expected: the LLM key guidance names `synth set-key` — not the hidden,
              deprecated `config set`, which four help strings still
              recommended after set-key shipped.
    """
    result = runner.invoke(app, ["synth", "generate", "--help"])
    assert result.exit_code == 0
    rendered = _rendered(result)
    assert "synth set-key" in rendered
    assert "config set llm_api_key_file" not in rendered


def test_release_group_help_no_longer_promises_inspect():
    """
    Input:    g3dt release --help
    Expected: the group help describes only what exists (`write`); it used
              to say "Write/inspect" with no inspect command anywhere.
    """
    result = runner.invoke(app, ["release", "--help"])
    assert result.exit_code == 0
    assert "inspect" not in result.output.lower()


def test_short_alias_convention_one_letter_one_meaning():
    """
    Input:    --help of the commands whose short aliases changed.
    Expected: the CLI-wide convention holds — a letter means ONE thing:
              -s = studies (never --sync), -l = --limit (freeing -n for
              record counts), -f = --follow (config diff --file is
              long-only), -v = the dictionary --version, -o = --on.

    Background: 3.8.x had three collisions (-s study/sync, -n
    num-records/limit, -f file/follow), so muscle memory from one command
    silently meant something else on another.
    """
    checks = [
        (["metadata", "upload-all", "--help"], "-s", "--studies"),
        (["synth", "deploy", "--help"], "-s", "--studies"),
        (["indexd", "check-download", "--help"], "-l", "--limit"),
        (["dict", "pull", "--help"], "-v", "--version"),
        (["metadata", "upload", "--help"], "-o", "--on"),
    ]
    for cmd, short, long in checks:
        rendered = _rendered(runner.invoke(app, cmd))
        assert short in rendered and long in rendered, f"{cmd}: {short} missing"

    # the collision losers: k8s --sync and config diff --file go long-only
    rendered = _rendered(runner.invoke(app, ["k8s", "restart-etl", "--help"]))
    assert "--sync" in rendered and " -s " not in f" {rendered} "
    rendered = _rendered(runner.invoke(app, ["config", "diff", "--help"]))
    assert "--file" in rendered and " -f " not in f" {rendered} "


def test_pipeline_which_rejects_unknown_stage_at_the_typer_layer():
    """
    Input:    g3dt pipeline status --which nope
    Expected: usage error (exit 2) listing the valid choices — previously a
              bad value survived to a runtime SSM lookup and a hand-written
              error message.
    """
    result = runner.invoke(app, ["pipeline", "status", "--which", "nope"])
    assert result.exit_code == 2
    rendered = _rendered(result)
    assert "writeReleaseInfo" in rendered and "dbtTestAndRun" in rendered


def test_config_diff_missing_file_is_a_usage_error_not_a_traceback():
    """
    Input:    g3dt config diff --file /does/not/exist.json
    Expected: Typer's path validation rejects it (exit 2) — previously the
              command crashed later with a raw FileNotFoundError traceback
              from file.read_text().
    """
    result = runner.invoke(
        app, ["config", "diff", "--env", "test", "--file", "/does/not/exist.json"]
    )
    assert result.exit_code == 2
    assert "does not exist" in _rendered(result)
