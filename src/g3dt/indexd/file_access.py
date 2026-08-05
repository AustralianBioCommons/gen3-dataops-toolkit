"""Verify that registered file objects are actually downloadable.

Walks the full chain a user hits when they click a file in the portal:

    Indexd record -> storage URLs -> DRS object -> access methods -> signed URL

and reports PASS/FAIL per object. Two failure modes this catches that nothing
else does:

  - the Indexd record exists but has no URLs / DRS access methods, so the
    object can never be downloaded;
  - the object has a storage URL but Fence fails to sign a download URL
    (e.g. HTTP 500 from the access endpoint).

**The environment selects the credential, and the credential selects the
commons.** The caller supplies an env; its ``aws_secret_name`` yields the Gen3
API key (Secrets Manager, or a local file for the documented absolute-path
convention), and the commons URL is derived from that key's JWT ``iss`` claim.
There is therefore no URL for an operator to get wrong.

That matters for a concrete reason: :class:`gen3.auth.Gen3Auth` silently falls
back to the Workspace Token Service whenever an explicitly-passed ``endpoint``
disagrees with a ``refresh_file``'s issuer (see ``gen3/auth.py``, the
``elif refresh_file:`` branch). WTS is not deployed on these commons (e.g.
``commons.example.org``), so that path fails with a misleading
``502 Bad Gateway`` on ``/wts/external_oidc/``. Constructing
``Gen3Auth(refresh_token=<key dict>)`` with no ``endpoint`` never enters that
branch, which makes the mismatch structurally impossible rather than merely
guarded.
"""
from __future__ import annotations

import json
from typing import Any, Callable, List, Optional, Tuple

import requests
from gen3.auth import Gen3Auth

from g3dt import config as g3dt_config
from g3dt import resolver
from g3dt.upload.metadata_submitter import (
    commons_url_from_jwt,
    create_boto3_session,
    get_gen3_api_key_aws_secret,
)


def registry_sample_sql(
    database: str, table: str, indexd_endpoint: str, limit: int
) -> str:
    """Build the SQL that samples the most recently registered objects.

    Latest revision per ``baseid`` only (re-registering a file creates a new
    ``did`` under the same ``baseid``; older revisions are superseded and
    would fail a download check spuriously), filtered to one commons via
    ``indexd_endpoint`` — that filter is what keeps another environment's
    registrations out of the sample when environments share a registry table.
    """
    return f"""
        SELECT did FROM (
            SELECT did, registered_at,
                ROW_NUMBER() OVER (
                    PARTITION BY baseid
                    ORDER BY registered_at DESC
                ) AS row_num
            FROM "{database}"."{table}"
            WHERE indexd_endpoint = '{indexd_endpoint}'
        )
        WHERE row_num = 1
        ORDER BY registered_at DESC
        LIMIT {int(limit)}
    """


def sample_recent_guids(env_cfg, commons_url: str, limit: int) -> List[str]:
    """Return the ``limit`` most recently registered GUIDs for a commons.

    Queries the env's indexd registry table (Athena) with the env's AWS
    profile. Every name comes from the resolver: the registry is the
    conventional ``indexd_registry`` table in the env's metadata Glue DB, and
    the Athena output location / workgroup are the env's own.

    The registry may live in a different AWS account than the commons being
    checked, so the env must be one whose ``aws_profile`` can reach it —
    otherwise pass explicit GUIDs and skip sampling entirely.

    The returned ``did`` values already include their prefix, so they feed
    straight into :func:`verify_objects`.
    """
    from g3dt.utils.athena_utils import AthenaConfig, AthenaQuery

    rc = resolver.resolve(
        g3dt_config.require_project(),
        g3dt_config.env_base(env_cfg.name),
        profile=env_cfg.aws_profile,
    )
    database = rc.metadata_db
    table = g3dt_config.INDEXD_REGISTRY_TABLE

    sql = registry_sample_sql(
        database, table, f"{commons_url}/index/index", limit
    )
    query = AthenaQuery(
        AthenaConfig(
            aws_region=env_cfg.region,
            aws_profile=env_cfg.aws_profile,
            athena_s3_output=rc.athena_output_location,
            workgroup=rc.athena_workgroup,
        )
    )
    try:
        df = query.query_athena(sql, database, ctas_approach=False)
    except Exception as exc:
        raise RuntimeError(
            f"could not query {database}.{table} with profile "
            f"'{env_cfg.aws_profile}': {exc}. The indexd registry may live "
            "in a different AWS account than the commons — use an env whose "
            "AWS profile can reach the registry, or pass explicit GUIDs to "
            "check-download instead."
        ) from exc
    return df["did"].dropna().tolist()


