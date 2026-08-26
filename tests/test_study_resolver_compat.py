"""Resolver compatibility + error-path tests for the 4.1.0 study registry.

Background: 4.1.0 moves the registry from ``s3://<metadata-bucket>/config/
studies.yaml`` (and a marker ``studies:`` block) into per-study SSM
parameters. Three things keep the transition safe and honest, and each is
pinned here:

* the S3 fallback still resolves (an EC2 box on an older toolkit keeps
  working until migrate + pin bump), but warns DEPRECATED exactly once;
* the marker block is ignored — silently shadowing the real registry was
  the old design's worst trap — with a one-time migration notice;
* failures guide: an unknown study lists what IS registered, an empty
  registry prints the exact setup commands, and an auth failure PROPAGATES
  instead of masquerading as "no studies configured" (the live 4.0.1
  incident that motivated this feature).

Moto provides SSM and S3; nothing is hand-mocked except the auth failure.
"""
import textwrap
from unittest.mock import patch

import boto3
import pytest
from botocore.exceptions import ClientError
from moto import mock_aws

from g3dt import config, studies

REGION = "ap-southeast-2"

_LEGACY_YAML = """
studies:
  ausdiab_staging:
    project_id: AusDiab
    program_id: program1
    s3_metadata_path: s3://b/release_jsons/v2.0.0/ausdiab/
  ausdiab_prod:
    project_id: AusDiab
    program_id: program1
    s3_metadata_path: s3://b/release_jsons/v1.5.0/ausdiab/
"""


@pytest.fixture(autouse=True)
def _region(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _seed_tree(with_bucket=True):
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(
        Name="/etl/staging/meta/region", Value=REGION, Type="String"
    )
    if with_bucket:
        ssm.put_parameter(
            Name="/etl/staging/buckets/metadata", Value="etl-meta", Type="String"
        )


def _seed_legacy_yaml():
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(
        Bucket="etl-meta",
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    s3.put_object(
        Bucket="etl-meta",
        Key=config.STUDIES_S3_KEY,
        Body=textwrap.dedent(_LEGACY_YAML).encode(),
    )


def _seed_ssm_study(name, path):
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name=f"/etl/staging/studies/{name}",
        Value=(
            '{"project_id": "AusDiab", "program_id": "program1", '
            f'"s3_metadata_path": "{path}"}}'
        ),
        Type="String",
    )


@mock_aws
def test_s3_fallback_when_subtree_empty(capsys):
    """
    Inputs:  no studies/ params, a legacy studies.yaml with suffixed keys
    Expected: 'ausdiab' resolves via the fallback with a BARE key and the
              staging path; the other-env key 'ausdiab_prod' stays suffixed
              (it must never silently resolve into staging).
    """
    _seed_tree()
    _seed_legacy_yaml()
    s = config.resolve_study("ausdiab", "staging")
    assert s.key == "ausdiab"
    assert "v2.0.0" in s.s3_metadata_path
    names = config.list_studies(env="staging")
    assert names == ["ausdiab", "ausdiab_prod"]


@mock_aws
def test_s3_fallback_deprecation_warns_once(capsys):
    """
    Inputs:  two resolves through the fallback in one process
    Expected: exactly ONE DEPRECATED line naming `g3dt study migrate` —
              informative, not nagging on every call of a bulk run.
    """
    _seed_tree()
    _seed_legacy_yaml()
    config.resolve_study("ausdiab", "staging")
    config.resolve_study("ausdiab", "staging")
    err = capsys.readouterr().err
    assert err.count("DEPRECATED") == 1
    assert "g3dt study migrate" in err


@mock_aws
def test_marker_block_ignored_with_migrate_notice(capsys, monkeypatch, tmp_path):
    """
    Inputs:  a marker with the old studies: block naming 'markeronly';
             the SSM registry holds only 'cdah'
    Expected: 'markeronly' does NOT resolve (the block is dead), a one-time
              notice explains where the registry went, and the SSM study
              still resolves normally.
    """
    marker = tmp_path / "g3dt.yaml"
    marker.write_text(
        textwrap.dedent(
            """
            project: etl
            region: ap-southeast-2
            studies:
              markeronly:
                project_id: X
                program_id: program1
                s3_metadata_path: s3://b/x/
            """
        )
    )
    monkeypatch.setenv("G3DT_MARKER", str(marker))
    config._load_yaml_cached.cache_clear()
    _seed_tree()
    _seed_ssm_study("cdah", "s3://b/release_jsons/v2.0.0/cdah/")

    assert config.resolve_study("cdah", "staging").key == "cdah"
    with pytest.raises(config.ConfigError):
        config.resolve_study("markeronly", "staging")
    err = capsys.readouterr().err
    assert err.count("studies: block is no longer read") == 1


@mock_aws
def test_auth_failure_not_swallowed():
    """
    Inputs:  the SSM read fails with AccessDenied (expired SSO, bad role)
    Expected: the error PROPAGATES. The 4.0.1 behaviour — swallowing it and
              reporting 'no studies configured' — sent operators debugging
              their registry instead of their credentials.
    """
    _seed_tree()
    denied = ClientError(
        {"Error": {"Code": "AccessDeniedException", "Message": "no"}},
        "GetParametersByPath",
    )
    with patch("g3dt.resolver.resolve", side_effect=denied):
        with pytest.raises(ClientError):
            studies.registry_for_env("staging")


@mock_aws
def test_empty_registry_error_gives_add_and_migrate_commands():
    """
    Inputs:  a deployed env with no studies anywhere (fresh environment)
    Expected: the miss error contains copy-pasteable `g3dt study add` and
              `g3dt study migrate` commands — never the 4.0.1 "(none — add a
              studies: block to g3dt.yaml)" dead end.
    """
    _seed_tree()
    with pytest.raises(config.ConfigError) as exc:
        config.resolve_study("cdah", "staging")
    msg = str(exc.value)
    assert "g3dt study add cdah" in msg
    assert "g3dt study migrate" in msg
    assert "studies: block" not in msg


@mock_aws
def test_case_forgiving_lookup_resolves_canonical_record():
    """
    Inputs:  resolve 'CDAH' (an operator typing the Gen3 project_id's case)
    Expected: the canonical lowercase record — reads forgive case so the
              live `config show --study AusDiab` confusion can't recur.
    """
    _seed_tree()
    _seed_ssm_study("cdah", "s3://b/release_jsons/v2.0.0/cdah/")
    s = config.resolve_study("CDAH", "staging")
    assert s.key == "cdah"
