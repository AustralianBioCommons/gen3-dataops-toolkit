"""Tests for EC2 dispatch via SSM (moto for SSM resolution; send_command mocked).

The dispatch design under test:
  * every name — instance id, log bucket/prefix, CloudWatch group — comes from
    the env's own SSM tree (``/{project}/{env}/...``), never from local config;
  * the auth split holds: the laptop authenticates with the *local* env's
    named profile, while the remote job runs under the ``*_ec2`` pseudo-env
    (ambient instance profile);
  * the remote command is a bare ``g3dt ...`` console-script invocation — no
    repo clone, no ``git pull``, no poetry — because the box is provisioned by
    CDK user-data with the toolkit pre-installed.
"""
import textwrap
from unittest.mock import MagicMock, patch

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from g3dt import config, resolver
from g3dt.cli.main import app

runner = CliRunner()

REGION = "ap-southeast-2"

_MARKER_YAML = """
project: etl
region: ap-southeast-2
profiles:
  staging: etl_staging
studies:
  ausdiab_staging:
    project_id: AusDiab
    program_id: program1
    s3_metadata_path: s3://b/staging/ausdiab/
"""


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Hermetic marker + fake AWS profile + clean caches for every test.

    The marker's ``profiles: {staging: etl_staging}`` requires that named
    profile to exist, so a scratch credentials file defines it — moto then
    intercepts every API call regardless of profile.
    """
    marker = tmp_path / "g3dt.yaml"
    marker.write_text(textwrap.dedent(_MARKER_YAML))
    monkeypatch.setenv("G3DT_MARKER", str(marker))

    creds = tmp_path / "aws_credentials"
    creds.write_text(
        "[etl_staging]\naws_access_key_id = testing\naws_secret_access_key = testing\n"
    )
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "aws_config"))
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)

    config._load_yaml_cached.cache_clear()
    resolver.resolve.cache_clear()
    yield
    config._load_yaml_cached.cache_clear()
    resolver.resolve.cache_clear()


def _seed_env(instance_id="i-0abc123"):
    """Publish the /etl/staging tree (app facts + ec2 leaves) into mocked SSM."""
    ssm = boto3.client("ssm", region_name=REGION)
    leaves = {
        "meta/region": REGION,
        "app/dictionary_version": "v1",
        "app/aws_secret_name": "sec",
        "app/schema_s3_uri": "u",
        "app/domain": "d",
        "app/app_name": "a",
        "app/namespace": "n",
        "app/cluster_name": "c",
        "app/schema_repo": "Org/schema-repo",
        "ec2/logGroup": "/etl/staging/ec2/jobs",
        "ec2/logBucket": "etl-staging-metadata-1-x",
        "ec2/logPrefix": "ec2-job-logs",
    }
    if instance_id:
        leaves["ec2/instanceId"] = instance_id
    for rel, value in leaves.items():
        ssm.put_parameter(Name=f"/etl/staging/{rel}", Value=value, Type="String")


@mock_aws
@patch("g3dt.cli._internal.registry.record")
@patch("g3dt.cli._internal.dispatch.create_boto3_session")
def test_metadata_upload_on_ec2_sends_ssm_command(mock_session, mock_record):
    """
    Inputs:  g3dt metadata upload --study ausdiab --env staging --on ec2
    Expected Output:
      - exit code 0
      - the SSM client is built with the LOCAL profile 'etl_staging' (auth split)
      - send_command targets the instance id from SSM ec2/instanceId
      - S3 output + CloudWatch group come from SSM ec2/logBucket + ec2/logGroup
      - the remote command is a bare `g3dt metadata upload ... --env staging_ec2`
        (no git pull, no poetry, no repo path) teeing to ~/.g3dt/logs/
      - the run is recorded in the registry
    """
    _seed_env()
    ssm = MagicMock()
    ssm.send_command.return_value = {"Command": {"CommandId": "cmd-xyz"}}
    session = MagicMock()
    session.client.return_value = ssm
    mock_session.return_value = session

    result = runner.invoke(
        app,
        ["metadata", "upload", "--study", "ausdiab", "--env", "staging",
         "--on", "ec2"],
    )
    assert result.exit_code == 0, result.output

    assert mock_session.call_args.kwargs.get("aws_profile") == "etl_staging"

    send_kwargs = ssm.send_command.call_args.kwargs
    assert send_kwargs["InstanceIds"] == ["i-0abc123"]
    assert send_kwargs["DocumentName"] == "AWS-RunShellScript"
    assert send_kwargs["OutputS3BucketName"] == "etl-staging-metadata-1-x"
    assert send_kwargs["OutputS3KeyPrefix"].startswith("ec2-job-logs/")
    assert (
        send_kwargs["CloudWatchOutputConfig"]["CloudWatchLogGroupName"]
        == "/etl/staging/ec2/jobs"
    )
    command = send_kwargs["Parameters"]["commands"][0]
    assert "g3dt metadata upload" in command
    assert "--study ausdiab" in command
    assert "--env staging_ec2" in command
    assert "git pull" not in command
    assert "poetry" not in command
    assert "~/.g3dt/logs" in command

    mock_record.assert_called_once()
    rec_kwargs = mock_record.call_args.kwargs
    assert rec_kwargs["instance_id"] == "i-0abc123"
    assert rec_kwargs["cw_log_group"] == "/etl/staging/ec2/jobs"


@mock_aws
@patch("g3dt.cli._internal.registry.record")
@patch("g3dt.cli._internal.dispatch.create_boto3_session")
def test_delete_metadata_on_ec2_sends_ssm_command(mock_session, mock_record):
    """
    Inputs:  g3dt delete metadata --studies ausdiab --env staging
             --version 0.9.8 --on ec2 --yes
    Expected Output:
      - exit code 0 (non-prod specific-version delete; --yes skips the prompt)
      - the remote command re-invokes the CLI (`g3dt delete metadata ...`) with
        --env staging_ec2 and --yes (confirmation already happened locally —
        SSM has no TTY)
    """
    _seed_env()
    ssm = MagicMock()
    ssm.send_command.return_value = {"Command": {"CommandId": "cmd-del"}}
    session = MagicMock()
    session.client.return_value = ssm
    mock_session.return_value = session

    result = runner.invoke(
        app,
        ["delete", "metadata", "--studies", "ausdiab", "--env", "staging",
         "--version", "0.9.8", "--on", "ec2", "--yes"],
    )
    assert result.exit_code == 0, result.output

    send_kwargs = ssm.send_command.call_args.kwargs
    assert send_kwargs["InstanceIds"] == ["i-0abc123"]
    command = send_kwargs["Parameters"]["commands"][0]
    assert "g3dt delete metadata" in command
    assert "--studies ausdiab" in command
    assert "--version 0.9.8" in command
    assert "--env staging_ec2" in command
    assert "--yes" in command
    mock_record.assert_called_once()


@mock_aws
@patch("g3dt.cli._internal.dispatch.create_boto3_session")
def test_on_ec2_without_instance_id_errors(mock_session):
    """
    Inputs:  --on ec2 when the env's tree has no ec2/instanceId (job-runner
             stack not deployed)
    Expected Output: exit code 2 with a deploy hint; no SSM send_command.
    """
    _seed_env(instance_id=None)
    ssm = MagicMock()
    session = MagicMock()
    session.client.return_value = ssm
    mock_session.return_value = session

    result = runner.invoke(
        app,
        ["metadata", "upload", "--study", "ausdiab", "--env", "staging",
         "--on", "ec2"],
    )
    assert result.exit_code == 2
    ssm.send_command.assert_not_called()


@patch("g3dt.cli._internal.registry.all_runs")
@patch("g3dt.cli._internal.dispatch.create_boto3_session")
def test_jobs_status_lists_all_runs_with_status(mock_session, mock_all_runs):
    """
    Inputs:  g3dt jobs status   (no run id)

    Should give an at-a-glance overview of every recorded run, querying SSM for
    each one's live status, so an operator can see what succeeded/failed
    without remembering individual run ids.

    Expected Output:
      - exit code 0
      - each run listed with its live status (cmd-success -> Success,
        cmd-failed -> Failed); a record with no command id shows 'n/a'
    """
    mock_all_runs.return_value = {
        "20260101T000000-metadata-upload": {
            "run_id": "20260101T000000-metadata-upload",
            "command_id": "cmd-success", "instance_id": "i-0abc123",
            "env": "staging_ec2", "mechanism": "ssm",
        },
        "20260102T000000-metadata-delete": {
            "run_id": "20260102T000000-metadata-delete",
            "command_id": "cmd-failed", "instance_id": "i-0abc123",
            "env": "staging_ec2", "mechanism": "ssm",
        },
        "20260103T000000-indexd-register": {
            "run_id": "20260103T000000-indexd-register",
            "command_id": None, "instance_id": "i-0abc123",
            "env": "staging_ec2", "mechanism": "ssm",
        },
    }

    statuses = {"cmd-success": "Success", "cmd-failed": "Failed"}
    ssm = MagicMock()
    ssm.get_command_invocation.side_effect = (
        lambda CommandId, InstanceId: {"Status": statuses[CommandId]}
    )
    session = MagicMock()
    session.client.return_value = ssm
    mock_session.return_value = session

    result = runner.invoke(app, ["jobs", "status"])
    assert result.exit_code == 0, result.output

    assert "20260101T000000-metadata-upload" in result.output
    assert "Success" in result.output
    assert "20260102T000000-metadata-delete" in result.output
    assert "Failed" in result.output
    assert "n/a" in result.output
    assert ssm.get_command_invocation.call_count == 2


@patch("g3dt.cli._internal.registry.get")
@patch("g3dt.cli._internal.dispatch.create_boto3_session")
def test_jobs_stop_cancels_in_progress_run(mock_session, mock_get):
    """
    Inputs:  g3dt jobs stop <run_id>   (run is InProgress)
    Expected Output: exit 0; cancel_command called once with the run's exact
    command id and instance id.
    """
    mock_get.return_value = {
        "run_id": "20260101T000000-metadata-upload",
        "command_id": "cmd-1", "instance_id": "i-0abc123",
        "env": "staging_ec2", "mechanism": "ssm",
    }
    ssm = MagicMock()
    ssm.get_command_invocation.return_value = {"Status": "InProgress"}
    session = MagicMock()
    session.client.return_value = ssm
    mock_session.return_value = session

    result = runner.invoke(app, ["jobs", "stop", "20260101T000000-metadata-upload"])
    assert result.exit_code == 0, result.output
    ssm.cancel_command.assert_called_once_with(
        CommandId="cmd-1", InstanceIds=["i-0abc123"]
    )


@patch("g3dt.cli._internal.registry.get")
@patch("g3dt.cli._internal.dispatch.create_boto3_session")
def test_jobs_stop_skips_finished_run(mock_session, mock_get):
    """
    Inputs:  g3dt jobs stop <run_id>   (run already Succeeded)
    Expected Output: exit 0, "already finished", and no cancel_command call —
    cancelling a finished run would be a pointless, confusing API call.
    """
    mock_get.return_value = {
        "run_id": "r", "command_id": "cmd-1", "instance_id": "i-0abc123",
        "env": "staging_ec2", "mechanism": "ssm",
    }
    ssm = MagicMock()
    ssm.get_command_invocation.return_value = {"Status": "Success"}
    session = MagicMock()
    session.client.return_value = ssm
    mock_session.return_value = session

    result = runner.invoke(app, ["jobs", "stop", "r"])
    assert result.exit_code == 0, result.output
    assert "already finished" in result.output
    ssm.cancel_command.assert_not_called()


@patch("g3dt.cli._internal.registry.get")
@patch("g3dt.cli._internal.dispatch.create_boto3_session")
def test_jobs_stop_rejects_run_without_command_id(mock_session, mock_get):
    """
    Inputs:  g3dt jobs stop <run_id> for a record with no SSM command id
    Expected Output: exit 1 and no cancel call — there is nothing to cancel,
    and pretending otherwise would hide the real state from the operator.
    """
    mock_get.return_value = {
        "run_id": "r", "command_id": None, "instance_id": "host",
        "env": "staging_ec2", "mechanism": "ssm",
    }
    ssm = MagicMock()
    session = MagicMock()
    session.client.return_value = ssm
    mock_session.return_value = session

    result = runner.invoke(app, ["jobs", "stop", "r"])
    assert result.exit_code == 1
    ssm.cancel_command.assert_not_called()
