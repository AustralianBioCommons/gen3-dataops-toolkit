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
from g3dt.config import EnvConfig, StudyConfig, dictionary_url

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
    # The script no longer builds the URL itself; it reads these.
    assert script_env["G3DT_DICT_URL"] == dictionary_url(_env_cfg("test"))
    assert script_env["G3DT_DICT_FILENAME"] == "acdc_schema_v1.json"


@patch("g3dt.cli.dict_cmds.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_dict_pull_passes_config_url_and_filename(mock_run, _env):
    """
    Inputs:  g3dt dict pull --env test
    Expected: bash <package>/services/dictionary/pull_dict.sh <url> <basename>

    `dict pull` had no test at all, and nothing asserted the composed URL
    anywhere. The URL is compared against config.dictionary_url rather than a
    literal, so this proves the command reads config instead of re-pinning a
    hardcoded string somewhere new. The explicit basename is what stops
    pull_dict.sh regexing a version out of the URL to name the file.
    """
    result = runner.invoke(app, ["dict", "pull", "--env", "test"])
    assert result.exit_code == 0, result.output
    argv = _argv(mock_run)
    assert argv[0] == "bash"
    assert argv[1].endswith("services/dictionary/pull_dict.sh")
    assert argv[2] == dictionary_url(_env_cfg("test"))
    assert argv[3] == "acdc_schema_v1.json"


@patch("g3dt.cli.dict_cmds.env_of", side_effect=_env_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_dict_deploy_version_flag_overrides_the_declared_version(mock_run, _env):
    """
    Inputs:  g3dt dict deploy --env test --version v9.9.9 (env declares v1)
    Expected: every G3DT_* dictionary variable describes v9.9.9, and the operator
    is warned that SSM still declares v1.

    This is what makes promotion practical: the same tag can go to test then
    staging without a `cdk deploy` per environment. The warning matters because
    the override does not persist -- `config show` keeps reporting v1 until the
    CDK config catches up, and `config diff` is what reconciles them.
    """
    result = runner.invoke(
        app, ["dict", "deploy", "--env", "test", "--version", "v9.9.9"]
    )
    assert result.exit_code == 0, result.output
    script_env = mock_run.call_args.kwargs["env"]
    assert script_env["G3DT_DICTIONARY_VERSION"] == "v9.9.9"
    assert "/refs/tags/v9.9.9/" in script_env["G3DT_DICT_URL"]
    assert script_env["G3DT_DICT_FILENAME"] == "acdc_schema_v9.9.9.json"
    assert "SSM says v1" in result.output


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
@patch("g3dt.cli.metadata.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_metadata_upload_all_prod_refused_without_allow_prod(mock_run, _study, _env):
    """
    Background:
        Bulk upload against production used to be impossible (the wrapped
        script hard-aborted on any 'prod') while showing no local guard at
        all. The gate makes prod possible but deliberate: without
        --allow-prod the CLI refuses before anything is dispatched.

    Inputs:  --env prod, no --allow-prod
    Expected Output: exit 2, the wrapped script is never invoked.
    """
    result = runner.invoke(
        app,
        ["metadata", "upload-all", "--studies", "ausdiab", "--env", "prod"],
    )
    assert result.exit_code == 2
    mock_run.assert_not_called()
    assert "--allow-prod" in result.output


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.metadata.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_metadata_upload_all_prod_with_flag_requires_typed_confirmation(
    mock_run, _study, _env
):
    """
    Background:
        --allow-prod alone is not enough: the operator must type the env
        name exactly (confirm_prod_strict), locally, BEFORE any dispatch —
        SSM has no TTY so a remote prompt could never be answered.

    Inputs:  --env prod --allow-prod, then 'prod' typed at the prompt
    Expected Output: exit 0 and the wrapped script receives --allow-prod.
    """
    result = runner.invoke(
        app,
        ["metadata", "upload-all", "--studies", "ausdiab", "--env", "prod",
         "--allow-prod"],
        input="prod\n",
    )
    assert result.exit_code == 0, result.output
    argv = _argv(mock_run)
    assert "--allow-prod" in argv
    i = argv.index("--studies")
    assert argv[i + 1] == "ausdiab_prod"


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.metadata.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_metadata_upload_all_prod_mismatched_confirmation_aborts(
    mock_run, _study, _env
):
    """
    Inputs:  --env prod --allow-prod, but 'staging' typed at the prompt
    Expected Output: non-zero exit, nothing dispatched.
    """
    result = runner.invoke(
        app,
        ["metadata", "upload-all", "--studies", "ausdiab", "--env", "prod",
         "--allow-prod"],
        input="staging\n",
    )
    assert result.exit_code != 0
    mock_run.assert_not_called()


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.metadata.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_metadata_upload_all_prod_study_key_trips_the_gate(mock_run, _study, _env):
    """
    Background:
        The env alone does not determine where the write lands: a study key
        can resolve to another environment's commons. `--env staging
        --studies ausdiab_prod` is a production write and must be gated
        exactly like --env prod.

    Inputs:  --env staging with a study whose resolved key contains 'prod'
    Expected Output: exit 2 without --allow-prod.
    """
    result = runner.invoke(
        app,
        ["metadata", "upload-all", "--studies", "ausdiab_prod",
         "--env", "staging"],
    )
    assert result.exit_code == 2
    mock_run.assert_not_called()


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


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_per_study_versions_build_qualified_spec(
    mock_run, _study, _env
):
    """
    Background:
        Release ladders diverge per study in practice (one study retiring
        0.7.5 while another retires 0.8.1), so one job must be able to carry
        a different version per study instead of one job per study.

    Inputs:  --studies "ausdiab:0.7.5,caughtcad:0.8.1" (no --version)
    Expected Output: the wrapper receives the qualified key:version list and
    NO --version flag.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab:0.7.5,caughtcad:0.8.1",
         "--env", "staging", "--yes"],
    )
    assert result.exit_code == 0, result.output
    argv = _argv(mock_run)
    i = argv.index("--studies")
    assert argv[i + 1] == "ausdiab_staging:0.7.5,caughtcad_staging:0.8.1"
    assert "--version" not in argv


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_uniform_versions_collapse_to_legacy_argv(
    mock_run, _study, _env
):
    """
    Background:
        When every study lands on the same version — every invocation that
        existed before per-study specs — the wrapper must receive the
        historical `--studies a,b --version X` shape, byte-identical, so a
        newer CLI stays compatible with an older installed service script.

    Inputs:  --studies "ausdiab:0.9.8,caughtcad" --version 0.9.8
    Expected Output: plain key list + one --version 0.9.8, no colons.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab:0.9.8,caughtcad",
         "--env", "staging", "--version", "0.9.8", "--yes"],
    )
    assert result.exit_code == 0, result.output
    argv = _argv(mock_run)
    i = argv.index("--studies")
    assert argv[i + 1] == "ausdiab_staging,caughtcad_staging"
    assert argv[argv.index("--version") + 1] == "0.9.8"


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_bare_study_without_version_is_usage_error(
    mock_run, _study, _env
):
    """
    Background:
        The whole list is validated before anything is dispatched — a typo in
        the last study must not leave the earlier ones already deleted.

    Inputs:  --studies "ausdiab:0.7.5,caughtcad" with NO --version
    Expected Output: exit 2 naming the bare study, nothing dispatched.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab:0.7.5,caughtcad",
         "--env", "staging", "--yes"],
    )
    assert result.exit_code == 2
    mock_run.assert_not_called()
    assert "caughtcad" in result.output


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_rejects_colon_with_no_version(mock_run, _study, _env):
    """
    Background:
        A trailing colon is a half-finished edit, not a request for the
        default. Silently falling back to --version would delete a version
        the operator did not name — partition(':') makes the two cases
        distinguishable.

    Inputs:  --studies "ausdiab:" --version 0.9.8
    Expected Output: exit 2, nothing dispatched.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab:",
         "--env", "staging", "--version", "0.9.8", "--yes"],
    )
    assert result.exit_code == 2
    mock_run.assert_not_called()


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_mixed_all_and_specific_always_prompts(
    mock_run, _study, _env
):
    """
    Background:
        Deleting ALL versions is the most destructive path and always
        prompts, even with --yes. One 'all' hidden mid-list must force the
        same prompt — otherwise it rides along on a batch marked unattended.

    Inputs:  --studies "ausdiab:0.9.8,caughtcad:all" --yes, prompt declined
    Expected Output: non-zero exit, nothing dispatched.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab:0.9.8,caughtcad:all",
         "--env", "staging", "--yes"],
        input="n\n",
    )
    assert result.exit_code != 0
    mock_run.assert_not_called()


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_strips_leading_v_from_version(mock_run, _study, _env):
    """
    Background:
        Athena stores the release version without a leading 'v' — the uploader
        parses it out of the S3 path and keeps only group(1) of ^v?(x.y.z)$.
        The delete query interpolates the operator's string literally, so a
        'v'-prefixed version matches zero rows; the bulk wrapper then reports
        exit 3 as "skipped" and the run looks clean while deleting nothing.
        Normalising in the CLI closes that silent no-op.

    Inputs:  --version V0.9.8 (case and prefix both wrong)
    Expected Output: the wrapper receives exactly 0.9.8.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab",
         "--env", "staging", "--version", "V0.9.8", "--yes"],
    )
    assert result.exit_code == 0, result.output
    argv = _argv(mock_run)
    assert argv[argv.index("--version") + 1] == "0.9.8"


@patch("g3dt.cli._internal.dispatch.resolve_env", side_effect=_env_cfg)
@patch("g3dt.cli.delete_cmds.study_of", side_effect=_study_cfg)
@patch("g3dt.cli._internal.runner.run")
def test_delete_metadata_rejects_malformed_version(mock_run, _study, _env):
    """
    Background:
        The upload path only ever writes three-part semver into the version
        column, so a truncated version like 0.9 can never match a row. Left
        unchecked it deletes nothing and is counted as a skip — invisible to
        the operator. Rejecting it loudly refuses no valid input.

    Inputs:  --version 0.9
    Expected Output: exit 2, nothing dispatched.
    """
    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab",
         "--env", "staging", "--version", "0.9", "--yes"],
    )
    assert result.exit_code == 2
    mock_run.assert_not_called()
    assert "Invalid version" in result.output


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
