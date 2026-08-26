"""SSM-backed study registry — the core behind ``g3dt study``.

The study registry is the control point that defines exactly what metadata a
``g3dt metadata upload`` sends to a commons. Since 4.1.0 it lives in the
env's SSM tree as **operational state**: one JSON String parameter per study
at ``/{project}/{env}/studies/<name>`` holding
``{"project_id", "program_id", "s3_metadata_path"}``.

Why SSM (design doc: docs/design/studies.md):

* The tree path IS the environment — ``/acdc/staging/studies/ausdiab`` and
  ``/acdc/prod/studies/ausdiab`` cannot cross-resolve, which retires the old
  ``{study}_{env}`` key-suffix convention and its bare-key fallback trap.
* One parameter per study makes every write atomic: repointing one study can
  never drop another (the whole-file clobber risk of the legacy
  ``s3://<metadata-bucket>/config/studies.yaml``).
* ``resolver._fetch_params`` is recursive, so the registry rides along in the
  same cached ``get_parameters_by_path`` round-trip every command already
  makes — reads are free and fail loudly (no swallowed auth errors).

Three config categories now exist: INPUTS (the CDK wrapper's ``config/*.json``
in git), deployed OUTPUTS (SSM, written only by ``cdk deploy``), and
OPERATIONAL state (this ``studies/`` subtree plus the Iceberg ledgers —
written by the toolkit, never by deploy).

The legacy S3 ``studies.yaml`` remains as a deprecation-warned read fallback
until 5.0 so an EC2 job box pinned to an older toolkit keeps working during
the transition; ``g3dt study migrate`` imports it and retires the file.
"""
from __future__ import annotations

import difflib
import json
import re
import sys
from typing import Dict, List, Optional, Tuple

from g3dt import config
from g3dt.config import ConfigError, StudyConfig

#: Relative SSM prefix of the registry inside an env's tree.
STUDIES_PREFIX = "studies/"

#: The three fields every study record carries — the exact set
#: ``metadata upload`` / ``delete`` / ``indexd register`` consume.
FIELDS = ("project_id", "program_id", "s3_metadata_path")

#: Canonical study names: lowercase, digits and underscores, letter first.
#: Lowercase is enforced on WRITE; lookups forgive case by lowering first.
NAME_RE = re.compile(r"^[a-z][a-z0-9_]*$")

#: Release tags as the ledger stores them (``2.0.0``, no ``v``) — accepts the
#: ``v``-prefixed form operators naturally type, mirroring
#: ``delete_cmds._normalise_version``.
_RELEASE_TAG_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$", re.IGNORECASE)

#: A path segment that is a release version, e.g. ``v2.0.0``.
_PATH_VERSION_SEGMENT_RE = re.compile(r"^v\d+\.\d+\.\d+$")

# One-shot notice flags (process-scoped; tests reset via reset_warning_flags).
_S3_FALLBACK_WARNED = False
_MARKER_STUDIES_NOTICED = False


def reset_warning_flags() -> None:
    """Test hook: clear the once-per-process deprecation-notice flags."""
    global _S3_FALLBACK_WARNED, _MARKER_STUDIES_NOTICED
    _S3_FALLBACK_WARNED = False
    _MARKER_STUDIES_NOTICED = False


def warn_s3_fallback_once(source: str) -> None:
    """Stderr deprecation warning, at most once per process."""
    global _S3_FALLBACK_WARNED
    if _S3_FALLBACK_WARNED:
        return
    _S3_FALLBACK_WARNED = True
    sys.stderr.write(
        f"DEPRECATED: study registry read from {source} — run "
        f"`g3dt study migrate` to move it into SSM (this fallback is removed "
        f"in 5.0).\n"
    )


def notice_marker_studies_once(marker: dict) -> None:
    """Stderr notice that a marker ``studies:`` block is no longer read."""
    global _MARKER_STUDIES_NOTICED
    if _MARKER_STUDIES_NOTICED or not marker.get("studies"):
        return
    _MARKER_STUDIES_NOTICED = True
    sys.stderr.write(
        "NOTE: the g3dt.yaml studies: block is no longer read (4.1.0) — the "
        "registry lives in SSM. Register studies with `g3dt study add` or "
        "import a legacy registry with `g3dt study migrate`.\n"
    )


