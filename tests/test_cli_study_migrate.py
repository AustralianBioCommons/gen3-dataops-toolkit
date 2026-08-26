"""Tests for ``g3dt study migrate`` — the one-shot legacy-registry import.

Background: migrate is the bridge off the legacy ``s3://<metadata-bucket>/
config/studies.yaml`` and it runs against a LIVE environment exactly once,
so its failure modes matter more than its happy path: a conflict or a
malformed entry must write NOTHING (an operator retries after fixing, never
untangles a half-import), and the legacy file must survive until every
written record has been re-read and verified — if the file were renamed
first and a write had failed, older toolkits on the EC2 box would lose
their registry mid-transition.

Moto provides SSM + S3; the verify-failure case stubs the writer to model
"SSM accepted the call but the record never landed".
"""
import json
import textwrap
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from g3dt import config
from g3dt.cli.main import app

runner = CliRunner()

REGION = "ap-southeast-2"
META = "etl-meta"

_LEGACY_YAML = """
studies:
  ausdiab_staging:
    project_id: AusDiab
    program_id: program1
    s3_metadata_path: s3://b/release_jsons/v2.0.0/ausdiab/
  cdah_staging:
    project_id: CDAH
    program_id: program1
    s3_metadata_path: s3://b/release_jsons/v2.0.0/cdah/
"""


@pytest.fixture(autouse=True)
def _region(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _seed(yaml_body=_LEGACY_YAML):
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(Name="/etl/staging/meta/region", Value=REGION, Type="String")
    ssm.put_parameter(
        Name="/etl/staging/buckets/metadata", Value=META, Type="String"
    )
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(
        Bucket=META, CreateBucketConfiguration={"LocationConstraint": REGION}
    )
    if yaml_body is not None:
        s3.put_object(
            Bucket=META,
            Key=config.STUDIES_S3_KEY,
            Body=textwrap.dedent(yaml_body).encode(),
        )


def _s3_keys():
    resp = boto3.client("s3", region_name=REGION).list_objects_v2(Bucket=META)
    return sorted(o["Key"] for o in resp.get("Contents", []))


def _ssm_study(name):
    return json.loads(
        boto3.client("ssm", region_name=REGION).get_parameter(
            Name=f"/etl/staging/studies/{name}"
        )["Parameter"]["Value"]
    )


def _migrate(*extra, input=None):
    return runner.invoke(
        app, ["study", "migrate", "--env", "staging", *extra], input=input
    )


@mock_aws
def test_migrate_happy_imports_strips_suffix_and_renames():
    """
    Inputs:  a legacy file with two {study}_staging entries
    Expected: bare-named SSM records, the file renamed to .migrated (the
              proof SSM is now the only source), and a summary naming the
              counts.
    """
    _seed()
    result = _migrate()
    assert result.exit_code == 0, result.output
    assert _ssm_study("ausdiab")["project_id"] == "AusDiab"
    assert _ssm_study("cdah")["project_id"] == "CDAH"
    assert _s3_keys() == ["config/studies.yaml.migrated"]
    assert "2 imported" in result.stdout


@mock_aws
def test_migrate_idempotent_after_success():
    """
    Inputs:  a second run after a successful migrate
    Expected: exit 0 with 'Already migrated' — reruns are safe and honest.
    """
    _seed()
    assert _migrate().exit_code == 0
    result = _migrate()
    assert result.exit_code == 0, result.output
    assert "Already migrated" in result.stdout


@mock_aws
def test_migrate_no_file_clear_message_exit_zero():
    """
    Inputs:  an env that never had a studies.yaml (fresh project)
    Expected: exit 0 pointing at `g3dt study add` — nothing to migrate is a
              state, not an error.
    """
    _seed(yaml_body=None)
    result = _migrate()
    assert result.exit_code == 0, result.output
    assert "nothing to migrate" in result.stdout
    assert "g3dt study add" in result.stdout


@mock_aws
def test_migrate_conflict_refuses_and_writes_nothing():
    """
    Inputs:  SSM already holds 'cdah' with a DIFFERENT path than the file
    Expected: exit 1 with a per-field diff and --force named; neither the
              conflicting record nor the clean 'ausdiab' import is written
              (all-or-nothing keeps a retry simple).
    """
    _seed()
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name="/etl/staging/studies/cdah",
        Value=json.dumps(
            {
                "project_id": "CDAH",
                "program_id": "program1",
                "s3_metadata_path": "s3://b/release_jsons/v9.9.9/cdah/",
            }
        ),
        Type="String",
    )
    result = _migrate()
    assert result.exit_code == 1
    assert "--force" in result.stderr
    assert "v9.9.9" in result.stderr and "v2.0.0" in result.stderr
    ssm = boto3.client("ssm", region_name=REGION)
    with pytest.raises(ssm.exceptions.ParameterNotFound):
        ssm.get_parameter(Name="/etl/staging/studies/ausdiab")
    assert "config/studies.yaml" in _s3_keys()  # file untouched


@mock_aws
def test_migrate_force_overwrites_conflicts():
    _seed()
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name="/etl/staging/studies/cdah",
        Value=json.dumps(
            {
                "project_id": "CDAH",
                "program_id": "program1",
                "s3_metadata_path": "s3://b/release_jsons/v9.9.9/cdah/",
            }
        ),
        Type="String",
    )
    result = _migrate("--force")
    assert result.exit_code == 0, result.output
    assert "v2.0.0" in _ssm_study("cdah")["s3_metadata_path"]
    assert "1 overwritten" in result.stdout


@mock_aws
def test_migrate_dry_run_no_writes_no_rename():
    _seed()
    result = _migrate("--dry-run")
    assert result.exit_code == 0, result.output
    assert "Dry run" in result.stdout
    ssm = boto3.client("ssm", region_name=REGION)
    with pytest.raises(ssm.exceptions.ParameterNotFound):
        ssm.get_parameter(Name="/etl/staging/studies/ausdiab")
    assert "config/studies.yaml" in _s3_keys()


@mock_aws
def test_migrate_malformed_entry_reports_all_writes_nothing():
    """
    Inputs:  one entry missing s3_metadata_path
    Expected: exit 1 naming the entry and the missing field; the clean
              entry is NOT imported either.
    """
    _seed(
        """
        studies:
          ausdiab_staging:
            project_id: AusDiab
            program_id: program1
            s3_metadata_path: s3://b/release_jsons/v2.0.0/ausdiab/
          broken_staging:
            project_id: Broken
            program_id: program1
        """
    )
    result = _migrate()
    assert result.exit_code == 1
    assert "broken_staging" in result.stderr
    assert "s3_metadata_path" in result.stderr
    ssm = boto3.client("ssm", region_name=REGION)
    with pytest.raises(ssm.exceptions.ParameterNotFound):
        ssm.get_parameter(Name="/etl/staging/studies/ausdiab")


@mock_aws
def test_migrate_verify_failure_keeps_s3_source():
    """
    Inputs:  the writer silently drops writes (modelling an SSM write that
             was accepted but never landed)
    Expected: exit 1 after the re-read verification, and the legacy file is
              STILL in place — older toolkits keep resolving while the
              operator investigates.
    """
    _seed()
    with patch("g3dt.studies.put_study", return_value=None):
        result = _migrate()
    assert result.exit_code == 1
    assert "verification failed" in result.stderr.lower()
    assert "config/studies.yaml" in _s3_keys()
