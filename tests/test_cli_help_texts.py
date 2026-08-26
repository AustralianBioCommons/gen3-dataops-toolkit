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
    text that IS rendered. Strip the borders, then collapse whitespace.
    """
    return " ".join(result.output.replace("│", " ").split())


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
