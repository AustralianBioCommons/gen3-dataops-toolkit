"""Tests for environment/study resolution in ``g3dt.config``.

The CLI relies on these rules to turn a friendly ``--study ausdiab --env staging``
into the right study key and S3 path, and to resolve every AWS name from the
env's SSM tree. The riskiest rule is the ``{study}_{env_base}`` derivation: a
wrong mapping could push *staging* data to a *prod* location, so each case
states its input and expected output explicitly.

Environments resolve from SSM (mocked with moto); studies resolve from the
local ``g3dt.yaml`` marker (passed as a dict here, exactly as ``load_marker``
returns it).
"""
import boto3
import pytest
from moto import mock_aws

from g3dt import config, resolver

REGION = "ap-southeast-2"


@pytest.fixture
def marker():
    """A minimal but realistic marker dict (staging + prod studies, profiles)."""
    return {
        "project": "etl",
        "region": REGION,
        "default_env": "staging",
        "profiles": {"staging": "etl_staging"},
        "studies": {
            "ausdiab_staging": {
                "project_id": "AusDiab",
                "program_id": "program1",
                "s3_metadata_path": "s3://b/staging/ausdiab/",
            },
            "ausdiab_prod": {
                "project_id": "AusDiab",
                "program_id": "program1",
                "s3_metadata_path": "s3://b/prod/ausdiab/",
            },
        },
    }


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path):
    """Point the marker search at an empty temp file and reset caches.

    Keeps every test hermetic: no developer ~/.g3dt/g3dt.yaml or env vars can
    leak in, and the resolver cache never crosses tests.
    """
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)
    monkeypatch.delenv("G3DT_PROJECT", raising=False)
    monkeypatch.delenv("G3DT_DEFAULT_ENV", raising=False)
    empty = tmp_path / "g3dt.yaml"
    empty.write_text("project: etl\nregion: ap-southeast-2\n")
    monkeypatch.setenv("G3DT_MARKER", str(empty))
    config._load_yaml_cached.cache_clear()
    resolver.resolve.cache_clear()
    yield
    config._load_yaml_cached.cache_clear()
    resolver.resolve.cache_clear()


def _seed_env(project: str, env: str, instance_id: str = "i-123") -> None:
    """Publish a complete app-facts tree for one env into mocked SSM."""
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
        "ec2/instanceId": instance_id,
    }
    for rel, value in leaves.items():
        ssm.put_parameter(Name=f"/{project}/{env}/{rel}", Value=value, Type="String")


# --------------------------------------------------------------------------- #
# env_base + studies (pure marker logic, no AWS)                               #
# --------------------------------------------------------------------------- #
def test_env_base_strips_ec2_suffix():
    """Input: 'staging_ec2' / 'staging' -> Expected: both base to 'staging'."""
    assert config.env_base("staging_ec2") == "staging"
    assert config.env_base("staging") == "staging"


def test_resolve_study_derives_env_specific_key(marker):
    """Input: study 'ausdiab', env 'staging' -> key 'ausdiab_staging', staging path."""
    s = config.resolve_study("ausdiab", "staging", marker)
    assert s.key == "ausdiab_staging"
    assert s.s3_metadata_path.endswith("/staging/ausdiab/")


def test_resolve_study_ec2_env_maps_to_base_study_key(marker):
    """Input: study 'ausdiab', env 'staging_ec2' -> Expected: still 'ausdiab_staging'."""
    s = config.resolve_study("ausdiab", "staging_ec2", marker)
    assert s.key == "ausdiab_staging"


def test_resolve_study_prod_is_separate_from_staging(marker):
    """Safety: env 'prod' must resolve to the prod path, never the staging one."""
    s = config.resolve_study("ausdiab", "prod", marker)
    assert s.key == "ausdiab_prod"
    assert "/prod/" in s.s3_metadata_path


