"""`g3dt metadata upload --release <tag>`: find that release's files in S3.

Background: an upload always read from the study's registry path, so
uploading a specific release meant moving the registry first
(`study repoint`). 5.0.0 adds --release, which swaps the vX.Y.Z segment of
the registry path for the requested tag and proves the prefix is uploadable
(DataImportOrder.txt + node JSONs, the checks repoint and the worker make)
before anything is dispatched — the registry itself is never written.

Moto provides SSM (the env tree + one registered study) and S3 (the release
prefixes); the worker subprocess is patched so only the CLI's resolution and
pre-flight run for real. The worker's own pure helper is tested directly.
"""
import importlib.util
import json
from pathlib import Path
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from g3dt.cli.main import app
from g3dt.config import ConfigError

runner = CliRunner()

REGION = "ap-southeast-2"
GOLD = "etl-gold"
WORKER = (
    Path(__file__).resolve().parent.parent / "src" / "g3dt" / "services"
    / "upload" / "metadata" / "upload_metadata.py"
)


@pytest.fixture(autouse=True)
def _region(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _path(version, study):
    return f"s3://{GOLD}/release_jsons/{version}/{study}/"


def _seed(study_path):
    """The /etl/staging tree plus one study 'cdah' at ``study_path``."""
    ssm = boto3.client("ssm", region_name=REGION)
    leaves = {
        "meta/region": REGION,
        "buckets/metadata": "etl-meta",
        "app/dictionary_version": "v1",
        "app/aws_secret_name": "sec",
        "app/schema_s3_uri": "u",
        "app/domain": "d",
        "app/app_name": "a",
        "app/namespace": "n",
        "app/cluster_name": "c",
        "app/schema_repo": "Org/schema-repo",
        "studies/cdah": json.dumps(
            {"project_id": "CDAH", "program_id": "program1",
             "s3_metadata_path": study_path}
        ),
    }
    for rel, value in leaves.items():
        ssm.put_parameter(Name=f"/etl/staging/{rel}", Value=value, Type="String")


def _seed_release_prefix(version, study):
    """A valid upload target: DataImportOrder.txt plus one node JSON."""
    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(
            Bucket=GOLD,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    prefix = f"release_jsons/{version}/{study}"
    s3.put_object(Bucket=GOLD, Key=f"{prefix}/DataImportOrder.txt", Body=b"subject\n")
    s3.put_object(Bucket=GOLD, Key=f"{prefix}/subject.json", Body=b"[]")


@mock_aws
@patch("g3dt.cli._internal.runner.run")
def test_release_present_in_s3_resolves_and_runs_worker(mock_run):
    """
    Inputs:  study cdah registered at v2.0.0; v2.1.0 exported to S3;
             `metadata upload --study cdah --env staging --release v2.1.0`
    Expected: the resolved v2.1.0 prefix and its JSON count are reported on
              stderr, the worker runs with `--release 2.1.0`, and the
              registry still says v2.0.0 (nothing was repointed).
    """
    _seed(_path("v2.0.0", "cdah"))
    _seed_release_prefix("v2.0.0", "cdah")
    _seed_release_prefix("v2.1.0", "cdah")
    result = runner.invoke(
        app,
        ["metadata", "upload", "--study", "cdah", "--env", "staging",
         "--release", "v2.1.0"],
    )
    assert result.exit_code == 0, result.output
    assert f"release 2.1.0 -> {_path('v2.1.0', 'cdah')} (1 node JSONs)" in result.stderr
    argv = list(mock_run.call_args.args[0])
    assert argv[argv.index("--release") + 1] == "2.1.0"
    record = boto3.client("ssm", region_name=REGION).get_parameter(
        Name="/etl/staging/studies/cdah"
    )["Parameter"]["Value"]
    assert json.loads(record)["s3_metadata_path"] == _path("v2.0.0", "cdah")


@mock_aws
@patch("g3dt.cli._internal.runner.run")
def test_release_missing_from_s3_fails_before_the_worker(mock_run):
    """
    Inputs:  the same study; `--release v9.9.9`, which was never exported
    Expected: exit 1, the missing DataImportOrder.txt named, and the worker
              never invoked — so a typo can never reach sheepdog.
    """
    _seed(_path("v2.0.0", "cdah"))
    _seed_release_prefix("v2.0.0", "cdah")
    result = runner.invoke(
        app,
        ["metadata", "upload", "--study", "cdah", "--env", "staging",
         "--release", "v9.9.9"],
    )
    assert result.exit_code == 1, result.output
    assert "not uploadable" in result.stderr
    assert "DataImportOrder.txt" in result.stderr
    mock_run.assert_not_called()


@mock_aws
@patch("g3dt.cli._internal.runner.run")
def test_registry_path_without_version_segment_is_usage_error(mock_run):
    """
    Inputs:  a study whose registry path has no vX.Y.Z segment
             (s3://etl-gold/flat/cdah/), plus `--release 2.1.0`
    Expected: exit 2 pointing at `g3dt study set --path`; the worker never
              runs. There is nothing to swap, so --release cannot apply.
    """
    _seed(f"s3://{GOLD}/flat/cdah/")
    result = runner.invoke(
        app,
        ["metadata", "upload", "--study", "cdah", "--env", "staging",
         "--release", "2.1.0"],
    )
    assert result.exit_code == 2, result.output
    assert "study set" in result.stderr
    mock_run.assert_not_called()


def _load_worker():
    spec = importlib.util.spec_from_file_location("upload_metadata_worker", WORKER)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_worker_release_base_dir_swaps_the_segment_and_normalises():
    """
    Inputs:  registry path .../release_jsons/v2.0.0/cdah/ and release 'v2.1.0'
    Expected: ('2.1.0', .../release_jsons/v2.1.0/cdah/) — the bare tag the
              receipts table stores, and the v-prefixed path S3 uses.
    """
    worker = _load_worker()
    tag, path = worker.release_base_dir(_path("v2.0.0", "cdah"), "v2.1.0")
    assert tag == "2.1.0"
    assert path == _path("v2.1.0", "cdah")


def test_worker_release_base_dir_rejects_bad_tag_and_flat_path():
    """
    Inputs:  a malformed tag; a registry path with no version segment
    Expected: ConfigError in both cases, before any S3 or sheepdog call.
    """
    worker = _load_worker()
    with pytest.raises(ConfigError):
        worker.release_base_dir(_path("v2.0.0", "cdah"), "latest")
    with pytest.raises(ConfigError):
        worker.release_base_dir(f"s3://{GOLD}/flat/cdah/", "2.1.0")
