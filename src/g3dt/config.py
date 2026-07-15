"""Configuration resolution for the g3dt CLI and services.

The platform has exactly two kinds of configuration:

* **INPUTS** — human-authored values, committed as
  ``config/<projectId>.<env>.json`` in the CDK repo (gen3-aws-data-pipeline)
  and read only by ``cdk deploy``. The toolkit never reads these files.
* **OUTPUTS** — every resource name the CDK creates, plus the mirrored Gen3
  app facts, published to SSM Parameter Store under ``/{project}/{env}/...``
  on deploy. The toolkit resolves everything from there at runtime
  (see :mod:`g3dt.resolver`).

The only local configuration is a tiny bootstrap **marker** — ``g3dt.yaml`` —
that tells the CLI which project/region to resolve (plus optional per-env AWS
profile names and the study registry). Search order: ``./g3dt.yaml`` →
``~/.g3dt/g3dt.yaml`` → ``/etc/g3dt/g3dt.yaml`` (the EC2 job box's copy,
written by CDK user-data). Environment variables override the file:
``G3DT_PROJECT``, ``AWS_REGION``, ``G3DT_DEFAULT_ENV``; ``G3DT_MARKER`` points
at an explicit marker path.

Design notes
------------
* ``resolve_env`` returns the same frozen :class:`EnvConfig` the pre-2.0 CLI
  used, so command groups and services are agnostic to where values came from.
* Studies are project data, not CDK-created names, so they stay in the marker
  (``studies:`` block) for now. ``resolve_study`` keeps the safety-critical
  ``{study}_{env_base}`` derivation that keeps staging data out of prod.
"""
from __future__ import annotations

import functools
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

DEFAULT_REGION = "ap-southeast-2"

#: Marker locations, most specific first.
MARKER_PATHS = ("g3dt.yaml", "~/.g3dt/g3dt.yaml", "/etc/g3dt/g3dt.yaml")

#: Marker keys `g3dt config set` may write.
SETTABLE_MARKER_KEYS = ("project", "region", "default_env")

#: Gen3 app facts mirrored to SSM /{project}/{env}/app/* by the CDK.
REQUIRED_APP_KEYS = (
    "dictionary_version",
    "aws_secret_name",
    "schema_s3_uri",
    "domain",
    "app_name",
    "namespace",
    "cluster_name",
    "schema_repo",
)

#: Operational table conventions. Fixed names, exactly like the CDK's
#: `releases` table (lib/names.ts) — they live in the env's metadata Glue DB
#: and under the env's metadata bucket, both resolved from SSM.
METADATA_UPLOAD_TABLE = "metadata_upload_iceberg"
METADATA_UPLOAD_PREFIX = "metadata_upload/"
FILE_METADATA_TABLE = "file_metadata"
INDEXD_REGISTRY_TABLE = "indexd_registry"
INDEXD_PREFIX = "indexd/"

#: Where a project's study registry lives when it isn't in the local marker:
#: s3://<metadata-bucket>/<STUDIES_S3_KEY> (see resolve_study).
STUDIES_S3_KEY = "config/studies.yaml"

#: Suffixes used to strip the environment from a study key.
_STUDY_ENV_SUFFIXES = ("_staging", "_prod", "_test")


class ConfigError(KeyError):
    """Raised when an env/study cannot be resolved or required keys are missing.

    Subclasses ``KeyError`` so existing ``except KeyError`` handlers still catch
    it, but carries a human-readable message that doubles as CLI help.
    """

    def __str__(self) -> str:  # KeyError repr adds quotes; we want the raw text
        return self.args[0] if self.args else ""


# --------------------------------------------------------------------------- #
# The bootstrap marker (g3dt.yaml)                                             #
# --------------------------------------------------------------------------- #
def marker_path() -> Optional[Path]:
    """Return the first marker file that exists, or ``None``.

    ``$G3DT_MARKER`` overrides the search entirely (useful in tests and CI).
    """
    override = os.getenv("G3DT_MARKER")
    if override:
        return Path(override).expanduser()
    for candidate in MARKER_PATHS:
        p = Path(candidate).expanduser()
        if p.is_file():
            return p
    return None