@mock_aws
def test_resolve_study_unknown_raises_with_valid_list(marker):
    """Unknown study -> ConfigError that names the valid studies (acts as help)."""
    with pytest.raises(config.ConfigError) as exc:
        config.resolve_study("nope", "staging", marker)
    assert "Valid studies" in str(exc.value)


def test_list_studies_strips_env_suffix(marker):
    assert config.list_studies(marker) == ["ausdiab"]


# --------------------------------------------------------------------------- #
# resolve_env (SSM-backed, moto)                                               #
# --------------------------------------------------------------------------- #
@mock_aws
def test_resolve_env_reads_app_facts_from_ssm():
    """Input: a deployed /etl/staging tree -> Expected: EnvConfig mirrors it."""
    _seed_env("etl", "staging")
    e = config.resolve_env("staging")
    assert e.dictionary_version == "v1"
    assert e.aws_secret_name == "sec"
    assert e.schema_repo == "Org/schema-repo"
    assert e.region == REGION
    assert e.is_ec2 is False


@mock_aws
def test_resolve_env_ec2_flag_and_instance():
    """The *_ec2 pseudo-env resolves the SAME tree with is_ec2 + ambient creds.

    This is the dispatch contract: SSM has one tree per real env; `_ec2` only
    switches authentication to the instance profile (aws_profile=None).
    """
    _seed_env("etl", "staging", instance_id="i-123")
    e = config.resolve_env("staging_ec2")
    assert e.is_ec2 is True
    assert e.ec2_instance_id == "i-123"
    assert e.aws_profile is None


@mock_aws
def test_resolve_env_missing_app_fact_raises():
    """A tree missing an app fact is rejected with a re-deploy hint.

    Why: an incomplete `cdk deploy` (or an old SSM stack) must fail loudly at
    resolve time, not as a KeyError mid-job.
    """
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(
        Name="/etl/broken/app/domain", Value="d", Type="String"
    )  # deliberately incomplete
    with pytest.raises(config.ConfigError) as exc:
        config.resolve_env("broken")
    assert "missing required app fact" in str(exc.value)


@mock_aws
def test_resolve_env_undeployed_raises_deploy_hint():
    """An env with no SSM tree at all names the fix: run cdk deploy."""
    with pytest.raises(config.ConfigError) as exc:
        config.resolve_env("nope")
    assert "has CDK been deployed" in str(exc.value)


@mock_aws
def test_list_envs_reads_deployed_trees():
    """list_envs discovers envs from SSM paths, not from any local file."""
    _seed_env("etl", "staging")
    _seed_env("etl", "test")
    assert config.list_envs() == ["staging", "test"]


# --------------------------------------------------------------------------- #
# marker bootstrap                                                             #
# --------------------------------------------------------------------------- #
def test_require_project_without_marker_gives_setup_help(monkeypatch, tmp_path):
    """No marker + no env var -> instructions, not a stack trace."""
    missing = tmp_path / "nope.yaml"
    monkeypatch.setenv("G3DT_MARKER", str(missing))
    with pytest.raises(config.ConfigError) as exc:
        config.require_project()
    assert "g3dt config set project" in str(exc.value)


def test_env_vars_override_marker(monkeypatch, tmp_path):
    """G3DT_PROJECT beats the marker file value (CI/EC2 escape hatch)."""
    m = tmp_path / "g3dt.yaml"
    m.write_text("project: filevalue\n")
    monkeypatch.setenv("G3DT_MARKER", str(m))
    monkeypatch.setenv("G3DT_PROJECT", "envvalue")
    config._load_yaml_cached.cache_clear()
    assert config.load_marker()["project"] == "envvalue"


def test_aws_profile_for_uses_marker_profiles(marker):
    """profiles: {staging: etl_staging} -> staging + staging_ec2 both map to it;
    an env not in the map gets None (ambient credentials)."""
    assert config.aws_profile_for("staging", marker) == "etl_staging"
    assert config.aws_profile_for("staging_ec2", marker) == "etl_staging"
    assert config.aws_profile_for("prod", marker) is None
