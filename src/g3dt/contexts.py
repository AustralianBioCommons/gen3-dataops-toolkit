"""Contexts: one named answer to "what is this command pointed at?".

A context is a 5-tuple ``(name, project, env, profile, region)`` plus an
optional ``production`` flag. It fully determines where a command acts; the
design contract lives in ``docs/design/contexts.md`` (normative sections 2-6).

Two marker generations are supported forever:

* **v2** — an explicit ``contexts:`` mapping plus ``current:``.
* **v1 / file-less** — no ``contexts:`` key. Contexts are then *synthesized*
  in memory from the legacy keys (``profiles:``, ``default_env``,
  ``G3DT_PROJECT``) so that a 3-key EC2 box marker and the marker-less
  CodeBuild path behave byte-identically to toolkit 3.7.x. Synthesis rules are
  design doc section 4 and are pinned by ``tests/test_contexts.py``.

The ``_ec2`` suffix never appears in a context: it remains the wire/auth
mechanism (``config.env_base`` semantics are untouched). ``resolve_context``
accepts ``--env staging_ec2`` and matches it to the ``staging`` context while
returning the *effective* env string with the suffix intact.
"""
from __future__ import annotations

import os
from collections import OrderedDict
from dataclasses import dataclass
from typing import Dict, Optional, Tuple

from g3dt import config as _config

#: Sub-command names of `g3dt config` that a context may not shadow.
RESERVED_CONTEXT_NAMES = frozenset(
    {"use", "contexts", "discover", "add", "forget", "current", "show", "set",
     "envs", "studies", "diff", "dbt-env"}
)


@dataclass(frozen=True)
class Context:
    """A named (project, env, profile, region) tuple. ``env`` is always base."""

    name: str
    project: str
    env: str
    profile: Optional[str]
    region: str
    production: Optional[bool] = None
    source: str = "marker"  # "marker" | "legacy" | "synthetic"


def _validate_context(name: str, spec: dict) -> None:
    if name in RESERVED_CONTEXT_NAMES:
        raise _config.ConfigError(
            f"Context name '{name}' is reserved (it collides with a "
            f"`g3dt config` sub-command). Pick another name."
        )
    env = spec.get("env") or ""
    if env.endswith("_ec2"):
        raise _config.ConfigError(
            f"Context '{name}' has env '{env}': contexts always use the BASE "
            f"environment name — the _ec2 suffix is a dispatch mechanism, not "
            f"an environment."
        )
    for key in ("project", "env"):
        if not spec.get(key):
            raise _config.ConfigError(
                f"Context '{name}' is missing required key '{key}'."
            )


def list_contexts(marker: Optional[dict] = None) -> "OrderedDict[str, Context]":
    """All configured contexts; legacy markers get synthesized ones.

    Synthesis (design doc section 4): one context per ``profiles:`` entry,
    plus an ambient context for a ``default_env`` with no profiles entry
    (the EC2 box's 3-key marker). Returns an empty mapping when nothing can
    be synthesized (no project at all).
    """
    m = marker if marker is not None else _config.load_marker()
    region_default = m.get("region") or _config.DEFAULT_REGION

    explicit = m.get("contexts")
    out: "OrderedDict[str, Context]" = OrderedDict()
    if explicit:
        for name, spec in explicit.items():
            _validate_context(name, spec or {})
            out[name] = Context(
                name=name,
                project=spec["project"],
                env=spec["env"],
                profile=spec.get("profile"),
                region=spec.get("region") or region_default,
                production=spec.get("production"),
                source="marker",
            )
        return out

    project = m.get("project")
    if not project:
        return out
    profiles: Dict[str, str] = m.get("profiles") or {}
    for env_key, profile in profiles.items():
        name = f"{project}/{env_key}"
        out[name] = Context(
            name=name, project=project, env=env_key, profile=profile,
            region=region_default, source="legacy",
        )
    default_env = m.get("default_env")
    if default_env and _config.env_base(default_env) not in profiles:
        base = _config.env_base(default_env)
        name = f"{project}/{base}"
        out.setdefault(
            name,
            Context(name=name, project=project, env=base, profile=None,
                    region=region_default, source="legacy"),
        )
    return out


def current_context_name(marker: Optional[dict] = None) -> Optional[str]:
    """The active context name: v2 ``current:``, else legacy ``default_env``."""
    m = marker if marker is not None else _config.load_marker()
    ctxs = list_contexts(m)
    explicit = m.get("current")
    if explicit and explicit in ctxs:
        return explicit
    default_env = m.get("default_env")
    project = m.get("project")
    if default_env and project:
        candidate = f"{project}/{_config.env_base(default_env)}"
        if candidate in ctxs:
            return candidate
    return None


def is_production(ctx: Context) -> bool:
    """Prod classification: explicit flag OR 'prod' in env or context name."""
    if ctx.production:
        return True
    return "prod" in ctx.env.lower() or "prod" in ctx.name.lower()