# --------------------------------------------------------------------------- #
# Names and lookup candidates                                                  #
# --------------------------------------------------------------------------- #
def validate_new_name(name: str) -> str:
    """Validate a study name for WRITE operations; return it unchanged.

    Raised messages are usage guidance — the CLI maps them to exit 2.
    """
    if name != name.lower():
        raise ConfigError(
            f"Study names are stored lowercase — use '{name.lower()}' "
            f"(the mixed-case Gen3 project_id goes in --project-id)."
        )
    for suffix in config._STUDY_ENV_SUFFIXES:
        if name.endswith(suffix):
            raise ConfigError(
                f"Study names are env-agnostic in 4.1 — drop the '{suffix}' "
                f"suffix (the environment comes from --env; the SSM path "
                f"/{{project}}/{{env}}/studies/ keeps envs apart)."
            )
    if not NAME_RE.match(name):
        raise ConfigError(
            f"Invalid study name '{name}': lowercase letters, digits and "
            f"underscores only, starting with a letter (e.g. 'caughtcad')."
        )
    return name


def strip_env_suffix(key: str, base_env: str) -> str:
    """Strip a trailing ``_{base_env}`` from a legacy-style study key.

    Only THIS env's suffix is stripped: a key suffixed for a different env
    (e.g. ``ausdiab_prod`` seen while resolving staging) stays intact, so it
    can never silently resolve across environments.
    """
    suffix = f"_{base_env}"
    return key[: -len(suffix)] if key.endswith(suffix) else key


def lookup_candidates(study: str, base_env: str) -> List[str]:
    """Names to try when resolving ``study`` (reads are case-forgiving).

    The suffix-stripped candidate keeps the CLI→service wire protocol
    working: the dispatched service scripts re-resolve the key the CLI
    passed, which was ``{study}_{env}`` under toolkits < 4.1.
    """
    lowered = study.lower()
    out = [lowered]
    stripped = strip_env_suffix(lowered, base_env)
    if stripped != lowered:
        out.append(stripped)
    return out


# --------------------------------------------------------------------------- #
# Reading the registry                                                         #
# --------------------------------------------------------------------------- #
def _decode(project: str, env: str, name: str, raw: str) -> StudyConfig:
    """Decode one registry parameter, failing loudly with its exact SSM path."""
    path = f"/{project}/{env}/{STUDIES_PREFIX}{name}"
    try:
        data = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(
            f"SSM parameter {path} is not valid JSON ({exc}). Repair it with "
            f"`g3dt study set {name} --project-id ... --program-id ... "
            f"--path ...` or delete it with `g3dt study remove {name}`."
        )
    missing = [f for f in FIELDS if not data.get(f)]
    if missing:
        raise ConfigError(
            f"SSM parameter {path} is missing field(s): {', '.join(missing)}. "
            f"Repair with `g3dt study set {name} ...`."
        )
    return StudyConfig(
        key=name,
        project_id=data["project_id"],
        program_id=data["program_id"],
        s3_metadata_path=data["s3_metadata_path"],
    )


def studies_from_rc(rc) -> Dict[str, StudyConfig]:
    """The registry held in ``rc``'s already-fetched SSM subtree."""
    out: Dict[str, StudyConfig] = {}
    for key in sorted(rc.params):
        if not key.startswith(STUDIES_PREFIX):
            continue
        name = key[len(STUDIES_PREFIX):]
        if "/" in name:  # future-proof: ignore any nested leaves
            continue
        out[name] = _decode(rc.project, rc.env, name, rc.params[key])
    return out


def registry_for_env(
    env: str, marker: Optional[dict] = None
) -> Tuple[Dict[str, StudyConfig], str, bool]:
    """Return ``(registry, source, used_fallback)`` for an environment.

    Source of truth is the env's SSM ``studies/`` subtree. Only when that
    subtree is EMPTY does the legacy S3 ``studies.yaml`` fallback apply
    (deprecation-warned by callers). Auth/SSM failures propagate — an
    expired session must never read as "no studies configured".
    """
    from g3dt import resolver

    m = marker if marker is not None else config.load_marker()
    project = config.require_project(m)
    base = config.env_base(env)
    profile = None if env.endswith("_ec2") else config.aws_profile_for(base, m)
    rc = resolver.resolve(project, base, profile=profile)

    reg = studies_from_rc(rc)
    ssm_source = f"/{project}/{base}/studies"
    if reg:
        return reg, ssm_source, False

    legacy = config._studies_from_s3(env, m)
    if legacy:
        out: Dict[str, StudyConfig] = {}
        for key, entry in legacy.items():
            entry = entry or {}
            missing = [f for f in FIELDS if not entry.get(f)]
            if missing:
                raise ConfigError(
                    f"Legacy studies.yaml entry '{key}' is missing field(s): "
                    f"{', '.join(missing)} "
                    f"(s3://{rc.metadata_bucket}/{config.STUDIES_S3_KEY})."
                )
            # Strip only THIS env's suffix; other-env keys stay suffixed so
            # they can never silently resolve here (and bulk-upload's
            # is_prod() check on keys still sees them).
            name = strip_env_suffix(str(key).lower(), base)
            out[name] = StudyConfig(
                key=name,
                project_id=entry["project_id"],
                program_id=entry["program_id"],
                s3_metadata_path=entry["s3_metadata_path"],
            )
        return out, f"s3://{rc.metadata_bucket}/{config.STUDIES_S3_KEY}", True

    return {}, ssm_source, False


