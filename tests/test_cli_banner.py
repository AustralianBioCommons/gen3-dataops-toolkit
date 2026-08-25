"""Tests for the universal context banner (design doc section 6).

Background: every g3dt command — read or write — prints the resolved context
as its FIRST output line, on STDERR. Stderr matters enormously here: the
buildspecs do `eval "$(g3dt config dbt-env --env X)"`, so a banner on stdout
would be eval'd as shell and break every CodeBuild run. These tests pin the
stream split, the once-per-process rule, and the [PROD]/(remote) markings.
"""
import textwrap
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from g3dt import config, contexts
from g3dt.cli.main import app

runner = CliRunner()


@pytest.fixture(autouse=True)
def _clean(monkeypatch, tmp_path):
    for var in ("G3DT_PROJECT", "G3DT_DEFAULT_ENV", "G3DT_CONTEXT", "AWS_REGION"):
        monkeypatch.delenv(var, raising=False)
    marker = tmp_path / "g3dt.yaml"
    monkeypatch.setenv("G3DT_MARKER", str(marker))
    config._load_yaml_cached.cache_clear()
    contexts.reset()
    yield marker
    config._load_yaml_cached.cache_clear()
    contexts.reset()


def _write(marker, content):
    marker.write_text(textwrap.dedent(content))
    config._load_yaml_cached.cache_clear()


V2 = """
    current: etl/staging
    contexts:
      etl/staging: { project: etl, env: staging, profile: etl_staging }
      etl/prod:    { project: etl, env: prod, profile: etl_prod }
    project: etl
    region: ap-southeast-2
"""


def _fake_rc():
    params = {
        "glue/db/silver": "etl_staging_silver_db",
        "glue/db/gold": "etl_staging_gold_db",
        "glue/db/bronze": "etl_staging_bronze_db",
        "buckets/silver": "etl-staging-silver-1-r",
        "buckets/gold": "etl-staging-gold-1-r",
    }
    return SimpleNamespace(
        project="etl", env="staging", params=params,
        get=params.get,
        region="ap-southeast-2",
        athena_workgroup="etl-staging",
        athena_output_location="s3://etl-staging-athena-results-1-r/",
    )


def test_banner_is_first_stderr_line_and_stdout_is_clean_for_dbt_env(_clean):
    """
    THE load-bearing case: `config dbt-env` stdout is eval'd by CodeBuild.

    Expected: every stdout line starts with 'export'; the banner appears on
    stderr only, as the first line, naming the resolved context.
    """
    _write(_clean, V2)
    with patch("g3dt.cli._internal.resolve.rc_of", return_value=_fake_rc()):
        result = runner.invoke(app, ["config", "dbt-env", "--env", "staging"])
    assert result.exit_code == 0, result.output
    stdout_lines = [l for l in result.stdout.splitlines() if l.strip()]
    assert stdout_lines and all(l.startswith("export ") for l in stdout_lines)
    stderr_lines = [l for l in result.stderr.splitlines() if l.strip()]
    assert stderr_lines[0].startswith("ctx etl/staging")


def test_banner_printed_once_per_process(_clean):
    """Two resolution paths in one command must still yield ONE banner."""
    _write(_clean, V2)
    with patch("g3dt.cli._internal.resolve.rc_of", return_value=_fake_rc()):
        result = runner.invoke(app, ["config", "dbt-env", "--env", "staging"])
    assert result.stderr.count("ctx etl/staging") == 1


def test_banner_marks_prod_contexts(_clean):
    _write(_clean, """
        current: etl/prod
        contexts:
          etl/prod: { project: etl, env: prod, profile: etl_prod }
        project: etl
        region: ap-southeast-2
    """)
    result = runner.invoke(app, ["config", "contexts"])
    assert "[PROD]" in result.stderr.splitlines()[0]


def test_envless_command_prints_banner(_clean):
    """`g3dt version` takes no --env yet still announces the context."""
    _write(_clean, V2)
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert result.stderr.splitlines()[0].startswith("ctx etl/staging")


def test_no_configuration_prints_none_configured_banner(_clean):
    """A bare machine must not crash — the banner says how to get started."""
    result = runner.invoke(app, ["version"])
    assert result.exit_code == 0
    assert "none configured" in result.stderr.splitlines()[0]