def _synthesize_for_env(env: str, marker: dict) -> Optional[Context]:
    """Design doc section 4 rule 4: the ephemeral 3.7.x-compatible context."""
    project = marker.get("project")
    if not project:
        return None
    base = _config.env_base(env)
    return Context(
        name=f"{project}/{base}",
        project=project,
        env=base,
        profile=_config.aws_profile_for(env, marker),
        region=marker.get("region") or _config.DEFAULT_REGION,
        source="synthetic",
    )


def resolve_context(
    ctx_name: Optional[str] = None,
    env: Optional[str] = None,
    marker: Optional[dict] = None,
    required: bool = True,
) -> Tuple[Optional[Context], Optional[str]]:
    """Resolve the acting context and the effective env string.

    Precedence (design doc section 5)::

        --ctx  >  --env (compat alias)  >  $G3DT_CONTEXT  >
        marker `current`  >  legacy default_env  >  error / (None, None)

    The effective env keeps a caller-supplied ``_ec2`` suffix intact (the
    dispatch/auth mechanics downstream depend on it).
    """
    m = marker if marker is not None else _config.load_marker()
    ctxs = list_contexts(m)
    has_explicit_block = bool(m.get("contexts"))

    ctx_name = ctx_name or None
    if ctx_name:
        ctx = ctxs.get(ctx_name)
        if ctx is None:
            raise _config.ConfigError(
                f"Unknown context '{ctx_name}'. Configured: "
                f"{', '.join(ctxs) or '(none)'}. "
                f"Run `g3dt config contexts` or `g3dt config discover --add`."
            )
        if env is not None and _config.env_base(env) != ctx.env:
            raise _config.ConfigError(
                f"--ctx {ctx_name} (env {ctx.env}) conflicts with "
                f"--env {env}. Drop one of the two."
            )
        return ctx, (env if env is not None else ctx.env)

    if env is not None:
        base = _config.env_base(env)
        project = None
        current = current_context_name(m)
        if current and current in ctxs:
            project = ctxs[current].project
        project = project or m.get("project")
        matches = [
            c for c in ctxs.values()
            if c.env == base and (project is None or c.project == project)
        ]
        if len(matches) == 1:
            return matches[0], env
        if len(matches) > 1:
            names = ", ".join(c.name for c in matches)
            raise _config.ConfigError(
                f"--env {base} matches more than one context ({names}). "
                f"Use --ctx <name> instead."
            )
        if has_explicit_block:
            raise _config.ConfigError(
                f"No context for env '{base}'"
                + (f" in project '{project}'" if project else "")
                + f". Configured: {', '.join(ctxs) or '(none)'}. "
                f"Run `g3dt config discover --add` or "
                f"`g3dt config add <name> --project <p> --env {base} "
                f"--profile <profile>`."
            )
        ctx = _synthesize_for_env(env, m)
        if ctx is not None:
            return ctx, env
        if required:
            _config.require_project(m)  # raises with setup guidance
        return None, env

    env_override = os.getenv("G3DT_CONTEXT")
    if env_override:
        return resolve_context(ctx_name=env_override, env=None, marker=m,
                               required=required)

    current = current_context_name(m)
    if current:
        ctx = ctxs[current]
        return ctx, ctx.env

    if required:
        raise _config.ConfigError(
            "No context selected. Pass --env/--ctx, set one with "
            "`g3dt config use <name>`, or register some with "
            "`g3dt config discover --all-profiles --add`."
        )
    return None, None


# --------------------------------------------------------------------------- #
# Process-scoped active context (set by the CLI layer, read by safety/banner)  #
# --------------------------------------------------------------------------- #
_override: Optional[str] = None
_active: Optional[Context] = None
_banner_printed = False


def set_override(name: Optional[str]) -> None:
    """Record the root `--ctx` option for this process."""
    global _override
    _override = name or None


def override() -> Optional[str]:
    return _override


def set_active(ctx: Optional[Context]) -> None:
    global _active
    _active = ctx


def active() -> Optional[Context]:
    return _active


def banner_line(ctx: Optional[Context], effective_env: Optional[str]) -> str:
    """The one-line context banner (design doc section 6). Pure formatting."""
    if ctx is None:
        return ("ctx (none configured) → run 'g3dt config discover "
                "--all-profiles --add' or pass --env/--ctx")
    tags = ""
    if is_production(ctx):
        tags += " [PROD]"
    if effective_env and effective_env.endswith("_ec2"):
        tags += " (remote)"
    profile = ctx.profile or "(ambient)"
    env_shown = effective_env or ctx.env
    if env_shown.endswith("_ec2"):
        profile = "(ambient)"
    return (f"ctx {ctx.name}{tags} → project={ctx.project} env={env_shown} "
            f"profile={profile} region={ctx.region}")


def print_banner(ctx: Optional[Context], effective_env: Optional[str]) -> None:
    """Print the banner to STDERR, at most once per process."""
    global _banner_printed
    if _banner_printed:
        return
    _banner_printed = True
    import typer

    line = banner_line(ctx, effective_env)
    prod = ctx is not None and is_production(ctx)
    typer.secho(line, err=True,
                fg=typer.colors.RED if prod else typer.colors.BRIGHT_BLACK,
                bold=prod)


def reset() -> None:
    """Test hook: clear process-scoped state."""
    global _override, _active, _banner_printed
    _override = None
    _active = None
    _banner_printed = False
