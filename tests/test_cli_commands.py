"""Tests that CLI commands build the correct packaged-script argv.

These patch the subprocess runner so nothing actually executes — we only
assert the exact command the CLI *would* run. Config resolution is stubbed at
each command module's boundary (env_of / study_of / dispatch.resolve_env) with
a fully-populated EnvConfig/StudyConfig, so these stay pure UX tests; the
SSM-backed resolution itself is covered in test_cli_config.py and
test_resolver.py.
"""
import sys
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
    """Mimic the {study}_{env_base} key derivation the real resolver applies."""
    base = env[:-4] if env.endswith("_ec2") else env
    return StudyConfig(
        key=f"{study}_{base}",
        project_id=study.title(),
        program_id="program1",
        s3_metadata_path=f"s3://b/{base}/{study}/",
    )


def _argv(mock_run):
    """Return the argv list passed to the (single) patched runner.run call."""
    assert mock_run.call_count == 1, mock_run.call_args_list
    return list(mock_run.call_args.args[0])


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.metadata.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_metadata_upload_builds_resolved_argv(mock_run, _study, _env):
    """
    Inputs:  g3dt metadata upload --study ausdiab --env staging
    Expected Output:
      - exit code 0
      - runs the packaged upload_metadata.py via the current interpreter with
        the resolved study key 'ausdiab_staging' and env 'staging'
    """
    result = runner.invoke(
        app, ["metadata", "upload", "--study", "ausdiab", "--env", "staging"]
    )
    assert result.exit_code == 0, result.output
    argv = _argv(mock_run)
    assert argv[0] == sys.executable
    assert argv[1].endswith("services/upload/metadata/upload_metadata.py")
    assert argv[2:] == ["--study", "ausdiab_staging", "--env", "staging"]


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.metadata.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_metadata_upload_passes_specific_node(mock_run, _study, _env):
    """--node maps to the script's --specific-node flag."""
    result = runner.invoke(
        app,
        ["metadata", "upload", "--study", "ausdiab", "--env", "staging",
         "--node", "subject"],
    )
    assert result.exit_code == 0, result.output
    assert _argv(mock_run)[-2:] == ["--specific-node", "subject"]


@patch("g3dt.cli.dict_cmds.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_dict_deploy_wraps_bash_script(mock_run, _env):
    """
    Inputs:  g3dt dict deploy --env test
    Expected: bash <package>/services/dictionary/deploy_dd.sh test — and the
    resolved env handed over as G3DT_* variables (the script reads no config).
    """
    result = runner.invoke(app, ["dict", "deploy", "--env", "test"])
    assert result.exit_code == 0, result.output
    argv = _argv(mock_run)
    assert argv[0] == "bash"
    assert argv[1].endswith("services/dictionary/deploy_dd.sh")
    assert argv[2] == "test"
    script_env = mock_run.call_args.kwargs["env"]
    assert script_env["G3DT_DICTIONARY_VERSION"] == "v1"
    assert script_env["G3DT_SCHEMA_REPO"] == "Org/schema-repo"


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.metadata.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_metadata_upload_all_resolves_each_study(mock_run, _study, _env):
    """
    Inputs:  g3dt metadata upload-all --studies ausdiab,caughtcad --env staging
    Expected: each bare study resolves to its '<study>_staging' key
    """
    result = runner.invoke(
        app,
        ["metadata", "upload-all", "--studies", "ausdiab,caughtcad",
         "--env", "staging"],
    )
    assert result.exit_code == 0, result.output
    argv = _argv(mock_run)
    assert argv[0] == "bash"
    i = argv.index("--studies")
    assert argv[i + 1] == "ausdiab_staging,caughtcad_staging"


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_specific_version_builds_argv(mock_run, _study, _env):
    """
    Inputs:  g3dt delete metadata --studies ausdiab,caughtcad --env staging
             --version 0.9.8 --yes
    Expected: bash <package>/services/delete/delete_metadata.sh with each bare
    study resolved to its '<study>_staging' key and the version passed through.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab,caughtcad",
         "--env", "staging", "--version", "0.9.8", "--yes"],
    )
    assert result.exit_code == 0, result.output
    argv = _argv(mock_run)
    assert argv[0] == "bash"
    assert argv[1].endswith("services/delete/delete_metadata.sh")
    i = argv.index("--studies")
    assert argv[i + 1] == "ausdiab_staging,caughtcad_staging"
    j = argv.index("--version")
    assert argv[j + 1] == "0.9.8"


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_all_versions_passes_all(mock_run, _study, _env):
    """
    Inputs:  g3dt delete metadata --studies ausdiab --env staging --version all
             (confirmed 'y' at the unskippable all-versions prompt)
    Expected: the wrapper is invoked with --version all, which selects the
    delete-everything worker downstream.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab", "--env", "staging",
         "--version", "all"],
        input="y\n",
    )
    assert result.exit_code == 0, result.output
    argv = _argv(mock_run)
    assert argv[0] == "bash"
    assert argv[1].endswith("services/delete/delete_metadata.sh")
    j = argv.index("--version")
    assert argv[j + 1] == "all"


@patch("g3dt.cli.k8s.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_k8s_restart_schema_passes_env_argo_args(mock_run, _env):
    """restart-schema passes the env's domain/app/namespace to the argo script."""
    result = runner.invoke(app, ["k8s", "restart-schema", "--env", "test"])
    assert result.exit_code == 0, result.output
    argv = _argv(mock_run)
    assert argv[0] == "bash"
    assert argv[1].endswith("services/k8s_ops/argocd_restart_schema.sh")
    assert "-d" in argv and "-a" in argv and "-n" in argv
