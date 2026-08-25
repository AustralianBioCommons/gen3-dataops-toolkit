"""Configuration resolution for the g3dt CLI and services.

The platform has exactly two kinds of configuration:

* **INPUTS** — human-authored values, committed as
  ``config/<projectId>.<env>.json`` in the CDK repo (gen3-aws-data-pipeline)
  and read only by ``cdk deploy``. The toolkit never reads these files.
* **OUTPUTS** — every resource name the CDK creates, plus the mirrored Gen3
  app facts, published to SSM Parameter Store under ``/{project}/{env}/...``
  on deploy. The toolkit resolves everything from there at runtime
  (see :mod:`g3dt.resolver`).

The only local configuration is the **marker** — ``g3dt.yaml`` — which since
3.8.0 stores named **contexts** (see :mod:`g3dt.contexts` and
``docs/design/contexts.md``): each a (project, env, profile, region) tuple
selected with ``g3dt config use``. Legacy markers (top-level
``project``/``default_env``/``profiles:`` keys) keep working — contexts are
synthesized from them in memory. Search order: ``./g3dt.yaml`` →
``~/.g3dt/g3dt.yaml`` → ``/etc/g3dt/g3dt.yaml`` (the EC2 job box's copy,
written by CDK user-data). Environment variables override the file:
``G3DT_PROJECT``, ``AWS_REGION``, ``G3DT_DEFAULT_ENV``, ``G3DT_CONTEXT``;
``G3DT_MARKER`` points at an explicit marker path.

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
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import yaml

DEFAULT_REGION = "ap-southeast-2"

#: Where the data dictionary is fetched from. The URL is composed as
#: ``{base_url}/{repo}/refs/tags/{version}/{dictionary_path}`` -- ``repo`` is the
#: env's ``app/schema_repo``, ``version`` its ``app/dictionary_version``.
#:
#: ``base_url`` and ``dictionary_path`` are OPTIONAL app inputs
#: (``app/dictionary_base_url``, ``app/dictionary_path``); these defaults keep an
#: env deployed before they existed resolving to the URL it always used, so the
#: keys can be added to the CDK config later without a coordinated release.
DEFAULT_DICT_BASE_URL = "https://raw.githubusercontent.com"
DEFAULT_DICT_PATH = "dictionary/prod_dict/acdc_schema.json"

#: Synthetic-data LLM facts, published by the CDK's OPTIONAL ``llm`` config
#: block as ``app/llm_provider`` / ``app/llm_model`` and consumed by
#: gen3-metadata-simulator through ``g3dt synth``. Optional app inputs:
#: environments deployed without the block fall back to this provider default.
#: The model deliberately has no default — ``g3dt synth`` errors with guidance
#: when the ``--llm`` path is used and no model is configured anywhere.
DEFAULT_LLM_PROVIDER = "anthropic"

#: Kubernetes restart targets, published by the CDK's OPTIONAL ``k8s`` config
#: block as ``app/restart_services`` (comma-separated, restarted in order) and
#: ``app/etl_cronjob``. Optional app inputs: environments deployed without the
#: block keep the classic Gen3 set. Consumed by every restart path — `g3dt
#: k8s restart-schema/restart-etl/restart-ms`, `dict deploy`, `synth deploy` —
#: via $G3DT_RESTART_SERVICES / $G3DT_ETL_CRONJOB in the service scripts.
DEFAULT_RESTART_SERVICES = (
    "sheepdog-deployment,peregrine-deployment,guppy-deployment,portal-deployment"
)
DEFAULT_ETL_CRONJOB = "etl-cronjob"

#: Marker locations, most specific first.
MARKER_PATHS = ("g3dt.yaml", "~/.g3dt/g3dt.yaml", "/etc/g3dt/g3dt.yaml")

#: Marker keys `g3dt config set` may write. ``llm_api_key_file`` is the path
#: to the file holding the synth LLM API key — the one LLM setting that stays
#: local (the provider and model come from SSM; the key never leaves the box).
SETTABLE_MARKER_KEYS = ("project", "region", "default_env", "llm_api_key_file")

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
    """Return the project id or fail with setup instructions.

    The active context's project wins; the marker's top-level ``project`` (or
    ``$G3DT_PROJECT``) is the legacy/file-less fallback — in legacy mode the
    two agree by construction.
    """
    m = marker if marker is not None else load_marker()
    from g3dt import contexts as _contexts  # lazy: avoids import cycle

    act = _contexts.active()
    if act is not None:
        return act.project
    if m.get("contexts"):
        current = _contexts.current_context_name(m)
        if current:
            return _contexts.list_contexts(m)[current].project
    project = m.get("project")
    if not project:
        raise ConfigError(
            "No project configured. Register a context with "
            "`g3dt config discover --all-profiles --add` and select it with "
            "`g3dt config use <name>` — or create a g3dt.yaml marker "
            f"(searched: {', '.join(MARKER_PATHS)}) with at least:\n"
            "    project: <projectId>\n"
            "    region: ap-southeast-2\n"
            "or set $G3DT_PROJECT."
        )
    return project


def llm_api_key_file(marker: Optional[dict] = None) -> Optional[str]:
    """Path to the file holding the synth LLM API key, from the marker.

    Set once per operator with ``g3dt config set llm_api_key_file <path>``.
    Returns ``None`` when unset — gen3-metadata-simulator then falls back to
    the vendor's standard env var (``ANTHROPIC_API_KEY`` / ``OPENAI_API_KEY``).
    """
    m = marker if marker is not None else load_marker()
    value = m.get("llm_api_key_file")
    return str(Path(str(value)).expanduser()) if value else None


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
            f"redeploy — the values flow to SSM, not to this file. "
            f"To use a different dictionary tag right now without redeploying, "
            f"pass --version to `g3dt dict pull/upload/deploy`."
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


def _marker_write_path() -> Path:
    """The marker file to write to: the existing one, else ``~/.g3dt/g3dt.yaml``."""
    path = marker_path()
    return path if path is not None else Path("~/.g3dt/g3dt.yaml").expanduser()


def _rewrite_marker(data: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, default_flow_style=False, sort_keys=False)
    _load_yaml_cached.cache_clear()


def upsert_contexts(ctxs, set_current: Optional[str] = None) -> Tuple[Path, list, list]:
    """Write contexts into the marker; never overwrite an existing name.

    Every pre-existing key (v1 ``project``/``region``/``default_env``/
    ``profiles``/``studies``/...) is preserved so an older toolkit reading the
    same file keeps working. Returns ``(path, added_names, skipped_names)``.
    """
    path = _marker_write_path()
    data = dict(_load_yaml_cached(str(path))) if path.is_file() else {}
    existing = data.get("contexts") or {}
    added, skipped = [], []
    for ctx in ctxs:
        if ctx.name in existing:
            skipped.append(ctx.name)
            continue
        spec = {"project": ctx.project, "env": ctx.env}
        if ctx.profile:
            spec["profile"] = ctx.profile
        if ctx.region:
            spec["region"] = ctx.region
        if ctx.production:
            spec["production"] = True
        existing[ctx.name] = spec
        added.append(ctx.name)
    data["contexts"] = existing
    if set_current and set_current in existing:
        data["current"] = set_current
        spec = existing[set_current]
        if data.get("project") in (None, spec["project"]):
            data["default_env"] = spec["env"]
    _rewrite_marker(data, path)
    return path, added, skipped


def set_current_context(name: str) -> Path:
    """Point ``current:`` at a configured context (the `config use` writer).

    Also syncs the legacy ``default_env`` key (older-toolkit goodwill) when the
    marker's top-level project matches the context's project.
    """
    from g3dt import contexts as _contexts  # lazy: avoids import cycle

    marker = load_marker()
    ctxs = _contexts.list_contexts(marker)
    if name not in ctxs:
        raise ConfigError(
            f"Unknown context '{name}'. Configured: {', '.join(ctxs) or '(none)'}. "
            f"Run `g3dt config contexts` or `g3dt config discover --add`."
        )
    ctx = ctxs[name]
    path = _marker_write_path()
    data = dict(_load_yaml_cached(str(path))) if path.is_file() else {}
    if not data.get("contexts"):
        # v1 marker: materialize the synthesized contexts first (auto-migrate),
        # preserving every legacy key in the file.
        data["contexts"] = {
            c.name: {k: v for k, v in (
                ("project", c.project), ("env", c.env),
                ("profile", c.profile), ("region", c.region),
                ("production", c.production),
            ) if v}
            for c in ctxs.values()
        }
    data["current"] = name
    if data.get("project") in (None, ctx.project):
        data["default_env"] = ctx.env
    _rewrite_marker(data, path)
    return path


def forget_context(name: str) -> Path:
    """Remove one context from the marker (local-only; nothing in AWS changes)."""
    path = _marker_write_path()
    data = dict(_load_yaml_cached(str(path))) if path.is_file() else {}
    existing = data.get("contexts") or {}
    if name not in existing:
        raise ConfigError(
            f"Unknown context '{name}'. Configured: "
            f"{', '.join(existing) or '(none — legacy markers have no stored contexts)'}."
        )
    del existing[name]
    data["contexts"] = existing
    if data.get("current") == name:
        data.pop("current", None)
    _rewrite_marker(data, path)
    return path


# --------------------------------------------------------------------------- #
# Environments                                                                 #
# --------------------------------------------------------------------------- #
def env_base(env: str) -> str:
    """Strip a trailing ``_ec2`` suffix: ``staging_ec2`` -> ``staging``."""
    return env[:-4] if env.endswith("_ec2") else env


def aws_profile_for(env: str, marker: Optional[dict] = None) -> Optional[str]:
    """Return the local AWS named profile for ``env``, or ``None`` (ambient).

    With a v2 marker (an explicit ``contexts:`` block) the profile comes from
    the context matching ``env`` within the active project — this is what lets
    one laptop hold profiles for the same env name across different projects.
    Legacy markers read the flat ``profiles:`` map exactly as before, e.g.::

        profiles:
          test: etl_test
          staging: etl_staging

    On the EC2 box / CodeBuild there is no ``profiles`` map (and usually no
    marker), so the default credential chain (the instance/build role) is
    used — by design.
    """
    m = marker if marker is not None else load_marker()
    base = env_base(env)
    if m.get("contexts"):
        from g3dt import contexts as _contexts  # lazy: avoids import cycle

        ctxs = _contexts.list_contexts(m)
        current = _contexts.current_context_name(m)
        project = ctxs[current].project if current in ctxs else m.get("project")
        matches = [
            c for c in ctxs.values()
            if c.env == base and (project is None or c.project == project)
        ]
        if len(matches) == 1:
            return matches[0].profile
        # zero or ambiguous: fall through to the legacy map (usually empty)
    profiles = m.get("profiles") or {}
    return profiles.get(base)


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
    # Optional dictionary-source inputs. Real defaults (not None) so a
    # hand-built EnvConfig still composes a valid URL.
    dictionary_base_url: str = DEFAULT_DICT_BASE_URL
    dictionary_path: str = DEFAULT_DICT_PATH
    # Optional synthetic-data LLM inputs (SSM app/llm_provider, app/llm_model).
    # The model has no default on purpose: synth's --llm path checks and errors
    # with guidance rather than silently picking a model.
    llm_provider: str = DEFAULT_LLM_PROVIDER
    llm_model: Optional[str] = None
    # Optional k8s restart targets (SSM app/restart_services, app/etl_cronjob).
    restart_services: str = DEFAULT_RESTART_SERVICES
    etl_cronjob: str = DEFAULT_ETL_CRONJOB


def _app_or_default(rc, leaf: str, default: str) -> str:
    """Read an OPTIONAL ``app/<leaf>`` parameter, treating blank as unset."""
    return (rc.get(f"app/{leaf}") or "").strip() or default


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
        # Optional: absent, and blank-or-whitespace, both fall back to the
        # default. SSM rejects a truly empty value, so a "blanked out" parameter
        # arrives as whitespace -- which is truthy, and would otherwise compose a
        # URL with no host at all. Deliberately NOT in REQUIRED_APP_KEYS: that
        # gate raises a "re-run cdk deploy" error, which would break every
        # environment deployed before these keys existed.
        dictionary_base_url=_app_or_default(
            rc, "dictionary_base_url", DEFAULT_DICT_BASE_URL
        ),
        dictionary_path=_app_or_default(rc, "dictionary_path", DEFAULT_DICT_PATH),
        # Same optional-app-fact contract: the CDK publishes these only when
        # the config has an llm block, so absence means "use the defaults".
        llm_provider=_app_or_default(rc, "llm_provider", DEFAULT_LLM_PROVIDER),
        llm_model=(_app_or_default(rc, "llm_model", "") or None),
        restart_services=_app_or_default(
            rc, "restart_services", DEFAULT_RESTART_SERVICES
        ),
        etl_cronjob=_app_or_default(rc, "etl_cronjob", DEFAULT_ETL_CRONJOB),
    )


def dictionary_url(e: EnvConfig, version: Optional[str] = None) -> str:
    """Compose the download URL for an environment's data dictionary.

    ``{base_url}/{repo}/refs/tags/{version}/{dictionary_path}``. ``version``
    overrides the env's ``dictionary_version`` (this backs ``--version``).

    Segments are stripped of leading/trailing slashes so a stray slash in an SSM
    value cannot produce a ``//`` mid-path (which raw GitHub 404s on); only the
    *ends* are touched, so the ``//`` in ``https://`` survives.

    The ``refs/tags`` segment is fixed, so this cannot express a branch or a
    release asset. Both callers go through this function, so switching to a
    single templated input later is a contained change.
    """
    parts = (
        e.dictionary_base_url,
        e.schema_repo,
        "refs/tags",
        version or e.dictionary_version,
        e.dictionary_path,
    )
    return "/".join(p.strip().strip("/") for p in parts if p)


def dictionary_filename(e: EnvConfig, version: Optional[str] = None) -> str:
    """Local basename for a pulled dictionary, e.g. ``acdc_schema_v1.1.6.json``.

    Derived from ``dictionary_path``'s basename with the version inserted before
    the extension -- the same rule ``pull_dict.sh`` applies. Callers pass this to
    that script as its explicit second argument, so the downloaded name is
    decided here rather than regexed back out of the URL.
    """
    v = version or e.dictionary_version
    # `rsplit` also drops any directory component, so a dictionary_path
    # containing `..` cannot steer the download out of the schema directory.
    base = e.dictionary_path.strip().strip("/").rsplit("/", 1)[-1]
    stem, dot, ext = base.rpartition(".")
    return f"{stem}_{v}.{ext}" if dot else f"{base}_{v}"


#: A version tag as this project writes them: 'v' then a digit, e.g. v1, v1.1.6,
#: v2.0.0-rc1. Deliberately strict -- a trailing word after an underscore is only
#: a version claim if it looks like one, or every ``my_draft.json`` would be read
#: as version "draft".
_VERSION_TAG_RE = re.compile(r"v\d[\w.+-]*\Z")


def dictionary_version_of(filename: str) -> Optional[str]:
    """Recover the version stamped into a dictionary filename, if any.

    ``acdc_schema_v1.1.6.json`` -> ``v1.1.6``; a name carrying no version-shaped
    suffix (``my_draft.json``) -> ``None``. Used to catch a ``--schema`` that
    contradicts ``--version`` before it mislabels a synthetic-data batch; a name
    that makes no version claim has nothing to contradict.
    """
    stem = filename.rsplit("/", 1)[-1].rpartition(".")[0] or filename
    _, sep, tail = stem.rpartition("_")
    if sep and _VERSION_TAG_RE.match(tail):
        return tail
    return None


def list_envs(project: Optional[str] = None) -> List[str]:
    """Return the environments that have a deployed SSM tree for the project.

    With a v2 marker the listing is the union across every context of the
    project — each context authenticates with its own profile, so a project
    spanning several AWS accounts lists all its envs (per-context auth
    failures are tolerated, not fatal). Legacy markers keep the historical
    single-call behavior: any configured profile (they all target the same
    account) or the ambient chain.
    """
    from g3dt import resolver

    marker = load_marker()
    project = project or require_project(marker)
    if marker.get("contexts"):
        from g3dt import contexts as _contexts

        found: List[str] = []
        for ctx in _contexts.list_contexts(marker).values():
            if ctx.project != project:
                continue
            try:
                for env in resolver.list_envs(
                    project, profile=ctx.profile, region=ctx.region
                ):
                    if env not in found:
                        found.append(env)
            except Exception:  # auth/SSO/network per-context: skip, not fatal
                continue
        return found
    # Legacy: listing spans envs, so authenticate with any configured profile
    # (they all target the same account) or the ambient chain.
    profiles = marker.get("profiles") or {}
    profile = next(iter(profiles.values()), None)
    return resolver.list_envs(project, profile=profile)


def script_env(e: EnvConfig, version: Optional[str] = None) -> Dict[str, str]:
    """Environment variables for a wrapped service script.

    The pre-2.0 shell scripts parsed the legacy YAML config themselves with
    ``yq``; now the Python caller resolves everything from SSM and hands it
    over as ``G3DT_*`` variables.

    ``version`` overrides the env's ``dictionary_version`` for this invocation
    (``--version`` on the dict commands). Passing it here rather than letting the
    scripts compose a URL keeps Python the single source of truth: the scripts
    read ``G3DT_DICT_URL``/``G3DT_DICT_FILENAME`` and never build either
    themselves, so there is only one implementation to keep correct.

    The interpreter's own bin directory is prepended to PATH: console scripts
    installed next to g3dt — gen3-metadata-simulator via
    ``g3dt synth install-simulator`` — land there, but a pipx install exposes
    only g3dt's own entry points on the caller's PATH, so without this the
    wrapped scripts' ``command -v gen3-metadata-simulator`` check fails even
    though the tool is installed.
    """
    import sys

    env = dict(os.environ)
    # No .resolve(): a venv's python is a symlink to the base interpreter, and
    # resolving it would point at the base install's bin instead of the venv
    # bin where console scripts are actually created.
    venv_bin = str(Path(sys.executable).parent)
    path_entries = env.get("PATH", "").split(os.pathsep)
    if venv_bin not in path_entries:
        env["PATH"] = venv_bin + os.pathsep + env.get("PATH", "")
    values = {
        "G3DT_ENV": e.name,
        "G3DT_REGION": e.region,
        "G3DT_AWS_PROFILE": e.aws_profile or "",
        "G3DT_DICTIONARY_VERSION": version or e.dictionary_version,
        "G3DT_DICT_URL": dictionary_url(e, version),
        "G3DT_DICT_FILENAME": dictionary_filename(e, version),
        "G3DT_AWS_SECRET_NAME": e.aws_secret_name,
        "G3DT_SCHEMA_S3_URI": e.schema_s3_uri,
        "G3DT_DOMAIN": e.domain,
        "G3DT_APP_NAME": e.app_name,
        "G3DT_NAMESPACE": e.namespace,
        "G3DT_CLUSTER_NAME": e.cluster_name,
        "G3DT_SCHEMA_REPO": e.schema_repo,
        "G3DT_LLM_PROVIDER": e.llm_provider,
        "G3DT_LLM_MODEL": e.llm_model,
        "G3DT_RESTART_SERVICES": e.restart_services,
        "G3DT_ETL_CRONJOB": e.etl_cronjob,
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
    # Explicit region: botocore ignores AWS_REGION (only AWS_DEFAULT_REGION),
    # so an ambient session on the EC2 box would have no region at all.
    session = boto3.Session(profile_name=profile, region_name=rc.region)
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
