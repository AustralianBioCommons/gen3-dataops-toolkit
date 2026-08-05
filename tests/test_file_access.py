"""Tests for the indexd file-access verification logic.

These cover how the Gen3 credential and the commons URL are resolved (the part
that caused a real incident — see test_commons_auth_never_passes_endpoint), and
the four outcomes of walking the download chain. All AWS and HTTP calls are
mocked; nothing touches a real commons.
"""
import base64
import json
from unittest.mock import MagicMock, patch

import pytest

from g3dt.config import EnvConfig
from g3dt.indexd.file_access import (
    api_key_for_env,
    commons_auth,
    verify_object,
    verify_objects,
)
from g3dt.resolver import ResolvedConfig

MODULE = "g3dt.indexd.file_access"


def _b64(obj):
    return base64.urlsafe_b64encode(json.dumps(obj).encode()).decode().rstrip("=")


def _fake_jwt(iss):
    """Build a structurally valid (unsigned) JWT carrying an `iss` claim."""
    header = _b64({"alg": "RS256", "typ": "JWT"})
    sig = base64.urlsafe_b64encode(b"sig").decode().rstrip("=")
    return f"{header}.{_b64({'iss': iss})}.{sig}"


STAGING_KEY = {"api_key": _fake_jwt("https://staging.commons.example.org/user")}


def _env_cfg(secret_name="gen3_api_key_staging"):
    return EnvConfig(
        name="staging",
        is_ec2=False,
        region="ap-southeast-2",
        dictionary_version="v1",
        aws_profile="etl_staging",
        aws_secret_name=secret_name,
        schema_s3_uri="u",
        domain="d",
        app_name="a",
        namespace="n",
        cluster_name="c",
        schema_repo="Org/schema-repo",
    )


def _response(status, body):
    """A stand-in for a requests.Response with a JSON body."""
    resp = MagicMock()
    resp.status_code = status
    resp.ok = 200 <= status < 300
    resp.json.return_value = body
    return resp


# ---------------------------------------------------------------------------
# Credential resolution
# ---------------------------------------------------------------------------

@patch(f"{MODULE}.get_gen3_api_key_aws_secret")
@patch(f"{MODULE}.create_boto3_session")
def test_api_key_for_env_fetches_the_envs_secret(mock_session, mock_load):
    """
    Test that the environment's configured secret is what gets loaded.

    Background:
        The CLI passes only an env; everything else must be derived from the
        env's config so a staging run can never authenticate with a prod key.

    Inputs:  an EnvConfig whose aws_secret_name is 'gen3_api_key_staging'
    Expected Output:
      - a boto3 session built with the env's profile and region
      - get_gen3_api_key_aws_secret called with that secret name and region
      - the returned key dict passed straight back to the caller
    """
    mock_load.return_value = STAGING_KEY

    result = api_key_for_env(_env_cfg())

    assert result is STAGING_KEY
    mock_session.assert_called_once_with(
        aws_profile="etl_staging", aws_region="ap-southeast-2"
    )
    assert mock_load.call_args.kwargs["secret_name"] == "gen3_api_key_staging"
    assert mock_load.call_args.kwargs["region_name"] == "ap-southeast-2"


@patch(f"{MODULE}.get_gen3_api_key_aws_secret")
@patch(f"{MODULE}.create_boto3_session")
def test_api_key_for_env_prefers_an_explicit_key_file(mock_session, mock_load, tmp_path):
    """
    Test the break-glass local key override.

    Background:
        An operator debugging a broken secret needs a way to test with a key
        downloaded from the portal. When that path is given, no AWS call should
        happen at all.

    Inputs:  key_path pointing at a local JSON key file
    Expected Output: the file's contents are returned; Secrets Manager and the
    boto3 session are never touched.
    """
    key_file = tmp_path / "key.json"
    key_file.write_text(json.dumps(STAGING_KEY))

    result = api_key_for_env(_env_cfg(), key_path=str(key_file))

    assert result == STAGING_KEY
    mock_session.assert_not_called()
    mock_load.assert_not_called()


