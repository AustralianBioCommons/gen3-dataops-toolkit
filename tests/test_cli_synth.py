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


@pytest.fixture(autouse=True)
def _isolated_marker(tmp_path, monkeypatch):
    """Keep tests hermetic: the LLM key-file fallback reads the marker, and a
    developer's real ~/.g3dt/g3dt.yaml (with llm_api_key_file set) would
    otherwise leak into the asserted subprocess env."""
    monkeypatch.setenv("G3DT_MARKER", str(tmp_path / "no-marker.yaml"))


def _env_cfg(name: str, llm_model: str = "ssm-model") -> EnvConfig:
    """A fully-populated EnvConfig as resolve_env would return it.

    Carries an llm_model by default (as an env deployed with the CDK's llm
    block would); pass llm_model=None to model a deployment without the block.
    """
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
        llm_model=llm_model,
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
    Expected Output: --provider llm is passed, and the env's SSM-resolved
    model reaches the script as G3DT_LLM_MODEL (the script forwards it to the
    simulator as a flag) — no local .env involved.
    """
    result = runner.invoke(app, ["synth", "generate", "AusDiab_Simulated", "--llm"])
    assert result.exit_code == 0, result.output
    argv = _gen_argv(mock_run)
    assert argv[argv.index("--provider") + 1] == "llm"
    env = _gen_env(mock_run)
    assert env["G3DT_LLM_MODEL"] == "ssm-model"
    assert env["G3DT_LLM_PROVIDER"] == "anthropic"


def _gen_env(mock_run):
    """Return the env dict of the generate_synth_metadata.sh invocation."""
    for call in mock_run.call_args_list:
        argv = list(call.args[0])
        if any(str(a).endswith("generate_synth_metadata.sh") for a in argv):
            return call.kwargs["env"]
    raise AssertionError(f"generate script not invoked: {mock_run.call_args_list}")


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_generate_llm_flags_override_ssm(mock_run, _env, schema_dir, tmp_path):
    """
    Inputs:  --llm --llm-provider openai --llm-model my-model
             --llm-api-key-file <existing file>
    Expected Output: the flag values win over the EnvConfig's SSM-resolved
    ones and the key path is exported as LLM_API_KEY_FILE. This is the
    "try a model with one command, no redeploy" path.
    """
    key_file = tmp_path / "key"
    key_file.write_text("sk-test")
    result = runner.invoke(
        app,
        ["synth", "generate", "AusDiab_Simulated", "--llm",
         "--llm-provider", "openai", "--llm-model", "my-model",
         "--llm-api-key-file", str(key_file)],
    )
    assert result.exit_code == 0, result.output
    env = _gen_env(mock_run)
    assert env["G3DT_LLM_PROVIDER"] == "openai"
    assert env["G3DT_LLM_MODEL"] == "my-model"
    assert env["LLM_API_KEY_FILE"] == str(key_file)


@patch("g3dt.cli.synth.env_of", side_effect=lambda name: _env_cfg(name, llm_model=None))
@patch("g3dt.cli._internal.runner.run")
def test_generate_llm_without_model_exits_with_guidance(mock_run, _env, schema_dir):
    """
    Inputs:  --llm against an env whose deployment has no llm block (no SSM
             model) and with no --llm-model flag
    Expected Output: exit 1 BEFORE the script runs, with a message pointing at
    both fixes (add the llm block to the CDK config, or pass --llm-model) —
    instead of the simulator failing later with its own .env-era error.
    """
    result = runner.invoke(app, ["synth", "generate", "AusDiab_Simulated", "--llm"])
    assert result.exit_code == 1
    assert "No LLM model configured" in result.output
    assert "--llm-model" in result.output
    assert not mock_run.called


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_generate_random_provider_skips_llm_plumbing(mock_run, _env, schema_dir):
    """
    Inputs:  a plain random-provider generate (the default)
    Expected Output: no LLM_API_KEY_FILE is injected — the keyless path stays
    keyless, and a missing model in the deployment can never affect it.
    """
    result = runner.invoke(app, ["synth", "generate", "AusDiab_Simulated"])
    assert result.exit_code == 0, result.output
    env = _gen_env(mock_run)
    assert "LLM_API_KEY_FILE" not in env


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


def _deploy_env(mock_run):
    """Return the env dict of the full_deploy_dd_and_synth.sh invocation."""
    for call in mock_run.call_args_list:
        argv = list(call.args[0])
        if any(str(a).endswith("full_deploy_dd_and_synth.sh") for a in argv):
            return call.kwargs["env"]
    raise AssertionError(f"full deploy script not invoked: {mock_run.call_args_list}")


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_deploy_studies_and_counts_reach_the_script(mock_run, _env):
    """
    Inputs:  synth deploy --studies synthetic_dataset_1 -n 100
             --prev-version v1.2.0
    Expected Output: the batch inputs reach full_deploy_dd_and_synth.sh as
    G3DT_SYNTH_* env vars, replacing its ACDC-era hardcoded studies, record
    counts, and previous-batch version.
    """
    result = runner.invoke(
        app,
        ["synth", "deploy", "--env", "test", "--studies", "synthetic_dataset_1",
         "-n", "100", "--prev-version", "v1.2.0"],
    )
    assert result.exit_code == 0, result.output
    env = _deploy_env(mock_run)
    assert env["G3DT_SYNTH_STUDIES"] == "synthetic_dataset_1"
    assert env["G3DT_SYNTH_NUM_RECORDS"] == "100"
    assert env["G3DT_SYNTH_PREV_VERSION"] == "v1.2.0"


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_deploy_without_batch_flags_leaves_script_defaults(mock_run, _env):
    """
    Inputs:  synth deploy with no batch flags
    Expected Output: no G3DT_SYNTH_* vars are injected — the script falls back
    to the documented ACDC demo defaults, exactly as before this change.
    """
    result = runner.invoke(app, ["synth", "deploy", "--env", "test"])
    assert result.exit_code == 0, result.output
    env = _deploy_env(mock_run)
    for key in ("G3DT_SYNTH_STUDIES", "G3DT_SYNTH_NUM_RECORDS",
                "G3DT_SYNTH_PREV_VERSION"):
        assert key not in env


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_deploy_rejects_mismatched_count_list(mock_run, _env):
    """
    Inputs:  --studies with one study but a two-value count list
    Expected Output: exit 1 before the script runs — the same per-study count
    validation generate performs, so a bad batch never reaches the commons.
    """
    result = runner.invoke(
        app,
        ["synth", "deploy", "--env", "test",
         "--studies", "only_one", "-n", "30,60"],
    )
    assert result.exit_code == 1
    assert "--num-records has 2 values but 1 studies" in result.output
    mock_run.assert_not_called()


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_deploy_skip_dict_reaches_the_script(mock_run, _env):
    """
    Inputs:  synth deploy --skip-dict
    Expected Output: G3DT_SYNTH_SKIP_DICT=1 in the subprocess env — the script
    then skips the dictionary S3 upload and schema restarts, running the
    synthetic-data-only flow (delete, generate, upload, ETL); without the
    flag the var is absent and the full flow runs.
    """
    result = runner.invoke(app, ["synth", "deploy", "--env", "test", "--skip-dict"])
    assert result.exit_code == 0, result.output
    assert _deploy_env(mock_run)["G3DT_SYNTH_SKIP_DICT"] == "1"

    mock_run.reset_mock()
    result = runner.invoke(app, ["synth", "deploy", "--env", "test"])
    assert result.exit_code == 0, result.output
    assert "G3DT_SYNTH_SKIP_DICT" not in _deploy_env(mock_run)
