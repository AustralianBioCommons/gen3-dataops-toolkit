"""Functional tests for `g3dt config set` (the local bootstrap marker).

In the SSM-backed toolkit, `config set` writes exactly one thing: the tiny
g3dt.yaml bootstrap marker (project / region / default_env). Deployed settings
(dictionary_version, domain, ...) are CDK INPUTS that flow to SSM — the command
must refuse them and point the operator at the CDK repo instead. These tests
guard both sides of that boundary end to end, through the CLI, plus the
stale-cache failure mode (a write followed by a read in the same process must
see the new value).
"""
import textwrap

import pytest
from typer.testing import CliRunner

from g3dt import config
from g3dt.cli.main import app

runner = CliRunner()

_MARKER_YAML = """
project: etl
region: ap-southeast-2
default_env: test
profiles:
  test: etl_test
"""


@pytest.fixture(autouse=True)
def _fresh_cache():
    config._load_yaml_cached.cache_clear()
    yield
    config._load_yaml_cached.cache_clear()


def _seed_marker(tmp_path, monkeypatch):
    """Write the fixture marker to a temp file and point the CLI at it."""
    marker_path = tmp_path / "g3dt.yaml"
    marker_path.write_text(textwrap.dedent(_MARKER_YAML))
    monkeypatch.setenv("G3DT_MARKER", str(marker_path))
    monkeypatch.delenv("G3DT_PROJECT", raising=False)
    monkeypatch.delenv("G3DT_DEFAULT_ENV", raising=False)
    return marker_path


def test_config_set_updates_bootstrap_key(tmp_path, monkeypatch):
    """
    Inputs:  g3dt config set default_env staging
    Expected Output:
      - exit code 0, confirmation showing 'test -> staging' and the file path
      - the marker file now carries default_env: staging
      - the untouched profiles: map survives the rewrite

    The marker is the only local config; a successor must be able to flip the
    default env with one command instead of hand-editing YAML.
    """
    marker_path = _seed_marker(tmp_path, monkeypatch)

    result = runner.invoke(app, ["config", "set", "default_env", "staging"])
    assert result.exit_code == 0, result.output
    assert "test -> staging" in result.output

    text = marker_path.read_text()
    assert "default_env: staging" in text
    assert "etl_test" in text  # the profiles map survived


def test_config_set_then_read_reflects_change(tmp_path, monkeypatch):
    """
    Inputs:  load_marker() (primes the YAML cache) -> config set -> load_marker()
    Expected Output: the second read returns the NEW value, not the cached one.

    The marker loader caches parsed YAML by path for the process lifetime. If
    set_marker_value forgot to invalidate that cache, a set followed by any
    command in the same process would silently use the stale value.
    """
    _seed_marker(tmp_path, monkeypatch)

    assert config.load_marker()["default_env"] == "test"  # warm the cache
    result = runner.invoke(app, ["config", "set", "default_env", "staging"])
    assert result.exit_code == 0, result.output
    assert config.load_marker()["default_env"] == "staging"


def test_config_set_rejects_deployed_settings(tmp_path, monkeypatch):
    """
    Inputs:  g3dt config set dictionary_version v9.9.9
    Expected Output: exit 1; the message names the settable keys AND redirects
    the operator to the CDK INPUT file + redeploy; the marker is untouched.

    This is the guard for the platform's config model: deployed settings live
    in config/<project>.<env>.json in the CDK repo and flow to SSM. Letting
    them be written locally would recreate the config drift the SSM design
    exists to kill.
    """
    marker_path = _seed_marker(tmp_path, monkeypatch)
    before = marker_path.read_text()

    result = runner.invoke(app, ["config", "set", "dictionary_version", "v9.9.9"])
    assert result.exit_code == 1
    assert "Settable keys" in result.output
    assert "gen3-aws-data-pipeline" in result.output  # the redirect
    assert marker_path.read_text() == before


def test_config_set_creates_marker_when_none_exists(tmp_path, monkeypatch):
    """
    Inputs:  no marker anywhere; g3dt config set project bpsyc
    Expected Output: exit 0 and a marker created at the G3DT_MARKER path with
    project: bpsyc — first-run setup is a single command, not a hand-authored
    file.
    """
    marker_path = tmp_path / "fresh" / "g3dt.yaml"  # does not exist yet
    marker_path.parent.mkdir()
    monkeypatch.setenv("G3DT_MARKER", str(marker_path))
    monkeypatch.delenv("G3DT_PROJECT", raising=False)

    result = runner.invoke(app, ["config", "set", "project", "bpsyc"])
    assert result.exit_code == 0, result.output
    assert marker_path.exists()
    assert "project: bpsyc" in marker_path.read_text()
