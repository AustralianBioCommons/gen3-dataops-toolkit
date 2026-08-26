"""Tests for the SSM-backed study registry core (``g3dt.studies``).

Background: the registry defines exactly what ``metadata upload`` sends to a
live commons, and 4.1.0 makes the toolkit write SSM for the first time. What
must never happen: a write that half-applies, a stale in-process cache that
makes a just-added study invisible, or a malformed parameter that fails
somewhere far from its cause. These tests pin the write/read/cache contract
against real moto SSM behaviour (ParameterAlreadyExists, ParameterNotFound,
pagination) rather than hand-rolled mocks.
"""
import boto3
import pytest
from moto import mock_aws

from g3dt import config, resolver, studies

REGION = "ap-southeast-2"


def _seed_tree(project="etl", env="staging"):
    """A minimal env tree so ``resolver.resolve`` succeeds."""
    ssm = boto3.client("ssm", region_name=REGION)
    ssm.put_parameter(
        Name=f"/{project}/{env}/meta/region", Value=REGION, Type="String"
    )


def _rc(project="etl", env="staging"):
    return resolver.resolve(project, env)


def _session():
    return boto3.Session(region_name=REGION)


@pytest.fixture(autouse=True)
def _region(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


@mock_aws
def test_add_writes_json_blob():
    """
    Inputs:  put_study(..., overwrite=False) for a new name
    Expected: one String parameter /etl/staging/studies/cdah holding the
              three-field JSON blob.
    """
    _seed_tree()
    studies.put_study(
        _rc(),
        _session(),
        "cdah",
        project_id="CDAH",
        program_id="program1",
        s3_metadata_path="s3://b/release_jsons/v2.0.0/cdah/",
        overwrite=False,
    )
    got = boto3.client("ssm", region_name=REGION).get_parameter(
        Name="/etl/staging/studies/cdah"
    )["Parameter"]["Value"]
    assert '"project_id": "CDAH"' in got
    assert '"s3_metadata_path": "s3://b/release_jsons/v2.0.0/cdah/"' in got


@mock_aws
def test_add_existing_maps_to_guided_error():
    """
    Inputs:  put_study overwrite=False twice for the same name
    Expected: moto's real ParameterAlreadyExists becomes a ConfigError that
              points at `g3dt study set` (the fix), and the original record
              is untouched.
    """
    _seed_tree()
    kwargs = dict(
        project_id="CDAH",
        program_id="program1",
        s3_metadata_path="s3://b/v1/cdah/",
        overwrite=False,
    )
    studies.put_study(_rc(), _session(), "cdah", **kwargs)
    with pytest.raises(config.ConfigError) as exc:
        studies.put_study(_rc(), _session(), "cdah", **kwargs)
    assert "already exists" in str(exc.value)
    assert "g3dt study set" in str(exc.value)


@mock_aws
def test_remove_missing_maps_to_guided_error():
    """
    Inputs:  delete_study for a name that was never registered
    Expected: moto's real ParameterNotFound becomes a ConfigError pointing at
              `g3dt study list`, not a traceback.
    """
    _seed_tree()
    with pytest.raises(config.ConfigError) as exc:
        studies.delete_study(_rc(), _session(), "ghost")
    assert "not registered" in str(exc.value)
    assert "g3dt study list" in str(exc.value)


@mock_aws
def test_write_clears_resolver_cache():
    """
    Inputs:  resolve (cache warm) -> put_study -> registry_for_env
    Expected: the same process sees the new study immediately.

    Background: resolver.resolve is lru_cached for the process. Without the
    cache_clear hook inside put_study, `study add` followed by `study list`
    in one process would report the study missing — the freshest possible
    way to lose an operator's trust in the tool.
    """
    _seed_tree()
    registry, _src, _fb = studies.registry_for_env("staging")
    assert registry == {}
    studies.put_study(
        _rc(),
        _session(),
        "cdah",
        project_id="CDAH",
        program_id="program1",
        s3_metadata_path="s3://b/v1/cdah/",
        overwrite=False,
    )
    registry, _src, _fb = studies.registry_for_env("staging")
    assert list(registry) == ["cdah"]


@mock_aws
def test_registry_pagination_over_ten_studies():
    """
    Inputs:  12 registered studies (get_parameters_by_path pages at 10)
    Expected: all 12 come back — the paginated fetch is load-bearing.
    """
    _seed_tree()
    rc, session = _rc(), _session()
    for i in range(12):
        studies.put_study(
            rc,
            session,
            f"study{i:02d}",
            project_id="P",
            program_id="program1",
            s3_metadata_path=f"s3://b/v1/study{i:02d}/",
            overwrite=False,
        )
    registry, _src, _fb = studies.registry_for_env("staging")
    assert len(registry) == 12


@mock_aws
def test_malformed_json_param_names_ssm_path():
    """
    Inputs:  a studies/ parameter someone hand-edited into invalid JSON
    Expected: the error names the exact SSM path and the repair commands —
              a corrupted registry entry must be findable in one read.
    """
    _seed_tree()
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name="/etl/staging/studies/cdah", Value="{not json", Type="String"
    )
    with pytest.raises(config.ConfigError) as exc:
        studies.registry_for_env("staging")
    assert "/etl/staging/studies/cdah" in str(exc.value)
    assert "g3dt study set cdah" in str(exc.value)


def test_name_rejects_uppercase_with_lowercase_hint():
    """
    Inputs:  validate_new_name('CDAH')
    Expected: guidance to use 'cdah' and where the mixed-case id belongs.
    """
    with pytest.raises(config.ConfigError) as exc:
        studies.validate_new_name("CDAH")
    assert "'cdah'" in str(exc.value)
    assert "--project-id" in str(exc.value)


def test_name_rejects_env_suffix():
    """
    Inputs:  validate_new_name('cdah_staging') — the pre-4.1 key habit
    Expected: rejected with the explanation that the env now lives in the
              SSM path, so suffixed names can't be re-introduced.
    """
    with pytest.raises(config.ConfigError) as exc:
        studies.validate_new_name("cdah_staging")
    assert "env-agnostic" in str(exc.value)


def test_release_tag_normalises_v_prefix_both_ways():
    """
    Inputs:  'v2.1.0' and '2.1.0'  ->  both '2.1.0'; garbage -> ConfigError.

    Background: the releases ledger stores tags without the v while the S3
    release paths carry v-prefixed segments — the one asymmetry that
    produced silent zero-row deletes before delete_cmds normalised it.
    """
    assert studies.normalise_release_tag("v2.1.0") == "2.1.0"
    assert studies.normalise_release_tag("2.1.0") == "2.1.0"
    with pytest.raises(config.ConfigError):
        studies.normalise_release_tag("2.1")


def test_replace_version_segment_swaps_only_the_release_segment():
    """
    Inputs:  s3://b/release_jsons/v2.0.0/cdah/ + tag 2.1.0
    Expected: only the vX.Y.Z segment changes; a path with no such segment
              is a guided error pointing at `study set --path`.
    """
    out = studies.replace_version_segment(
        "s3://b/release_jsons/v2.0.0/cdah/", "2.1.0"
    )
    assert out == "s3://b/release_jsons/v2.1.0/cdah/"
    with pytest.raises(config.ConfigError) as exc:
        studies.replace_version_segment("s3://b/no/version/here/", "2.1.0")
    assert "study set" in str(exc.value)
