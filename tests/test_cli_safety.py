"""Tests for the destructive/production guards via the CLI.

These confirm the most important invariant: production actions can never
proceed without typing the target (a project id for ``delete``, the env name
for the ``synth`` commands), even unattended. Config resolution is stubbed at
each command module's boundary (env_of / study_of / dispatch.resolve_env) —
the guards must fire regardless of what resolution returns, and the runner is
patched so nothing executes.
"""
from unittest.mock import patch

from typer.testing import CliRunner

from g3dt.cli.main import app
from g3dt.config import EnvConfig, StudyConfig

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


def _study_cfg(study: str, env: str) -> StudyConfig:
    base = env[:-4] if env.endswith("_ec2") else env
    return StudyConfig(
        key=f"{study}_{base}",
        project_id=study.title(),
        program_id="program1",
        s3_metadata_path=f"s3://b/{base}/{study}/",
    )


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_synth_deploy_staging_runs_without_prompt(mock_run, _env):
    """
    Inputs:  g3dt synth deploy --env staging
    Expected Output: staging is not prod, so the deploy runs once with no prompt.
    """
    result = runner.invoke(app, ["synth", "deploy", "--env", "staging"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_synth_deploy_prod_aborts_without_typed_confirmation(mock_run, _env):
    """
    Inputs:  g3dt synth deploy --env prod   (empty confirmation)
    Expected Output: exit code 1 and the deploy never runs.
    """
    result = runner.invoke(app, ["synth", "deploy", "--env", "prod"], input="\n")
    assert result.exit_code == 1
    mock_run.assert_not_called()


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_synth_upload_staging_runs_without_prompt(mock_run, _env):
    """
    Inputs:  g3dt synth upload --env staging
    Expected Output: staging is not prod, so the upload runs once with no prompt.
    """
    result = runner.invoke(app, ["synth", "upload", "--env", "staging"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_synth_upload_prod_aborts_without_typed_confirmation(mock_run, _env):
    """
    Inputs:  g3dt synth upload --env prod   (empty confirmation)
    Expected Output: exit code 1 and the upload never runs.
    """
    result = runner.invoke(app, ["synth", "upload", "--env", "prod"], input="\n")
    assert result.exit_code == 1
    mock_run.assert_not_called()


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_synth_upload_prod_proceeds_when_env_typed(mock_run, _env):
    """
    Inputs:  g3dt synth upload --env prod   (types 'prod')
    Expected Output: exit code 0 and the upload runs once.
    """
    result = runner.invoke(app, ["synth", "upload", "--env", "prod"], input="prod\n")
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


@patch("g3dt.cli.synth.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_synth_delete_prod_aborts_without_typed_confirmation(mock_run, _env):
    """
    Inputs:  g3dt synth delete --env prod   (empty confirmation)
    Expected Output: exit code 1 and the deletion never runs.
    """
    result = runner.invoke(app, ["synth", "delete", "--env", "prod"], input="\n")
    assert result.exit_code == 1
    mock_run.assert_not_called()


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_prod_aborts_without_typed_confirmation(
    mock_run, _study, _env
):
    """
    Inputs:  g3dt delete metadata --studies ausdiab --env prod --version 0.8.1
             (empty confirmation)
    Expected Output: exit code 1 and the deletion never runs.

    Production deletes must never proceed unattended: even a specific-version
    delete requires typing the exact target (the resolved study key), so just
    pressing Enter aborts.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab", "--env", "prod",
         "--version", "0.8.1"],
        input="\n",
    )
    assert result.exit_code == 1
    mock_run.assert_not_called()


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_prod_proceeds_when_target_typed(mock_run, _study, _env):
    """
    Inputs:  g3dt delete metadata --studies ausdiab --env prod --version 0.8.1
             (types the resolved key 'ausdiab_prod')
    Expected Output: exit code 0 and the deletion runs once.

    Typing the exact target (the resolved study key) is the prod safety gate;
    once it matches, the single sequential job is dispatched.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab", "--env", "prod",
         "--version", "0.8.1"],
        input="ausdiab_prod\n",
    )
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_staging_yes_skips_prompt(mock_run, _study, _env):
    """
    Inputs:  g3dt delete metadata --studies ausdiab --env staging
             --version 0.8.1 --yes
    Expected Output: exit code 0 (non-prod prompt skipped) and runs once.

    For a specific version in a non-prod env, --yes is allowed to skip the
    confirmation so the command can run unattended.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab", "--env", "staging",
         "--version", "0.8.1", "--yes"],
    )
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_all_versions_prompts_even_with_yes(mock_run, _study, _env):
    """
    Inputs:  g3dt delete metadata --studies ausdiab --env staging
             --version all --yes   (empty confirmation)
    Expected Output: exit code 1 and the deletion never runs.

    Deleting ALL versions is the most destructive path, so its confirmation is
    unskippable: --yes does not bypass it, and an empty reply aborts even in a
    non-prod env.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab", "--env", "staging",
         "--version", "all", "--yes"],
        input="\n",
    )
    assert result.exit_code == 1
    mock_run.assert_not_called()


@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_requires_version(mock_run):
    """
    Inputs:  g3dt delete metadata --studies ausdiab --env staging  (no --version)
    Expected Output: exit code 2 and nothing runs.

    --version is mandatory so a forgotten flag can never silently trigger an
    all-versions wipe; omitting it is a usage error.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab", "--env", "staging"],
    )
    assert result.exit_code == 2
    mock_run.assert_not_called()