@patch(f"{MODULE}.get_gen3_api_key_aws_secret")
@patch(f"{MODULE}.create_boto3_session")
def test_api_key_for_env_reads_path_style_secret_name_as_local_file(
    mock_session, mock_load, tmp_path
):
    """
    Test the documented absolute-path convention for aws_secret_name.

    Background:
        An env may declare its aws_secret_name as an absolute path — the
        dual-mode convention register_indexd.py already honours: a path-style
        value is a local Gen3 API key file, anything else is a Secrets
        Manager secret name. check-download must follow the same rule so an
        env that registers files can always verify them.

    Inputs:  an EnvConfig whose aws_secret_name is '/abs/path/key.json'
    Expected Output: the file's contents are returned; no AWS call happens.
    """
    key_file = tmp_path / "key.json"
    key_file.write_text(json.dumps(STAGING_KEY))

    result = api_key_for_env(_env_cfg(secret_name=str(key_file)))

    assert result == STAGING_KEY
    mock_session.assert_not_called()
    mock_load.assert_not_called()


# ---------------------------------------------------------------------------
# Commons resolution — the incident this feature exists to prevent
# ---------------------------------------------------------------------------

@patch(f"{MODULE}.Gen3Auth")
def test_commons_auth_derives_url_from_the_key(mock_gen3auth):
    """
    Test that the commons URL comes from the API key's JWT issuer.

    Background:
        The key IS the environment selector. A staging key must produce the
        staging commons without anyone typing a URL.

    Inputs:  an API key whose iss is 'https://staging.commons.example.org/user'
    Expected Output: commons == 'https://staging.commons.example.org'
    """
    commons, _auth = commons_auth(STAGING_KEY)
    assert commons == "https://staging.commons.example.org"


@patch(f"{MODULE}.Gen3Auth")
def test_commons_auth_never_passes_endpoint(mock_gen3auth):
    """
    Test that Gen3Auth is constructed with the key alone — no endpoint.

    Background:
        This is the whole reason the command resolves credentials this way.
        gen3/auth.py switches to the Workspace Token Service whenever an
        explicit `endpoint` disagrees with a credential's issuer. WTS is not
        deployed on these commons, so that path fails with a misleading
        '502 Bad Gateway' on /wts/external_oidc/ — which reads like broken
        object_id links but is purely a client-side mismatch. Passing
        refresh_token and NO endpoint never enters that branch, making the
        mismatch structurally impossible instead of merely guarded.

    Inputs:  a staging API key
    Expected Output: Gen3Auth called once with refresh_token=<key> only; no
    'endpoint' and no 'refresh_file' keyword anywhere in the call.
    """
    commons_auth(STAGING_KEY)

    mock_gen3auth.assert_called_once_with(refresh_token=STAGING_KEY)
    assert "endpoint" not in mock_gen3auth.call_args.kwargs
    assert "refresh_file" not in mock_gen3auth.call_args.kwargs


# ---------------------------------------------------------------------------
# The download chain
# ---------------------------------------------------------------------------

@patch(f"{MODULE}.requests.get")
def test_verify_object_passes_when_the_whole_chain_resolves(mock_get):
    """
    Test the happy path: Indexd -> DRS -> signed URL all succeed.

    Inputs:  an Indexd record with a storage URL, a DRS object with one access
             method, and an access endpoint that returns a signed URL
    Expected Output: True, and the final line emitted is the PASS message.
    """
    mock_get.side_effect = [
        _response(200, {"did": "PREFIX/aaa", "urls": ["s3://bucket/f.csv"]}),
        _response(200, {"access_methods": [{"access_id": "s3"}]}),
        _response(200, {"url": "https://signed"}),
    ]
    lines = []

    assert verify_object("https://commons", MagicMock(), "PREFIX/aaa", emit=lines.append)
    assert lines[-1] == "PASS: object is downloadable."