def not_found_message(
    study: str, env: str, registry: Dict[str, StudyConfig], source: str
) -> str:
    """A guided miss message: real names, a did-you-mean, or setup commands."""
    if not registry:
        return (
            f"Study '{study}' (env '{env}') not found — no studies are "
            f"registered under {source}. Register one with:\n"
            f"    g3dt study add {study.lower()} --project-id <ProjectId> "
            f"--program-id <program> --path s3://.../release_jsons/vX.Y.Z/"
            f"{study.lower()}/\n"
            f"or import a legacy studies.yaml with:\n"
            f"    g3dt study migrate --env {config.env_base(env)}"
        )
    names = sorted(registry)
    close = difflib.get_close_matches(study.lower(), names, n=1, cutoff=0.6)
    hint = f" Did you mean '{close[0]}'?" if close else ""
    return (
        f"Study '{study}' (env '{env}') not found in {source}. "
        f"Registered: {', '.join(names)}.{hint}"
    )


# --------------------------------------------------------------------------- #
# Writing the registry (the toolkit's only SSM writes)                         #
# --------------------------------------------------------------------------- #
def _param_name(rc, name: str) -> str:
    return f"/{rc.project}/{rc.env}/{STUDIES_PREFIX}{name}"


def _clear_caches() -> None:
    """Every successful write invalidates the process-wide resolver cache,
    so an add/set followed by a list in the same process sees fresh state."""
    from g3dt import resolver

    resolver.resolve.cache_clear()