def api_key_for_env(env_cfg, key_path: Optional[str] = None) -> dict:
    """Load the Gen3 API key an environment authenticates with.

    Precedence:
      1. ``key_path`` — an explicit local key file (break-glass override);
      2. the env's ``aws_secret_name`` as an absolute path (local file — the
         documented path-style convention, as in ``register_indexd.py``);
      3. the env's ``aws_secret_name`` as a Secrets Manager secret.

    Args:
        env_cfg: Resolved :class:`~g3dt.config.EnvConfig`.
        key_path: Optional path to a local Gen3 API key JSON file.

    Returns:
        dict: The parsed API key, e.g. ``{"api_key": "<JWT>", ...}``.
    """
    if key_path:
        with open(key_path, "r") as handle:
            return json.load(handle)

    secret_name = env_cfg.aws_secret_name
    if secret_name.startswith("/"):
        with open(secret_name, "r") as handle:
            return json.load(handle)

    session = create_boto3_session(
        aws_profile=env_cfg.aws_profile,
        aws_region=env_cfg.region,
    )
    return get_gen3_api_key_aws_secret(
        secret_name=secret_name,
        region_name=env_cfg.region,
        session=session,
    )


def commons_auth(api_key: dict) -> Tuple[str, Gen3Auth]:
    """Return the commons URL and an authenticated client for an API key.

    Both are derived from the same token, so they cannot disagree. Note the
    deliberate absence of an ``endpoint`` argument to ``Gen3Auth`` — passing
    one alongside a credential is what triggers the Workspace Token Service
    fallback described in the module docstring.

    Returns:
        tuple: ``(commons_url, Gen3Auth)``.
    """
    return commons_url_from_jwt(api_key["api_key"]), Gen3Auth(refresh_token=api_key)


def get_json(
    commons_url: str, auth: Gen3Auth, path: str
) -> Tuple[requests.Response, Any]:
    """GET a commons path, returning the response and its parsed body."""
    response = requests.get(f"{commons_url}{path}", auth=auth, timeout=30)
    try:
        body = response.json()
    except ValueError:
        body = response.text
    return response, body


def verify_object(
    commons_url: str,
    auth: Gen3Auth,
    object_id: str,
    emit: Callable[[str], None] = print,
) -> bool:
    """Return True if one object is fully downloadable, else explain why not.

    Args:
        commons_url: Base URL of the commons to check.
        auth: Authenticated Gen3 client.
        object_id: The object GUID, e.g. ``PREFIX/<uuid>``.
        emit: Where to write progress (defaults to ``print``).
    """
    emit(f"\n{'=' * 80}")
    emit(f"Checking: {object_id}")

    index_response, index_record = get_json(commons_url, auth, f"/index/{object_id}")
    emit(f"Indexd status: {index_response.status_code}")
    if not index_response.ok:
        emit(str(index_record))
        emit("FAIL: no Indexd record.")
        return False

    urls = index_record.get("urls", [])
    emit(f"Indexd DID: {index_record.get('did')}")
    emit(f"Indexd URLs: {json.dumps(urls, indent=2)}")

    drs_response, drs_object = get_json(
        commons_url, auth, f"/ga4gh/drs/v1/objects/{object_id}"
    )
    emit(f"DRS object status: {drs_response.status_code}")
    if not drs_response.ok:
        emit(str(drs_object))
        emit("FAIL: no DRS object.")
        return False

    access_methods = drs_object.get("access_methods", [])
    emit(f"DRS access methods: {json.dumps(access_methods, indent=2)}")

    if not urls or not access_methods:
        emit(
            "FAIL: the Indexd record has no usable storage location, "
            "so the object cannot be downloaded."
        )
        return False

    ok = True
    for method in access_methods:
        access_id = method.get("access_id")
        if not access_id:
            continue

        access_response, access_body = get_json(
            commons_url, auth, f"/ga4gh/drs/v1/objects/{object_id}/access/{access_id}"
        )
        emit(f"Access endpoint ({access_id}) status: {access_response.status_code}")
        emit(
            "Access response: "
            + (
                json.dumps(access_body, indent=2)
                if isinstance(access_body, (dict, list))
                else str(access_body)
            )
        )
        if not access_response.ok:
            emit(
                "FAIL: the object has a storage URL, but the server "
                "failed to generate a usable download URL."
            )
            ok = False

    if ok:
        emit("PASS: object is downloadable.")
    return ok


def verify_objects(
    commons_url: str,
    auth: Gen3Auth,
    object_ids: List[str],
    emit: Callable[[str], None] = print,
) -> List[str]:
    """Verify several objects; return the GUIDs that failed (empty if all pass)."""
    return [
        object_id
        for object_id in object_ids
        if not verify_object(commons_url, auth, object_id, emit=emit)
    ]
