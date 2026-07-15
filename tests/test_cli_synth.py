"""Tests that `g3dt synth generate` builds the right gen3-metadata-simulator call.

The command wraps services/synthetic_data/generate_synth_metadata.sh; these patch
the subprocess runner and assert the exact flags: the required positional study,
the default keyless 'random' provider, the --llm opt-in, and the consolidated
--num-records flag (a single count, or a comma list with one count per study).
A per-study count list whose length does not match the studies is rejected before
anything runs. Targeting a production env requires typing the env name to confirm.

Environment resolution is stubbed at the synth module boundary (env_of) — these
are UX tests for flag construction, not SSM tests (see test_cli_config.py for
those).
"""
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from g3dt.cli.main import app
from g3dt.config import EnvConfig

runner = CliRunner()


def _env_cfg(name: str) -> EnvConfig:
    """A fully-populated EnvConfig as resolve_env would return it."""
    return EnvConfig(
        name=name,
        is_ec2=name.endswith("_ec2"),
        region="ap-southeast-2",
        dictionary_version="v1.1.6",
        aws_profile=None,
        aws_secret_name="sec",
        schema_s3_uri="u",
        domain="d",
        app_name="a",
        namespace="n",
        cluster_name="c",
        schema_repo="Org/schema-repo",
    )


@pytest.fixture
def schema_dir(tmp_path, monkeypatch):
    """Point the schema cache at a temp dir holding the v1.1.6 schema.

    The generate command checks the local cache before pulling; seeding the
    file keeps these tests offline.
    """
    monkeypatch.setattr("g3dt.cli.synth.SCHEMA_DIR", tmp_path)
    (tmp_path / "acdc_schema_v1.1.6.json").write_text("{}")
    return tmp_path


def _gen_argv(mock_run):
    """Return the argv of the generate_synth_metadata.sh invocation (last call)."""
    for call in mock_run.call_args_list:
        argv = list(call.args[0])
        if any(str(a).endswith("generate_synth_metadata.sh") for a in argv):
            return argv
    raise AssertionError(f"generate script not invoked: {mock_run.call_args_list}")


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_generate_passes_study_and_defaults_to_random(mock_run, _env, schema_dir):
    """
    Inputs:  g3dt synth generate AusDiab_Simulated --num-records 5
    Expected Output:
      - wraps generate_synth_metadata.sh with the study as --studies, the default
        keyless --provider random, --num-records 5, and --version from the env.
    """
    result = runner.invoke(
        app, ["synth", "generate", "AusDiab_Simulated", "--num-records", "5"]
    )
    assert result.exit_code == 0, result.output
    argv = _gen_argv(mock_run)
    assert argv[argv.index("--studies") + 1] == "AusDiab_Simulated"
    assert argv[argv.index("--provider") + 1] == "random"
    assert argv[argv.index("--num-records") + 1] == "5"
    assert argv[argv.index("--version") + 1] == "v1.1.6"


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_generate_llm_flag_enables_llm(mock_run, _env, schema_dir):
    """
    Inputs:  g3dt synth generate AusDiab_Simulated --llm
    Expected Output: --provider llm is passed (opt-in; reads LLM config from .env).
    """
    result = runner.invoke(app, ["synth", "generate", "AusDiab_Simulated", "--llm"])
    assert result.exit_code == 0, result.output
    argv = _gen_argv(mock_run)
    assert argv[argv.index("--provider") + 1] == "llm"


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_generate_num_records_list_for_many_studies(mock_run, _env, schema_dir):
    """
    Inputs:  g3dt synth generate "AusDiab_Simulated,Baker-Biobank_Simulated" -n "30,60"
    Expected Output: the comma study list and the matching per-study count list are
    passed straight through to the script (one count per study).
    """
    result = runner.invoke(
        app,
        ["synth", "generate", "AusDiab_Simulated,Baker-Biobank_Simulated", "-n", "30,60"],
    )
    assert result.exit_code == 0, result.output
    argv = _gen_argv(mock_run)
    assert (
        argv[argv.index("--studies") + 1]
        == "AusDiab_Simulated,Baker-Biobank_Simulated"
    )
    assert argv[argv.index("--num-records") + 1] == "30,60"


def test_generate_requires_study():
    """
    Inputs:  g3dt synth generate            (no study given)
    Expected Output: exit code 2 — the study argument is required.
    """
    result = runner.invoke(app, ["synth", "generate"])
    assert result.exit_code == 2


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_generate_rejects_mismatched_num_records_list(mock_run, _env):
    """
    Inputs:  g3dt synth generate "A,B" --num-records "30,60,90"
    Expected Output: exit code 1 (3 counts for 2 studies) and nothing runs.
    """
    result = runner.invoke(
        app, ["synth", "generate", "A,B", "--num-records", "30,60,90"]
    )
    assert result.exit_code == 1
    mock_run.assert_not_called()


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_generate_non_prod_env_runs_without_prompt(mock_run, _env, schema_dir):
    """
    Inputs:  g3dt synth generate AusDiab_Simulated --env staging
    Expected Output: staging is not production, so it runs with no confirmation.
    """
    result = runner.invoke(
        app, ["synth", "generate", "AusDiab_Simulated", "--env", "staging"]
    )
    assert result.exit_code == 0, result.output
    _gen_argv(mock_run)  # raises if the generate script was not invoked


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_generate_prod_aborts_without_typed_confirmation(mock_run, _env):
    """
    Inputs:  g3dt synth generate AusDiab_Simulated --env prod   (empty confirmation)
    Expected Output: exit code 1 and the generate script never runs.

    The typed-confirmation guard is the last line of defence against synthetic
    data landing in a production commons; --yes must not bypass it.
    """
    result = runner.invoke(
        app, ["synth", "generate", "AusDiab_Simulated", "--env", "prod"], input="\n"
    )
    assert result.exit_code == 1
    mock_run.assert_not_called()


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_generate_prod_proceeds_when_env_typed(mock_run, _env, schema_dir):
    """
    Inputs:  g3dt synth generate AusDiab_Simulated --env prod   (types 'prod')
    Expected Output: exit code 0 and the generate script runs once.
    """
    result = runner.invoke(
        app, ["synth", "generate", "AusDiab_Simulated", "--env", "prod"], input="prod\n"
    )
    assert result.exit_code == 0, result.output
    _gen_argv(mock_run)  # raises if the generate script was not invoked