def put_study(
    rc,
    session,
    name: str,
    *,
    project_id: str,
    program_id: str,
    s3_metadata_path: str,
    overwrite: bool,
) -> None:
    """Write one study record. ``overwrite=False`` refuses an existing name."""
    from botocore.exceptions import ClientError

    value = json.dumps(
        {
            "project_id": project_id,
            "program_id": program_id,
            "s3_metadata_path": s3_metadata_path,
        },
        sort_keys=True,
    )
    try:
        session.client("ssm").put_parameter(
            Name=_param_name(rc, name),
            Value=value,
            Type="String",
            Overwrite=overwrite,
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ParameterAlreadyExists":
            raise ConfigError(
                f"Study '{name}' already exists in {rc.env}. Change its "
                f"fields with `g3dt study set {name} ...`; view it with "
                f"`g3dt study show {name}`."
            )
        raise
    _clear_caches()


def delete_study(rc, session, name: str) -> None:
    """Delete one study record; a missing name gets a guided error."""
    from botocore.exceptions import ClientError

    try:
        session.client("ssm").delete_parameter(Name=_param_name(rc, name))
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code == "ParameterNotFound":
            raise ConfigError(
                f"Study '{name}' is not registered in {rc.env} — nothing to "
                f"remove. See `g3dt study list`."
            )
        raise
    _clear_caches()


# --------------------------------------------------------------------------- #
# Release tags and path repointing                                             #
# --------------------------------------------------------------------------- #
def normalise_release_tag(raw: str) -> str:
    """``v2.1.0`` and ``2.1.0`` both → ``2.1.0``; anything else is a usage error.

    The releases ledger stores tags WITHOUT the ``v`` (the ``data-v`` prefix
    is stripped at release time), while the S3 release paths carry ``v{tag}``
    segments — this is the one place that asymmetry is handled.
    """
    match = _RELEASE_TAG_RE.match(raw.strip())
    if not match:
        raise ConfigError(
            f"Invalid release '{raw}': expected x.y.z or vx.y.z (e.g. 2.1.0)."
        )
    return match.group(1)


def replace_version_segment(path: str, tag: str) -> str:
    """Swap the last ``vX.Y.Z`` path segment for ``v{tag}``.

    e.g. ``s3://b/release_jsons/v2.0.0/cdah/`` + ``2.1.0`` →
    ``s3://b/release_jsons/v2.1.0/cdah/``.
    """
    parts = path.split("/")
    for i in range(len(parts) - 1, -1, -1):
        if _PATH_VERSION_SEGMENT_RE.match(parts[i]):
            parts[i] = f"v{tag}"
            return "/".join(parts)
    raise ConfigError(
        f"Path '{path}' has no vX.Y.Z release segment to repoint — set the "
        f"full path instead with `g3dt study set <name> --path s3://...`."
    )


def validate_upload_prefix(path: str, session) -> int:
    """Prove a metadata path is uploadable; return its node-JSON count.

    Runs the EXACT checks ``metadata upload`` performs (same helpers), so
    repoint and upload can never disagree about what a valid target is.
    """
    from botocore.exceptions import ClientError

    from g3dt.upload.metadata_submitter import (
        find_data_import_order_file_s3,
        list_metadata_jsons_s3,
    )

    if not path.startswith("s3://"):
        raise ConfigError(f"'{path}' is not an s3:// URI.")
    try:
        find_data_import_order_file_s3(path, session)
        jsons = list_metadata_jsons_s3(path, session)
    except FileNotFoundError:
        raise ConfigError(
            f"No DataImportOrder.txt under {path} — has this release been "
            f"exported? Check `aws s3 ls {path}`."
        )
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        raise ConfigError(f"Cannot list {path} ({code or exc}).")
    if not jsons:
        raise ConfigError(
            f"No node *.json files under {path} — the release export looks "
            f"empty. Check `aws s3 ls {path}`."
        )
    return len(jsons)


def latest_release_tag(rc, profile: Optional[str]) -> Optional[str]:
    """Newest ``release_tag`` in the env's releases ledger, or ``None``.

    ``None`` covers both "table missing" and "no rows" — a normal state for
    an env that has never cut a release; the CLI turns it into guidance.
    """
    from g3dt.utils.athena_utils import AthenaConfig, AthenaQuery

    try:
        cfg = AthenaConfig(
            aws_region=rc.region,
            aws_profile=profile,
            athena_s3_output=rc.athena_output_location,
            workgroup=rc.athena_workgroup,
        )
        sql = (
            f'SELECT max(release_tag) AS tag '
            f'FROM "{rc.release_db}"."{rc.release_table}"'
        )
        df = AthenaQuery(cfg).query_athena(sql, rc.release_db, ctas_approach=False)
    except Exception:
        # Missing release/* SSM leaves, a missing table, and a genuinely
        # empty ledger all mean the same thing to the caller: no release to
        # point at yet. The CLI turns None into guidance.
        return None
    if df.empty or df.iloc[0]["tag"] is None:
        return None
    tag = str(df.iloc[0]["tag"])
    return tag or None


# --------------------------------------------------------------------------- #
# Legacy studies.yaml (migrate + fallback support)                             #
# --------------------------------------------------------------------------- #
MIGRATED_S3_KEY = config.STUDIES_S3_KEY + ".migrated"


def load_legacy_yaml(session, bucket: str) -> Optional[dict]:
    """The legacy registry map, or ``None`` when no file exists.

    Auth and other client errors propagate — only a genuinely absent file is
    ``None``.
    """
    import yaml
    from botocore.exceptions import ClientError

    try:
        body = session.client("s3").get_object(
            Bucket=bucket, Key=config.STUDIES_S3_KEY
        )["Body"].read()
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code in ("NoSuchKey", "404"):
            return None
        raise
    data = yaml.safe_load(body) or {}
    return data.get("studies", data)


def legacy_migrated_exists(session, bucket: str) -> bool:
    from botocore.exceptions import ClientError

    try:
        session.client("s3").head_object(Bucket=bucket, Key=MIGRATED_S3_KEY)
        return True
    except ClientError:
        return False


def rename_legacy(session, bucket: str) -> None:
    """Retire the legacy file: copy to ``.migrated`` then delete the original."""
    s3 = session.client("s3")
    s3.copy_object(
        Bucket=bucket,
        Key=MIGRATED_S3_KEY,
        CopySource={"Bucket": bucket, "Key": config.STUDIES_S3_KEY},
    )
    s3.delete_object(Bucket=bucket, Key=config.STUDIES_S3_KEY)
