"""Tests for g3dt.resolver: reading the CDK-published SSM tree.

Why this matters: the resolver is the *only* thing standing between the toolkit
and an AWS resource name. If it silently returned the wrong bucket, a job could
write to the wrong environment. These tests pin the three behaviours that make
that impossible: (a) SSM leaves map to the right typed fields, (b) an
un-deployed env fails loudly with a "has CDK been deployed?" hint instead of a
cryptic AWS error, and (c) resolution is cached so a CLI invocation makes
exactly one SSM round-trip.

The seeded tree mirrors the as-built shape published by
gen3-aws-data-pipeline/lib/stacks/ssm-parameters-stack.ts (drift-guarded there
by test/ssm-publishing.test.ts).
"""
import boto3
import pytest
from moto import mock_aws

from g3dt import resolver
from g3dt.config import ConfigError

REGION = "ap-southeast-2"
ACCOUNT = "232870232581"


def _seed(project: str, env: str) -> None:
    """Write a minimal as-built-shaped tree into the mocked SSM."""
    ssm = boto3.client("ssm", region_name=REGION)
    tree = {
        "meta/region": REGION,
        "meta/toolkitVersion": "2.0.0",
        "buckets/metadata": f"{project}-{env}-metadata-{ACCOUNT}-{REGION}",
        "glue/db/metadata": f"{project}_{env}_dataops_metadata_db",
        "release/db": f"{project}_{env}_dataops_metadata_db",
        "release/table": "releases",
        "athena/workgroup": f"{project}-{env}",
        "athena/outputLocation": f"s3://{project}-{env}-athena-results-{ACCOUNT}-{REGION}/",
        "ec2/instanceId": "i-0123456789abcdef0",
        "ec2/logGroup": f"/{project}/{env}/ec2/jobs",
        "ec2/logBucket": f"{project}-{env}-metadata-{ACCOUNT}-{REGION}",
        "ec2/logPrefix": "ec2-job-logs",
        "app/domain": "cd.example.test.biocommons.org.au",
        "app/aws_secret_name": "example_api_key.json",
        "app/schema_repo": "AustralianBioCommons/acdc-schema-json",
    }
    for rel, value in tree.items():
        ssm.put_parameter(Name=f"/{project}/{env}/{rel}", Value=value, Type="String")


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    """Each test gets an empty resolver cache and a pinned region."""
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    resolver.resolve.cache_clear()
    yield
    resolver.resolve.cache_clear()


@mock_aws
def test_resolves_leaf_names_to_typed_fields():
    """A seeded as-built tree resolves to the expected typed accessors.

    Input: the SSM tree for project `etl`, env `test`. Expected: each typed
    property returns exactly the seeded value (bucket, DB, workgroup, instance
    id, log destinations, app facts).
    """
    _seed("etl", "test")
    rc = resolver.resolve("etl", "test")
    assert rc.metadata_bucket == f"etl-test-metadata-{ACCOUNT}-{REGION}"
    assert rc.metadata_db == "etl_test_dataops_metadata_db"
    assert rc.release_db == "etl_test_dataops_metadata_db"
    assert rc.release_table == "releases"
    assert rc.athena_workgroup == "etl-test"
    assert rc.ec2_instance_id == "i-0123456789abcdef0"
    assert rc.ec2_log_group == "/etl/test/ec2/jobs"
    assert rc.ec2_log_bucket == f"etl-test-metadata-{ACCOUNT}-{REGION}"
    assert rc.ec2_log_prefix == "ec2-job-logs"
    assert rc.region == REGION
    assert rc.app("domain") == "cd.example.test.biocommons.org.au"


@mock_aws
def test_two_envs_resolve_independently():
    """test and prod trees never bleed into each other.

    This is the property that kills the legacy shared-box bug: each env's
    instance id comes from its own tree, so `test` can never dispatch to
    `prod`'s box.
    """
    _seed("etl", "test")
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(
        Name="/etl/prod/ec2/instanceId", Value="i-0fffffffffffffff0", Type="String"
    )
    assert resolver.resolve("etl", "test").ec2_instance_id == "i-0123456789abcdef0"
    assert resolver.resolve("etl", "prod").ec2_instance_id == "i-0fffffffffffffff0"


@mock_aws
def test_empty_path_raises_friendly_error():
    """An env with no parameters fails with the 'has CDK been deployed?' hint.

    A junior operator who typos --env (or targets an account where the CDK was
    never deployed) must get instructions, not a KeyError from deep inside a
    job.
    """
    with pytest.raises(ConfigError) as exc:
        resolver.resolve("etl", "never-deployed")
    assert "has CDK been deployed" in str(exc.value)
    assert "/etl/never-deployed" in str(exc.value)


@mock_aws
def test_missing_leaf_raises_friendly_error():
    """A deployed-but-incomplete tree names the missing parameter exactly."""
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(Name="/etl/test/meta/region", Value=REGION, Type="String")
    rc = resolver.resolve("etl", "test")
    with pytest.raises(ConfigError) as exc:
        _ = rc.metadata_bucket
    assert "/etl/test/buckets/metadata is missing" in str(exc.value)


@mock_aws
def test_resolution_is_cached_single_network_call():
    """Two resolves of the same (project, env) hit SSM once.

    The second call must be an lru_cache hit — this keeps a multi-command CLI
    invocation at exactly one get_parameters_by_path round-trip.
    """
    _seed("etl", "test")
    resolver.resolve("etl", "test")
    resolver.resolve("etl", "test")
    info = resolver.resolve.cache_info()
    assert info.misses == 1
    assert info.hits == 1


@mock_aws
def test_list_envs_returns_deployed_envs():
    """list_envs discovers env names from the parameter paths under /{project}/."""
    _seed("etl", "test")
    _seed("etl", "staging")
    assert resolver.list_envs("etl") == ["staging", "test"]