@patch(f"{MODULE}.requests.get")
def test_verify_object_fails_when_there_is_no_indexd_record(mock_get):
    """
    Test the unregistered-object case.

    Inputs:  Indexd returns 404
    Expected Output: False, and no further calls are made (no point asking DRS
    about an object that does not exist).
    """
    mock_get.side_effect = [_response(404, {"error": "no record"})]
    lines = []

    assert not verify_object("https://commons", MagicMock(), "PREFIX/aaa", emit=lines.append)
    assert mock_get.call_count == 1
    assert "FAIL: no Indexd record." in lines


@patch(f"{MODULE}.requests.get")
def test_verify_object_fails_when_the_record_has_no_storage_location(mock_get):
    """
    Test the 'registered but undownloadable' failure mode.

    Background:
        An Indexd record can exist with an empty `urls` list — the metadata
        looks fine in the portal, but there is nothing to download. This is one
        of the two real failure modes this checker exists to catch.

    Inputs:  Indexd 200 with urls=[]; DRS 200 with access_methods=[]
    Expected Output: False, with the 'no usable storage location' explanation.
    """
    mock_get.side_effect = [
        _response(200, {"did": "PREFIX/aaa", "urls": []}),
        _response(200, {"access_methods": []}),
    ]
    lines = []

    assert not verify_object("https://commons", MagicMock(), "PREFIX/aaa", emit=lines.append)
    assert any("no usable storage location" in line for line in lines)


@patch(f"{MODULE}.requests.get")
def test_verify_object_fails_when_fence_cannot_sign(mock_get):
    """
    Test the 'storage URL exists but Fence errors' failure mode.

    Background:
        The second real failure mode: the record and DRS object are healthy,
        but the access endpoint 500s, so a user still cannot download the file.

    Inputs:  a healthy record and DRS object, access endpoint returns 500
    Expected Output: False, with the 'failed to generate a usable download URL'
    explanation.
    """
    mock_get.side_effect = [
        _response(200, {"did": "PREFIX/aaa", "urls": ["s3://bucket/f.csv"]}),
        _response(200, {"access_methods": [{"access_id": "s3"}]}),
        _response(500, {"error": "service failure"}),
    ]
    lines = []

    assert not verify_object("https://commons", MagicMock(), "PREFIX/aaa", emit=lines.append)
    assert any("failed to generate a usable download URL" in line for line in lines)


@patch(f"{MODULE}.verify_object")
def test_verify_objects_returns_only_the_failed_guids(mock_verify):
    """
    Test that the caller can derive an exit code from the result.

    Background:
        The script exits non-zero when any object fails, so this command can
        gate a deployment step. That depends on failures being reported
        precisely rather than as a bare boolean.

    Inputs:  three GUIDs where the middle one fails
    Expected Output: ['PREFIX/bbb'] — only the failure, order preserved.
    """
    mock_verify.side_effect = [True, False, True]

    failures = verify_objects(
        "https://commons", MagicMock(), ["PREFIX/aaa", "PREFIX/bbb", "PREFIX/ccc"]
    )

    assert failures == ["PREFIX/bbb"]


# ---------------------------------------------------------------------------
# Registry sampling (auto-extracted GUIDs for check-download)
# ---------------------------------------------------------------------------

def _rc():
    """The slice of the env's SSM tree that sampling reads (resolver names)."""
    return ResolvedConfig(
        project="etl",
        env="staging",
        params={
            "glue/db/metadata": "etl_staging_metadata_db",
            "athena/outputLocation": "s3://etl-staging-metadata/athena-query-results/",
            "athena/workgroup": "etl_staging_wg",
        },
    )


def test_registry_sample_sql_scopes_to_one_commons_and_latest_revision():
    """
    Test that the sampling SQL cannot leak objects from another environment
    or return superseded revisions.

    Background:
        Environments can write to a shared registry table, distinguished
        only by the indexd_endpoint column — without that filter a "prod"
        check would happily sample staging objects and prove nothing. And
        re-registering a file creates a new did under the same baseid, so
        without the latest-revision window function the sample could contain
        old dids that legitimately no longer download.

    Inputs:  database, table, a prod indexd endpoint, limit 10
    Expected Output: SQL containing the endpoint equality filter, the
    ROW_NUMBER window partitioned by baseid, and LIMIT 10.
    """
    from g3dt.indexd.file_access import registry_sample_sql

    sql = registry_sample_sql(
        "etl_staging_metadata_db",
        "indexd_registry",
        "https://commons.example.org/index/index",
        10,
    )

    assert "indexd_endpoint = 'https://commons.example.org/index/index'" in sql
    assert "PARTITION BY baseid" in sql
    assert "row_num = 1" in sql
    assert "LIMIT 10" in sql


