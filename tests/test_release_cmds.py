"""Tests for `g3dt release write` and `g3dt config dbt-env` (moto SSM).

Why this matters: these two commands are the buildspec's whole interface to
AWS — `write_release_info.yml` passes only `--env`, the tag-derived version,
and the commit SHA, and `dbt-env` supplies every name dbt needs. If either
resolves the wrong name, a release row lands in the wrong database. The tests
seed an as-built SSM tree and pin exactly which resolved values reach the
(patched) writer / stdout.
"""
import textwrap
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from g3dt import config, resolver
from g3dt.cli.main import app

runner = CliRunner()

REGION = "ap-southeast-2"
ACCOUNT = "232870232581"

_MARKER_YAML = """
project: etl
region: ap-southeast-2
"""


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    """Hermetic marker (project=etl, no profiles → ambient creds) + clean caches."""
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


def _seed(project="etl", env="test"):
    """Publish the release/dbt-relevant slice of the as-built tree."""
    ssm = boto3.client("ssm", region_name=REGION)
    leaves = {
        "meta/region": REGION,
        "buckets/metadata": f"{project}-{env}-metadata-{ACCOUNT}-{REGION}",
        "buckets/rawSilver": f"{project}-{env}-raw-silver-{ACCOUNT}-{REGION}",
        "buckets/rawGold": f"{project}-{env}-raw-gold-{ACCOUNT}-{REGION}",
        "glue/db/rawBronze": f"{project}_{env}_raw_bronze_db",
        "glue/db/rawSilver": f"{project}_{env}_raw_silver_db",
        "glue/db/rawGold": f"{project}_{env}_raw_gold_db",
        "release/db": f"{project}_{env}_dataops_metadata_db",
        "release/table": "releases",
        "athena/workgroup": f"{project}-{env}",
        "athena/outputLocation": f"s3://{project}-{env}-athena-results-{ACCOUNT}-{REGION}/",
    }
    for rel, value in leaves.items():
        ssm.put_parameter(Name=f"/{project}/{env}/{rel}", Value=value, Type="String")


@mock_aws
@patch("g3dt.utils.release_writer.run")
def test_release_write_resolves_names_from_ssm(mock_run):
    """
    Inputs:  g3dt release write --env test --data-release-version 1.4.0
             --commit-id deadbeef
    Expected Output: release_writer.run called once with the SSM-resolved
    release DB/table, the metadata bucket as the table location, the workgroup
    output location, and the region — the caller supplied none of them.
    """
    _seed()
    result = runner.invoke(
        app,
        ["release", "write", "--env", "test",
         "--data-release-version", "1.4.0", "--commit-id", "deadbeef"],
    )
    assert result.exit_code == 0, result.output

    mock_run.assert_called_once()
    kwargs = mock_run.call_args.kwargs
    assert kwargs["release_db"] == "etl_test_dataops_metadata_db"
    assert kwargs["release_table"] == "releases"
    assert kwargs["release_s3_location"] == f"s3://etl-test-metadata-{ACCOUNT}-{REGION}/"
    assert kwargs["athena_s3_output"] == f"s3://etl-test-athena-results-{ACCOUNT}-{REGION}/"
    assert kwargs["aws_region"] == REGION
    assert kwargs["data_release_version"] == "1.4.0"
    assert kwargs["commit_id"] == "deadbeef"
    assert kwargs["dry_run"] is False
    # the resolved target is echoed so a build log always shows where rows go
    assert "etl_test_dataops_metadata_db.releases" in result.output


@mock_aws
@patch("g3dt.utils.release_writer.run")
def test_release_write_dry_run_flag_passes_through(mock_run):
    """--dry-run reaches the writer (which logs SQL instead of writing)."""
    _seed()
    result = runner.invoke(
        app,
        ["release", "write", "--env", "test",
         "--data-release-version", "0.0.0-dryrun", "--dry-run"],
    )
    assert result.exit_code == 0, result.output
    assert mock_run.call_args.kwargs["dry_run"] is True
    assert "nothing was written" in result.output


