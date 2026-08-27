"""Regression tests for the dictionary upload script's S3 target parsing.

Background: ``upload_dictionary.py`` receives its S3 target as a CLI argument
composed by callers that prepend ``s3://`` to the env's ``schema_s3_uri``
(deploy_dd.sh, full_deploy_dd_and_synth.sh, dict_cmds.py). When the SSM value
already carried the scheme, the script was handed
``s3://s3://schema-bpsyc-test-biocommons-org-au/schema_dev.json``; its old
``startswith("s3://")`` guard passed and ``split("/", 1)`` produced the bucket
``"s3:"``, crashing boto3 with ``Invalid bucket name "s3:"``.

The script now normalizes its target via ``g3dt.config.normalize_s3_location``
(defense-in-depth — the config layer normalizes too, but this script is also
runnable by hand). These tests exercise the real boto3 code path against a
moto-mocked S3 bucket, because the incident lived exactly at the
parse-to-boto3 boundary. The normalizer's full input matrix is covered in
test_s3_location.py.
"""
import json

import boto3
import pytest
from moto import mock_aws

from g3dt.services.dictionary.upload_dictionary import upload_dict_to_s3

REGION = "ap-southeast-2"
BUCKET = "schema-bucket"


@pytest.fixture
def dict_file(tmp_path):
    """A minimal dictionary JSON carrying the version the script uploads as
    S3 metadata (the same shape ``get_dict_version`` reads in production)."""
    p = tmp_path / "schema_dev.json"
    p.write_text(json.dumps({"_settings.yaml": {"_dict_version": "v1"}}))
    return str(p)


@pytest.fixture
def s3(monkeypatch):
    """A moto-mocked S3 with the target bucket created and creds stubbed."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    with mock_aws():
        client = boto3.client("s3", region_name=REGION)
        client.create_bucket(
            Bucket=BUCKET,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
        yield client


def test_doubled_scheme_uploads_to_the_real_bucket(dict_file, s3):
    """Input: the incident-shaped target 's3://s3://bucket/key'.

    Expected: True, and the object lands in the REAL bucket with the version
    metadata — not in a bucket named 's3:'. This is the exact pre-fix crash.
    """
    ok = upload_dict_to_s3(dict_file, f"s3://s3://{BUCKET}/schema.json", "v1")
    assert ok is True
    head = s3.head_object(Bucket=BUCKET, Key="schema.json")
    assert head["Metadata"] == {"version": "v1"}


def test_https_url_is_accepted(dict_file, s3):
    """Input: the bucket's https endpoint URL (a console 'Copy URL' paste).

    Expected: True — the user-facing forgiveness requirement: an https link
    works anywhere an s3:// URI does.
    """
    url = f"https://{BUCKET}.s3.{REGION}.amazonaws.com/schema.json"
    assert upload_dict_to_s3(dict_file, url, "v1") is True
    s3.head_object(Bucket=BUCKET, Key="schema.json")


def test_bare_bucket_key_is_accepted(dict_file, s3):
    """Input: scheme-less 'bucket/key' (the canonical config form, passed raw).

    Expected: True — someone running the script by hand with the value
    exactly as stored in SSM should not need to add the scheme themselves.
    """
    assert upload_dict_to_s3(dict_file, f"{BUCKET}/schema.json", "v1") is True
    s3.head_object(Bucket=BUCKET, Key="schema.json")


def test_missing_key_returns_false(dict_file, s3):
    """Input: a bucket with no object key -> Expected: False, nothing uploaded.

    The config layer deliberately tolerates key-less values (resolve_env gates
    every command); the upload is the point of use, so THIS is where the
    'needs an object key' rule must hold.
    """
    assert upload_dict_to_s3(dict_file, f"s3://{BUCKET}", "v1") is False
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)


def test_unparseable_uri_returns_false(dict_file, s3):
    """Input: an AWS console page URL -> Expected: False, no S3 call attempted.

    Preserves the script's exit contract (False -> exit 1) instead of raising
    through main(), and keeps the failure at the parse step with an
    accepted-forms message rather than deep inside boto3.
    """
    url = "https://console.aws.amazon.com/s3/buckets/b?prefix=k"
    assert upload_dict_to_s3(dict_file, url, "v1") is False
    assert "Contents" not in s3.list_objects_v2(Bucket=BUCKET)