@patch("g3dt.utils.athena_utils.AthenaQuery")
@patch(f"{MODULE}.resolver.resolve")
@patch(f"{MODULE}.g3dt_config.require_project", return_value="etl")
def test_sample_recent_guids_returns_dids_for_the_derived_endpoint(
    _project, mock_resolve, mock_query_cls
):
    """
    Test that sampling queries the registry with the env's own AWS session
    and resolver-provided names, and returns plain did strings ready for
    verify_objects.

    Background:
        The dids in the registry already include the "PREFIX/" prefix, so
        the sample must be forwarded untouched — historically operators
        double-prefixed GUIDs pasted from Athena and got spurious 404s. The
        registry database, Athena output location and workgroup all come
        from SSM via the resolver — nothing is hard-coded.

    Inputs:  a resolver returning the env's metadata DB and Athena names, a
             staging env, commons https://staging.commons.example.org, limit 2
    Expected Output: the two dids from the query result, in order; the SQL
    passed to Athena filters on the commons URL + /index/index; the resolver
    is asked for this project/env with the env's profile.
    """
    import pandas as pd

    from g3dt.indexd.file_access import sample_recent_guids

    mock_resolve.return_value = _rc()
    mock_query_cls.return_value.query_athena.return_value = pd.DataFrame(
        {"did": ["PREFIX/aaa", "PREFIX/bbb"]}
    )

    guids = sample_recent_guids(
        _env_cfg(), "https://staging.commons.example.org", 2
    )

    assert guids == ["PREFIX/aaa", "PREFIX/bbb"]
    sql = mock_query_cls.return_value.query_athena.call_args[0][0]
    assert "https://staging.commons.example.org/index/index" in sql
    assert '"etl_staging_metadata_db"."indexd_registry"' in sql
    mock_resolve.assert_called_once_with("etl", "staging", profile="etl_staging")
    athena_cfg = mock_query_cls.call_args[0][0]
    assert athena_cfg.athena_s3_output == (
        "s3://etl-staging-metadata/athena-query-results/"
    )
    assert athena_cfg.workgroup == "etl_staging_wg"


@patch("g3dt.utils.athena_utils.AthenaQuery")
@patch(f"{MODULE}.resolver.resolve")
@patch(f"{MODULE}.g3dt_config.require_project", return_value="etl")
def test_sample_recent_guids_explains_cross_account_athena_failures(
    _project, mock_resolve, mock_query_cls
):
    """
    Test that an Athena failure surfaces as advice, not a raw stack trace.

    Background:
        The registry table may live in a different AWS account than the
        commons being checked (e.g. a prod commons whose registry sits in a
        shared staging account). Running check-download with a profile that
        authenticates against the wrong account is the most likely sampling
        failure in practice. The error must tell the operator the two ways
        out: use an env whose profile can reach the registry, or pass
        explicit GUIDs and skip sampling.

    Inputs:  AthenaQuery.query_athena raising an exception
    Expected Output: RuntimeError explaining the cross-account possibility
    and naming explicit GUIDs as the fallback.
    """
    from g3dt.indexd.file_access import sample_recent_guids

    mock_resolve.return_value = _rc()
    mock_query_cls.return_value.query_athena.side_effect = Exception(
        "EntityNotFoundException: Database etl_staging_metadata_db not found"
    )

    with pytest.raises(RuntimeError) as excinfo:
        sample_recent_guids(
            _env_cfg(), "https://staging.commons.example.org", 10
        )

    assert "different AWS account" in str(excinfo.value)
    assert "explicit GUIDs" in str(excinfo.value)