@functools.lru_cache(maxsize=None)
def _load_yaml_cached(path_str: str) -> dict:
    with open(path_str, "r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def load_marker() -> dict:
    """Read the g3dt.yaml marker; env vars override file values.

    Returns a dict with at least ``project``/``region``/``default_env`` keys
    (possibly ``None``) plus whatever else the file carries (``profiles``,
    ``studies``, ``ssh_key``, ``ssh_user``).
    """
    path = marker_path()
    data = dict(_load_yaml_cached(str(path))) if path and path.is_file() else {}
    data["project"] = os.getenv("G3DT_PROJECT", data.get("project"))
    data["region"] = os.getenv("AWS_REGION", data.get("region", DEFAULT_REGION))
    data["default_env"] = os.getenv("G3DT_DEFAULT_ENV", data.get("default_env"))
    return data


def require_project(marker: Optional[dict] = None) -> str:
    """Return the project id or fail with setup instructions."""
    m = marker if marker is not None else load_marker()
    project = m.get("project")
    if not project:
        raise ConfigError(
            "No project configured. Create a g3dt.yaml marker (searched: "
            f"{', '.join(MARKER_PATHS)}) with at least:\n"
            "    project: <projectId>\n"
            "    region: ap-southeast-2\n"
            "or set $G3DT_PROJECT. Run `g3dt config set project <id>` to write one."
        )
    return project


def set_marker_value(key: str, value: str) -> Tuple[Optional[str], str, Path]:
    """Set one bootstrap key in the user's marker file and write it back.

    Writes to the existing marker if one is found, else creates
    ``~/.g3dt/g3dt.yaml``. Returns ``(old_value, new_value, path)``.
    """
    if key not in SETTABLE_MARKER_KEYS:
        raise ConfigError(
            f"Key '{key}' is not a settable bootstrap key. "
            f"Settable keys: {', '.join(SETTABLE_MARKER_KEYS)}. "
            f"Deployed settings (dictionary_version, domain, ...) are CDK INPUTS: "
            f"edit config/<project>.<env>.json in gen3-aws-data-pipeline and "
            f"redeploy — the values flow to SSM, not to this file."
        )
    path = marker_path()
    if path is None:
        path = Path("~/.g3dt/g3dt.yaml").expanduser()
    if path.is_file():
        data = dict(_load_yaml_cached(str(path)))
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {}
    old = data.get(key)
    data[key] = value
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    _load_yaml_cached.cache_clear()
    return old, value, path


# --------------------------------------------------------------------------- #
# Environments                                                                 #
# --------------------------------------------------------------------------- #
def env_base(env: str) -> str:
    """Strip a trailing ``_ec2`` suffix: ``staging_ec2`` -> ``staging``."""
    return env[:-4] if env.endswith("_ec2") else env


def aws_profile_for(env: str, marker: Optional[dict] = None) -> Optional[str]:
    """Return the local AWS named profile for ``env``, or ``None`` (ambient).

    Read from the marker's optional ``profiles:`` map, e.g.::

        profiles:
          test: etl_test
          staging: etl_staging

    On the EC2 box / CodeBuild there is no ``profiles`` map, so the default
    credential chain (the instance/build role) is used — by design.
    """
    m = marker if marker is not None else load_marker()
    profiles = m.get("profiles") or {}
    return profiles.get(env_base(env))


@dataclass(frozen=True)
class EnvConfig:
    """Resolved settings for one environment (names from SSM, auth from marker)."""

    name: str
    is_ec2: bool
    region: str
    dictionary_version: str
    aws_profile: Optional[str]
    aws_secret_name: str
    schema_s3_uri: str
    domain: str
    app_name: str
    namespace: str
    cluster_name: str
    schema_repo: str
    ec2_instance_id: Optional[str] = None
    ssh_key: Optional[str] = None
    ssh_user: Optional[str] = None


def resolve_env(env: str, project: Optional[str] = None) -> EnvConfig:
    """Resolve one environment: app INPUT facts + CDK OUTPUT names, from SSM.

    ``env`` may carry a ``_ec2`` suffix (dispatch pseudo-env): SSM has one tree
    per real env, so the suffix is stripped for resolution and recorded as
    ``is_ec2``. An ``*_ec2`` env authenticates with the ambient credential
    chain (``aws_profile=None``) — on the box that is the instance profile.
    """
    from g3dt import resolver  # late import: resolver imports ConfigError from here

    marker = load_marker()
    project = project or require_project(marker)
    base = env_base(env)
    is_ec2 = env.endswith("_ec2")
    profile = None if is_ec2 else aws_profile_for(base, marker)

    rc = resolver.resolve(project, base, profile=profile)

    missing = [k for k in REQUIRED_APP_KEYS if f"app/{k}" not in rc.params]
    if missing:
        raise ConfigError(
            f"Environment '{env}' is missing required app fact(s) in SSM: "
            f"{', '.join(missing)} (expected at /{project}/{base}/app/*). "
            f"Re-run `cdk deploy` in gen3-aws-data-pipeline so the inputs are "
            f"mirrored to SSM."
        )
    return EnvConfig(
        name=env,
        is_ec2=is_ec2,
        region=rc.get("meta/region", marker["region"]),
        dictionary_version=rc.app("dictionary_version"),
        aws_profile=profile,
        aws_secret_name=rc.app("aws_secret_name"),
        schema_s3_uri=rc.app("schema_s3_uri"),
        domain=rc.app("domain"),
        app_name=rc.app("app_name"),
        namespace=rc.app("namespace"),
        cluster_name=rc.app("cluster_name"),
        schema_repo=rc.app("schema_repo"),
        ec2_instance_id=rc.get("ec2/instanceId"),
        ssh_key=(marker.get("ssh_key") if is_ec2 else None),
        ssh_user=(marker.get("ssh_user") if is_ec2 else None),
    )


def list_envs(project: Optional[str] = None) -> List[str]:
    """Return the environments that have a deployed SSM tree for the project."""
    from g3dt import resolver

    marker = load_marker()
    project = project or require_project(marker)
    # Listing spans envs, so authenticate with any configured profile (they all
    # target the same account) or the ambient chain.
    profiles = marker.get("profiles") or {}
    profile = next(iter(profiles.values()), None)
    return resolver.list_envs(project, profile=profile)


def script_env(e: EnvConfig) -> Dict[str, str]:
    """Environment variables for a wrapped service script.

    The pre-2.0 shell scripts parsed the legacy YAML config themselves with
    ``yq``; now the Python caller resolves everything from SSM and hands it
    over as ``G3DT_*`` variables.
    """
    env = dict(os.environ)
    values = {
        "G3DT_ENV": e.name,
        "G3DT_REGION": e.region,
        "G3DT_AWS_PROFILE": e.aws_profile or "",
        "G3DT_DICTIONARY_VERSION": e.dictionary_version,
        "G3DT_AWS_SECRET_NAME": e.aws_secret_name,
        "G3DT_SCHEMA_S3_URI": e.schema_s3_uri,
        "G3DT_DOMAIN": e.domain,
        "G3DT_APP_NAME": e.app_name,
        "G3DT_NAMESPACE": e.namespace,
        "G3DT_CLUSTER_NAME": e.cluster_name,
        "G3DT_SCHEMA_REPO": e.schema_repo,
    }
    env.update({k: v for k, v in values.items() if v is not None})
    return env


# --------------------------------------------------------------------------- #
# Studies (project data — marker-resident for now)                             #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class StudyConfig:
    """Resolved settings for one study (in a given environment)."""

    key: str
    project_id: str
    program_id: str
    s3_metadata_path: str


def list_studies(marker: Optional[dict] = None, env: Optional[str] = None) -> List[str]:
    """Return unique bare study names (env suffix stripped) for friendly help."""
    m = marker if marker is not None else load_marker()
    studies = m.get("studies") or {}
    if not studies and env:
        studies = _studies_from_s3(env, m) or {}
    prefixes = set()
    for key in studies:
        for suffix in _STUDY_ENV_SUFFIXES:
            if key.endswith(suffix):
                prefixes.add(key[: -len(suffix)])
                break
        else:
            prefixes.add(key)
    return sorted(prefixes)


@functools.lru_cache(maxsize=None)
def _studies_from_s3_cached(project: str, base_env: str, profile: Optional[str]) -> dict:
    """Fetch the project's study registry from S3 (see STUDIES_S3_KEY)."""
    import boto3
    from botocore.exceptions import ClientError

    from g3dt import resolver

    rc = resolver.resolve(project, base_env, profile=profile)
    session = boto3.Session(profile_name=profile) if profile else boto3.Session()
    try:
        body = session.client("s3").get_object(
            Bucket=rc.metadata_bucket, Key=STUDIES_S3_KEY
        )["Body"].read()
    except ClientError:
        return {}
    data = yaml.safe_load(body) or {}
    # Accept either a bare map of study keys or a {studies: {...}} wrapper.
    return data.get("studies", data)


def _studies_from_s3(env: str, marker: dict) -> dict:
    """Best-effort S3 fallback for the study registry (empty dict on any miss)."""
    try:
        project = require_project(marker)
        base = env_base(env)
        is_ec2 = env.endswith("_ec2")
        profile = None if is_ec2 else aws_profile_for(base, marker)
        return _studies_from_s3_cached(project, base, profile)
    except Exception:
        return {}


def resolve_study(study: str, env: str, marker: Optional[dict] = None) -> StudyConfig:
    """Resolve a study against an environment.

    The registry comes from the marker's ``studies:`` map if present, else from
    ``s3://<metadata-bucket>/config/studies.yaml`` (so the EC2 job box — whose
    marker carries only the bootstrap — resolves the same registry the laptop
    does; upload it once per env with ``aws s3 cp``).

    Tries ``<study>_<env_base>`` first (so ``ausdiab`` + ``staging`` →
    ``ausdiab_staging``), then the literal ``study`` key for back-compat. This
    derivation is the safety rule that keeps staging data out of prod.
    """
    m = marker if marker is not None else load_marker()
    studies = m.get("studies") or {}
    if not studies:
        studies = _studies_from_s3(env, m) or {}
    candidates = [f"{study}_{env_base(env)}", study]
    for key in candidates:
        sc = studies.get(key)
        if sc:
            return StudyConfig(
                key=key,
                project_id=sc["project_id"],
                program_id=sc["program_id"],
                s3_metadata_path=sc["s3_metadata_path"],
            )
    valid = ", ".join(list_studies(m)) or "(none — add a studies: block to g3dt.yaml)"
    raise ConfigError(
        f"Study '{study}' (env '{env}') not found. Tried {candidates}. "
        f"Valid studies: {valid}"
    )
