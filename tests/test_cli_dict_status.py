"""`g3dt dict status`: the deployed dictionary version next to the declared one.

Background: `dict deploy --version <tag>` deliberately lets one tag be
promoted through environments without a `cdk deploy` per env, but that
leaves SSM (and `config show`) declaring one version while the S3 object the
services read carries another. Nothing in the CLI could show that gap;
`config diff` compares SSM with the wrapper's JSON, not with S3. `dict
status` reads the `version` stamp `dict upload` puts on the object.

Moto provides SSM (the env tree) and S3 (the schema object).
"""
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from g3dt.cli.main import app

runner = CliRunner()

REGION = "ap-southeast-2"
BUCKET = "gen3schema-example"
KEY = "cad.json"


@pytest.fixture(autouse=True)
def _region(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _seed(declared="v1.2.0"):
    ssm = boto3.client("ssm", region_name=REGION)
    leaves = {
        "meta/region": REGION,
        "buckets/metadata": "etl-meta",
        "app/dictionary_version": declared,
        "app/aws_secret_name": "sec",
        "app/schema_s3_uri": f"{BUCKET}/{KEY}",
        "app/domain": "d",
        "app/app_name": "a",
        "app/namespace": "n",
        "app/cluster_name": "c",
        "app/schema_repo": "Org/schema-repo",
    }
    for rel, value in leaves.items():
        ssm.put_parameter(Name=f"/etl/staging/{rel}", Value=value, Type="String")
    boto3.client("s3", region_name=REGION).create_bucket(
        Bucket=BUCKET, CreateBucketConfiguration={"LocationConstraint": REGION}
    )


def _put_schema(version):
    boto3.client("s3", region_name=REGION).put_object(
        Bucket=BUCKET, Key=KEY, Body=b"{}", Metadata={"version": version}
    )


@mock_aws
def test_in_sync_when_stamp_matches_declared():
    """
    Inputs:  SSM declares v1.2.0; the S3 object is stamped version=v1.2.0
    Expected: both versions printed and 'in sync'; exit 0 even with --strict.
    """
    _seed("v1.2.0")
    _put_schema("v1.2.0")
    result = runner.invoke(app, ["dict", "status", "--env", "staging", "--strict"])
    assert result.exit_code == 0, result.output
    assert "declared : v1.2.0" in result.stdout
    assert "deployed : v1.2.0" in result.stdout
    assert "in sync" in result.stdout


@mock_aws
def test_drift_is_reported_and_strict_exits_one():
    """
    Inputs:  SSM declares v1.2.0; S3 carries v1.2.1 (a `dict deploy --version`
             promotion the CDK config has not caught up with)
    Expected: DRIFT with both versions named; exit 0 by default, exit 1 with
              --strict so scripts can gate on it.
    """
    _seed("v1.2.0")
    _put_schema("v1.2.1")
    result = runner.invoke(app, ["dict", "status", "--env", "staging"])
    assert result.exit_code == 0, result.output
    assert "DRIFT" in result.stdout
    assert "v1.2.1" in result.stdout and "v1.2.0" in result.stdout

    result = runner.invoke(app, ["dict", "status", "--env", "staging", "--strict"])
    assert result.exit_code == 1


@mock_aws
def test_missing_object_says_so_instead_of_crashing():
    """
    Inputs:  the env tree exists but nothing was ever uploaded to schema_s3_uri
    Expected: 'deployed' explains the object is missing and names the fix
              (`g3dt dict deploy`); exit 0 without --strict.
    """
    _seed("v1.2.0")
    result = runner.invoke(app, ["dict", "status", "--env", "staging"])
    assert result.exit_code == 0, result.output
    assert "nothing at s3://" in result.stdout
    assert "g3dt dict deploy" in result.stdout


@mock_aws
def test_stamp_without_v_prefix_is_in_sync_with_the_declared_tag():
    """
    Background: on ACDC staging (2026-09-08) the S3 object was stamped
    `1.2.0` — the JSON's _settings _dict_version has no `v` — while SSM
    declares the git tag `v1.2.0`. The first cut of `dict status` called that
    DRIFT. They are the same version and must compare equal.

    Inputs:  SSM declares v1.2.0; the S3 object is stamped version=1.2.0
    Expected: 'in sync', exit 0 with --strict.
    """
    _seed("v1.2.0")
    _put_schema("1.2.0")
    result = runner.invoke(app, ["dict", "status", "--env", "staging", "--strict"])
    assert result.exit_code == 0, result.output
    assert "in sync" in result.stdout
    assert "DRIFT" not in result.stdout
