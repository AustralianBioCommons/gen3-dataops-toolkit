"""Tests for `g3dt pipeline status` / `logs` (SSM names, mocked AWS clients).

Why this matters: these commands are the operator's only non-console window
into a running data release. They must find the pipeline/CodeBuild names in
SSM (never ask for an ARN) and target exactly the latest build's log stream.
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
"""


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    marker = tmp_path / "g3dt.yaml"
    marker.write_text(textwrap.dedent(_MARKER_YAML))
    monkeypatch.setenv("G3DT_MARKER", str(marker))
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("G3DT_PROJECT", raising=False)
    config._load_yaml_cached.cache_clear()
    resolver.resolve.cache_clear()
    yield
    config._load_yaml_cached.cache_clear()
    resolver.resolve.cache_clear()


def _seed():
    ssm = boto3.client("ssm", region_name=REGION)
    leaves = {
        "meta/region": REGION,
        "codepipeline/writeReleaseInfo": "etl-test-dbt-write-release-info",
        "codepipeline/dbtTestAndRun": "etl-test-dbt-test-and-run",
        "codebuild/dbtReleaseBuilder": "etl-test-dbt-release-builder",
        "codebuild/dbtTestAndRun": "etl-test-dbt-test-and-run",
    }
    for rel, value in leaves.items():
        ssm.put_parameter(Name=f"/etl/test/{rel}", Value=value, Type="String")


@mock_aws
@patch("g3dt.cli.pipeline_cmds.create_boto3_session")
def test_pipeline_status_reads_name_from_ssm(mock_session):
    """
    Inputs:  g3dt pipeline status --env test   (default --which writeReleaseInfo)
    Expected Output: get_pipeline_state called with the SSM-resolved pipeline
    name, and each stage printed with its latest status.
    """
    _seed()
    cp = MagicMock()
    cp.get_pipeline_state.return_value = {
        "stageStates": [
            {"stageName": "Source", "latestExecution": {"status": "Succeeded"}},
            {"stageName": "Build", "latestExecution": {"status": "InProgress"}},
        ]
    }
    session = MagicMock()
    session.client.return_value = cp
    mock_session.return_value = session

    result = runner.invoke(app, ["pipeline", "status", "--env", "test"])
    assert result.exit_code == 0, result.output
    cp.get_pipeline_state.assert_called_once_with(name="etl-test-dbt-write-release-info")
    assert "Source" in result.output and "Succeeded" in result.output
    assert "Build" in result.output and "InProgress" in result.output


@mock_aws
@patch("g3dt.cli.pipeline_cmds.create_boto3_session")
def test_pipeline_logs_targets_latest_build_stream(mock_session):
    """
    Inputs:  g3dt pipeline logs --env test   (default --which dbtReleaseBuilder)
    Expected Output: the newest build id's uuid is used as the stream prefix in
    /aws/codebuild/<project>, and its events are printed once (de-duped).
    """
    _seed()
    cb = MagicMock()
    cb.list_builds_for_project.return_value = {
        "ids": ["etl-test-dbt-release-builder:uuid-123"]
    }
    logs_client = MagicMock()
    logs_client.filter_log_events.return_value = {
        "events": [
            {"eventId": "1", "timestamp": 1, "message": "dbt build started\n"},
            {"eventId": "2", "timestamp": 2, "message": "[OK] Inserted release row\n"},
        ]
    }
    logs_client.exceptions.ResourceNotFoundException = Exception

    def client(service):
        return {"codebuild": cb, "logs": logs_client}[service]

    session = MagicMock()
    session.client.side_effect = client
    mock_session.return_value = session

    result = runner.invoke(app, ["pipeline", "logs", "--env", "test"])
    assert result.exit_code == 0, result.output
    kwargs = logs_client.filter_log_events.call_args.kwargs
    assert kwargs["logGroupName"] == "/aws/codebuild/etl-test-dbt-release-builder"
    assert kwargs["logStreamNamePrefix"] == "uuid-123"
    assert "dbt build started" in result.output
    assert "[OK] Inserted release row" in result.output


@mock_aws
@patch("g3dt.cli.pipeline_cmds.create_boto3_session")
def test_pipeline_logs_no_builds_yet_is_friendly(mock_session):
    """A project with no builds prints a note instead of crashing."""
    _seed()
    cb = MagicMock()
    cb.list_builds_for_project.return_value = {"ids": []}
    session = MagicMock()
    session.client.return_value = cb
    mock_session.return_value = session

    result = runner.invoke(app, ["pipeline", "logs", "--env", "test"])
    assert result.exit_code == 0, result.output
    assert "No builds yet" in result.output


@mock_aws
@patch("g3dt.cli.pipeline_cmds.create_boto3_session")
def test_pipeline_status_rejects_unknown_which(mock_session):
    """--which not in SSM exits 2 and names the valid values."""
    _seed()
    result = runner.invoke(
        app, ["pipeline", "status", "--env", "test", "--which", "nope"]
    )
    assert result.exit_code == 2
    assert "writeReleaseInfo" in result.output
