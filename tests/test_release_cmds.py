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
        "buckets/silver": f"{project}-{env}-silver-{ACCOUNT}-{REGION}",
        "buckets/gold": f"{project}-{env}-gold-{ACCOUNT}-{REGION}",
        "glue/db/bronze": f"{project}_{env}_bronze_db",
        "glue/db/silver": f"{project}_{env}_silver_db",
        "glue/db/gold": f"{project}_{env}_gold_db",
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
    # platform roles are workgroup-scoped; the model search is scoped to the
    # env's own DBs so shared accounts can't cross-match
    assert kwargs["workgroup"] == "etl-test"
    assert kwargs["search_databases"] == [
        "etl_test_silver_db", "etl_test_gold_db"
    ]
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
            db_name="etl_test_silver_db",
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
    assert "export G3DT_DB_BRONZE=etl_test_bronze_db" in out
    assert "export G3DT_DB_SILVER=etl_test_silver_db" in out
    assert "export G3DT_DB_GOLD=etl_test_gold_db" in out
    assert f"export G3DT_S3_SILVER_DATA_DIR=s3://etl-test-silver-{ACCOUNT}-{REGION}/dbt/" in out
    assert f"export G3DT_S3_GOLD_DATA_DIR=s3://etl-test-gold-{ACCOUNT}-{REGION}/dbt/" in out
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


@mock_aws
def test_config_dbt_env_emits_ci_isolation_vars():
    """
    Inputs:  g3dt config dbt-env --env test
    Expected Output: alongside the real names, the CI-isolation variants —
    ci_-prefixed database names and dbt_ci/ data dirs — that the dbt
    template's `ci` target consumes. The REAL names must stay unprefixed
    (the CI-isolation invariant: only the ci target is prefixed).

    Background:
        Commit-triggered CI dbt builds are isolated into ci_<real-db-name>
        Glue databases with data under s3://<same-bucket>/dbt_ci/, so CI can
        never advance the warehouse's Iceberg snapshots that releases pin.
    """
    _seed()
    result = runner.invoke(app, ["config", "dbt-env", "--env", "test"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "export G3DT_DB_SILVER_CI=ci_etl_test_silver_db" in out
    assert "export G3DT_DB_GOLD_CI=ci_etl_test_gold_db" in out
    assert f"export G3DT_S3_SILVER_DATA_DIR_CI=s3://etl-test-silver-{ACCOUNT}-{REGION}/dbt_ci/" in out
    assert f"export G3DT_S3_GOLD_DATA_DIR_CI=s3://etl-test-gold-{ACCOUNT}-{REGION}/dbt_ci/" in out
    # the real names remain unprefixed
    assert "export G3DT_DB_SILVER=etl_test_silver_db" in out
    assert "export G3DT_DB_GOLD=etl_test_gold_db" in out


@mock_aws
@patch("g3dt.utils.release_writer.run")
def test_missing_medallion_keys_fail_loudly(mock_run):
    """
    Background:
        gen3-aws-data-pipeline < v2.0.0 published the medallion names under
        SSM keys carrying a legacy raw prefix in the leaf name. Toolkit
        versions < 3 read them with rc.get(), which returns None for
        an absent key — so a key-name mismatch FAILED SILENTLY: dbt-env
        dropped the G3DT_DB_* export lines entirely and emitted
        `s3://None/dbt/` data dirs, and `release write` quietly fell back to
        an account-wide Glue catalog walk. Toolkit >= 3 reads only the
        raw-free keys and must fail loudly when they are missing.

    Inputs:  an SSM tree seeded WITHOUT the medallion keys — exactly what a
             pipeline deployment older than v2.0.0 presents to toolkit >= 3.
    Expected: `config dbt-env` and `release write` both exit 1 with a
              ConfigError naming the missing SSM key and pointing at the
              pipeline upgrade (>= v2.0.0); no partial exports, no s3://None,
              and the release writer is never invoked.
    """
    ssm = boto3.client("ssm", region_name=REGION)
    leaves = {  # everything _seed publishes EXCEPT the medallion keys
        "meta/region": REGION,
        "buckets/metadata": f"etl-test-metadata-{ACCOUNT}-{REGION}",
        "release/db": "etl_test_dataops_metadata_db",
        "release/table": "releases",
        "athena/workgroup": "etl-test",
        "athena/outputLocation": f"s3://etl-test-athena-results-{ACCOUNT}-{REGION}/",
    }
    for rel, value in leaves.items():
        ssm.put_parameter(Name=f"/etl/test/{rel}", Value=value, Type="String")

    result = runner.invoke(app, ["config", "dbt-env", "--env", "test"])
    assert result.exit_code == 1
    assert "/etl/test/glue/db/silver is missing" in result.output
    assert "v2.0.0" in result.output
    assert "export" not in result.output  # all-or-nothing: no partial env
    assert "s3://None" not in result.output

    result = runner.invoke(
        app,
        ["release", "write", "--env", "test", "--data-release-version", "1.0.0"],
    )
    assert result.exit_code == 1
    assert "/etl/test/glue/db/silver is missing" in result.output
    assert "v2.0.0" in result.output
    mock_run.assert_not_called()


def test_release_writer_run_aggregates_model_failures():
    """
    Inputs:  release_writer.run over three models where the snapshot lookup
             for one model raises.
    Expected: the other two models are still recorded (inserts happen), and
              run() raises RuntimeError naming exactly the failed model.

    Background:
        Models are processed concurrently; one bad model must not abort the
        in-flight others, but the release build must still fail loudly and
        say which model to investigate. Inserts are idempotent, so re-running
        after a fix fills in only the remainder.
    """
    from unittest.mock import MagicMock

    import g3dt.utils.release_writer as rw

    inserted = []

    def fake_insert(**kwargs):
        inserted.append(kwargs["model_name"])

    def fake_snapshot(self, return_commit_datetime=False):
        if self.table_name == "silver_bad":
            raise ValueError("boom")
        return 42, "2026-01-01 00:00:00"

    with patch.object(rw, "get_model_names", return_value=["silver_a", "silver_bad", "silver_b"]), \
         patch.object(rw, "insert_release_row", side_effect=fake_insert), \
         patch.object(rw.AthenaQuery, "create_release_table", MagicMock()), \
         patch.object(rw.AthenaQuery, "find_db_for_model", lambda self, m, databases=None: "etl_test_silver_db"), \
         patch.object(rw.AthenaValidationWriter, "_get_latest_snapshot_id", fake_snapshot):
        with pytest.raises(RuntimeError, match="silver_bad"):
            rw.run(
                dbt_schema_path="schema.yml",
                release_db="etl_test_dataops_metadata_db",
                release_table="releases",
                release_s3_location="s3://etl-test-metadata/",
                data_release_version="0.0.1",
                commit_id="deadbeef",
                aws_region="ap-southeast-2",
                athena_s3_output="s3://etl-test-athena-results/",
                max_workers=2,
            )

    assert sorted(inserted) == ["silver_a", "silver_b"]