@mock_aws
@patch("g3dt.utils.release_writer.run")
def test_release_write_undeployed_env_fails_loudly(mock_run):
    """An env with no SSM tree exits 1 with the deploy hint; the writer never runs."""
    result = runner.invoke(
        app,
        ["release", "write", "--env", "nope", "--data-release-version", "1.0.0"],
    )
    assert result.exit_code == 1
    assert "has CDK been deployed" in result.output
    mock_run.assert_not_called()


def test_release_writer_dry_run_writes_nothing():
    """
    Unit check on the writer itself: with dry_run, insert_release_row builds
    and logs the INSERT but never calls query_athena — the property O3 relies on.
    """
    from unittest.mock import MagicMock

    from g3dt.utils.release_writer import insert_release_row

    athena_config = MagicMock()
    with patch("g3dt.utils.release_writer.AthenaQuery") as mock_q:
        insert_release_row(
            athena_config=athena_config,
            model_name="silver_x",
            db_name="etl_test_raw_silver_db",
            snapshot_id=1,
            committed_at="2026-01-01 00:00:00",
            release_db="etl_test_dataops_metadata_db",
            release_table="releases",
            release_tag="0.0.0-dryrun",
            github_sha="deadbeef",
            dry_run=True,
        )
        mock_q.return_value.query_athena.assert_not_called()


@mock_aws
def test_config_dbt_env_emits_every_dbt_setting():
    """
    Inputs:  g3dt config dbt-env --env test
    Expected Output: shell-evaluable `export` lines carrying the workgroup,
    Athena output, region, bronze/silver/gold DBs and the silver/gold
    s3_data_dir values — the full env_var() contract of the dbt template.
    """
    _seed()
    result = runner.invoke(app, ["config", "dbt-env", "--env", "test"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "export G3DT_ATHENA_WORKGROUP=etl-test" in out
    assert f"export G3DT_ATHENA_OUTPUT=s3://etl-test-athena-results-{ACCOUNT}-{REGION}/" in out
    assert f"export G3DT_REGION={REGION}" in out
    assert "export G3DT_DB_RAW_BRONZE=etl_test_raw_bronze_db" in out
    assert "export G3DT_DB_RAW_SILVER=etl_test_raw_silver_db" in out
    assert "export G3DT_DB_RAW_GOLD=etl_test_raw_gold_db" in out
    assert f"export G3DT_S3_SILVER_DATA_DIR=s3://etl-test-raw-silver-{ACCOUNT}-{REGION}/dbt/" in out
    assert f"export G3DT_S3_GOLD_DATA_DIR=s3://etl-test-raw-gold-{ACCOUNT}-{REGION}/dbt/" in out
    # no profile configured -> ambient credentials and the default dbt target
    assert "G3DT_AWS_PROFILE" not in out
    assert "G3DT_DBT_TARGET" not in out


@mock_aws
def test_config_dbt_env_selects_local_target_with_profile(tmp_path, monkeypatch):
    """
    Inputs:  a marker whose profiles: map covers the env
    Expected Output: dbt-env additionally exports G3DT_AWS_PROFILE and
    G3DT_DBT_TARGET=local, selecting the profiles.yml target that carries
    aws_profile_name — so a laptop run authenticates with the named profile
    while CodeBuild (no profiles map) stays on ambient credentials.
    """
    marker = tmp_path / "g3dt.yaml"
    marker.write_text(
        "project: etl\nregion: ap-southeast-2\nprofiles:\n  test: etl_test\n"
    )
    monkeypatch.setenv("G3DT_MARKER", str(marker))
    creds = tmp_path / "aws_credentials"
    creds.write_text(
        "[etl_test]\naws_access_key_id = testing\naws_secret_access_key = testing\n"
    )
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(creds))
    config._load_yaml_cached.cache_clear()
    resolver.resolve.cache_clear()

    _seed()
    result = runner.invoke(app, ["config", "dbt-env", "--env", "test"])
    assert result.exit_code == 0, result.output
    assert "export G3DT_AWS_PROFILE=etl_test" in result.output
    assert "export G3DT_DBT_TARGET=local" in result.output
