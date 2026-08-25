"""Confirmation and environment guards for destructive / production operations.

These mirror (and strengthen) the guards already baked into the shell scripts:
the test-only ``synth deploy`` guard, the prod aborts in the bulk scripts, and
the optional delete confirmation prompts.

3.8.0: the guards are context-aware. Production is any env/study key whose
name contains ``prod`` (unchanged), OR an active context classified production
(``production: true`` flag, or 'prod' in its name — see
``contexts.is_production``). When a *named* context from the marker is active,
the typed confirmation token is the **context name** (e.g. ``acdc/prod``);
legacy and synthetic contexts keep the historical env-name/target tokens, so
older muscle memory and the pinned safety tests are unaffected. ``--yes``
never bypasses a production prompt, and confirmation always happens locally,
before any EC2 dispatch.
"""
from __future__ import annotations

from typing import Optional

import typer

from g3dt.config import env_base


def is_prod(env: str) -> bool:
    """True if the environment name refers to production."""
    return "prod" in env.lower()


def _active_prod_context():
    """The active context when it is production-classified, else ``None``."""
    from g3dt import contexts

    ctx = contexts.active()
    if ctx is not None and contexts.is_production(ctx):
        return ctx
    return None


def _named(ctx) -> bool:
    """True for a context the operator configured by name (marker source)."""
    return ctx is not None and ctx.source == "marker"


def _typed_gate(header: str, token: str) -> None:
    typer.secho(header, fg=typer.colors.RED, bold=True)
    # default="" so an empty entry (just pressing Enter) returns immediately
    # and aborts, instead of click re-prompting forever.
    typed = typer.prompt(
        f"Type '{token}' to confirm", default="", show_default=False
    )
    if typed.strip() != token:
        typer.secho(
            "Confirmation did not match. Aborting.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)


def require_test_env(env: str) -> None:
    """Abort unless ``env`` is the test environment (``test`` or ``test_ec2``).

    A hard guard for any command that must never run outside test. (The ``synth``
    commands no longer use this — they allow any env and gate prod with
    :func:`confirm_prod_strict` instead.)
    """
    if env_base(env) != "test":
        typer.secho(
            f"Refusing: this command is only allowed for the 'test' "
            f"environment (got '{env}').",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)


def confirm_destructive(action: str, target: str, env: str, assume_yes: bool) -> None:
    """Gate a destructive operation with an appropriate confirmation.

    * Production (env name, study key, or the active context's
      classification): ALWAYS require a typed confirmation, even with
      ``--yes`` (so automation can never silently delete prod data). The
      token is the active context's name when one is configured, else the
      ``target``.
    * Non-production: a simple y/N prompt, skippable with ``--yes``.

    Confirmation always happens locally, before any EC2 dispatch (SSM has no
    TTY), after which the remote job is invoked with ``--yes``.
    """
    ctx = _active_prod_context()
    if is_prod(env) or ctx is not None:
        token = ctx.name if _named(ctx) else target
        _typed_gate(
            f"PRODUCTION {action} targeting '{target}' (env={env}"
            + (f", ctx={ctx.name}" if ctx is not None else "")
            + ").",
            token,
        )
        return

    if assume_yes:
        return
    if not typer.confirm(f"{action} targeting '{target}' (env={env}). Proceed?"):
        typer.secho("Aborted.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)


def confirm_prod_strict(action: str, env: str) -> None:
    """Warn and require a typed confirmation before any action on production.

    Production is any env whose name contains ``prod`` (see :func:`is_prod`)
    or an active production-classified context. Non-production returns
    immediately (no prompt). The confirmation cannot be bypassed, so
    automation can never silently act on prod. The token is the active
    context's name when one is configured, else the env name.

    Used by the ``synth`` commands, which may target any configured environment.
    """
    ctx = _active_prod_context()
    if not is_prod(env) and ctx is None:
        return
    token = ctx.name if _named(ctx) else env
    _typed_gate(
        f"PRODUCTION {action} targeting env '{env}'"
        + (f" (ctx={ctx.name})" if ctx is not None else "")
        + ".",
        token,
    )
