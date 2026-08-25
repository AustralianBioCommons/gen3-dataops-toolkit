"""Thin wrappers that turn config resolution errors into clean CLI exits.

3.8.0 additions: :func:`active_env` / :func:`announce_context` implement the
universal context banner (design doc docs/design/contexts.md section 6), and
:func:`rc_of` is the single resolved-config idiom that replaces the hand-rolled
``load_marker``/``require_project``/``env_base``/``aws_profile_for`` block that
used to be copy-pasted across command modules (one copy of which forgot the
``_ec2`` → ambient rule — the class of bug this unification removes).
"""
from __future__ import annotations

from typing import Optional

import typer

from g3dt import config, contexts


def env_of(env: str) -> config.EnvConfig:
    try:
        return config.resolve_env(env)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


def study_of(study: str, env: str) -> config.StudyConfig:
    try:
        return config.resolve_study(study, env)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


def active_env(env: Optional[str]) -> str:
    """Resolve the acting context, print the banner, return the effective env.

    Every command that takes ``--env`` calls this first. The returned string
    keeps a caller-supplied ``_ec2`` suffix intact (dispatch/auth mechanics
    downstream depend on it, and the remote wire form never changes).
    """
    try:
        ctx, effective = contexts.resolve_context(
            ctx_name=contexts.override(), env=env
        )
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    contexts.set_active(ctx)
    contexts.print_banner(ctx, effective)
    return effective


def announce_context() -> None:
    """Banner-only entry point for commands that take no ``--env``.

    Never fails: with nothing configured it prints the "(none configured)"
    banner and returns — `jobs list`, `version` etc. must keep working on a
    bare machine.
    """
    try:
        ctx, effective = contexts.resolve_context(required=False)
    except config.ConfigError:
        ctx, effective = None, None
    contexts.set_active(ctx)
    contexts.print_banner(ctx, effective)


def rc_of(env: str):
    """Resolved SSM config for ``env`` with the correct credential rule.

    ``_ec2``-suffixed envs always use the ambient chain (never a laptop
    profile) — the rule three call sites applied and two forgot.
    """
    from g3dt import resolver

    marker = config.load_marker()
    project = config.require_project(marker)
    base = config.env_base(env)
    profile = None if env.endswith("_ec2") else config.aws_profile_for(env, marker)
    try:
        return resolver.resolve(project, base, profile=profile)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
