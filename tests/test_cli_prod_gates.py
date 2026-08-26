"""Production gates on the control-plane commands (dict / k8s).

Background: the destructive data-plane commands (delete, metadata
upload-all) and every synth command have long required a typed
confirmation before acting on production — but the commands with the
biggest blast radius had none: `dict deploy` uploads a new schema to the
live commons and restarts its services, and `k8s restart-ms` restarts
every Gen3 microservice. A help-text audit flagged the inversion: a new
operator learned that fake-data commands are dangerous and real
service restarts are not. These tests pin the widened gate set.

Config resolution is stubbed at each module's boundary (env_of) and the
runner is patched, exactly as in test_cli_safety.py: the gate must fire
regardless of what resolution returns, and nothing may execute.
"""
from unittest.mock import patch

from typer.testing import CliRunner

from g3dt.cli.main import app
from g3dt.config import EnvConfig

runner = CliRunner()


def _env_cfg(name: str) -> EnvConfig:
    return EnvConfig(
        name=name,
        is_ec2=name.endswith("_ec2"),
        region="ap-southeast-2",
        dictionary_version="v1",
        aws_profile=None,
        aws_secret_name="sec",
        schema_s3_uri="u",
        domain="d",
        app_name="a",
        namespace="n",
        cluster_name="c",
        schema_repo="Org/schema-repo",
    )


@patch("g3dt.cli.dict_cmds.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_dict_deploy_prod_aborts_without_typed_confirmation(mock_run, _env):
    """
    Inputs:  g3dt dict deploy --env prod   (empty confirmation)
    Expected: exit 1 and the deploy script never runs.
    """
    result = runner.invoke(app, ["dict", "deploy", "--env", "prod"], input="\n")
    assert result.exit_code == 1
    mock_run.assert_not_called()


@patch("g3dt.cli.dict_cmds.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_dict_deploy_prod_proceeds_when_env_typed(mock_run, _env):
    """
    Inputs:  g3dt dict deploy --env prod   (types 'prod')
    Expected: the typed token opens the gate and the script runs once.
    """
    result = runner.invoke(app, ["dict", "deploy", "--env", "prod"], input="prod\n")
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


@patch("g3dt.cli.dict_cmds.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_dict_deploy_staging_runs_without_prompt(mock_run, _env):
    """
    Inputs:  g3dt dict deploy --env staging  (no stdin)
    Expected: non-prod is unchanged — no prompt, runs once. The gate must
    add zero friction to the everyday flow or it will get worked around.
    """
    result = runner.invoke(app, ["dict", "deploy", "--env", "staging"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


@patch("g3dt.cli.dict_cmds.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_dict_upload_prod_aborts_without_typed_confirmation(mock_run, _env):
    """
    Inputs:  g3dt dict upload --env prod   (empty confirmation)
    Expected: exit 1, nothing uploaded. (dict pull stays ungated — it only
    downloads a schema locally and touches nothing in the commons.)
    """
    result = runner.invoke(app, ["dict", "upload", "--env", "prod"], input="\n")
    assert result.exit_code == 1
    mock_run.assert_not_called()


@patch("g3dt.cli.k8s.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_k8s_restart_etl_prod_aborts_without_typed_confirmation(mock_run, _env):
    """
    Inputs:  g3dt k8s restart-etl --env prod   (empty confirmation)
    Expected: exit 1 and no restart runs.
    """
    result = runner.invoke(app, ["k8s", "restart-etl", "--env", "prod"], input="\n")
    assert result.exit_code == 1
    mock_run.assert_not_called()


@patch("g3dt.cli.k8s.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_k8s_restart_ms_prod_proceeds_when_env_typed(mock_run, _env):
    """
    Inputs:  g3dt k8s restart-ms --env prod   (types 'prod')
    Expected: the typed token opens the gate and the restart script runs.
    """
    result = runner.invoke(app, ["k8s", "restart-ms", "--env", "prod"], input="prod\n")
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


def test_k8s_restart_uses_context_name_as_token_for_prod_context(tmp_path, monkeypatch):
    """
    Inputs:  a marker whose CURRENT context is production-classified; the
             operator runs k8s restart-schema through that context and types
             the CONTEXT NAME (not the bare env).
    Expected: the context name is the token (same contract as
             test_cli_safety_contexts.py pins for synth/delete).
    """
    from g3dt import config, contexts

    marker = tmp_path / "g3dt.yaml"
    marker.write_text(
        "current: etl/prod\n"
        "contexts:\n"
        "  etl/prod: { project: etl, env: prod, profile: p }\n"
        "project: etl\n"
        "region: ap-southeast-2\n"
    )
    monkeypatch.setenv("G3DT_MARKER", str(marker))
    config._load_yaml_cached.cache_clear()
    contexts.reset()

    with patch("g3dt.cli.k8s.env_of", side_effect=_env_cfg), \
         patch("g3dt.cli._internal.runner.run") as mock_run:
        result = runner.invoke(
            app, ["k8s", "restart-schema"], input="etl/prod\n"
        )
    assert result.exit_code == 0, result.output
    assert "etl/prod" in result.output  # the token the prompt demands
    mock_run.assert_called_once()
