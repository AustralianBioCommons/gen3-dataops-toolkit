"""Confirmation and environment guards for destructive / production operations.

These mirror (and strengthen) the guards already baked into the shell scripts:
the test-only ``synth deploy`` guard, the prod aborts in the bulk scripts, and
the optional delete confirmation prompts.
"""
from __future__ import annotations

import typer

from g3dt.config import env_base


def is_prod(env: str) -> bool:
    """True if the environment name refers to production."""
    return "prod" in env.lower()


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


def abort_if_prod(env: str) -> None:
    """Hard abort for bulk operations that must never touch production."""
    if is_prod(env):
        typer.secho(
            f"Refusing bulk operation against a production environment "
            f"('{env}').",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)


def confirm_destructive(action: str, target: str, env: str, assume_yes: bool) -> None:
    """Gate a destructive operation with an appropriate confirmation.

    * Production: ALWAYS require typing the ``target`` exactly, even with
      ``--yes`` (so automation can never silently delete prod data).
    * Non-production: a simple y/N prompt, skippable with ``--yes``.

    Confirmation always happens locally, before any EC2 dispatch (SSM has no
    TTY), after which the remote job is invoked with ``--yes``.
    """
    if is_prod(env):
        typer.secho(
            f"PRODUCTION {action} targeting '{target}' (env={env}).",
            fg=typer.colors.RED,
            bold=True,
        )
        # default="" so an empty entry (just pressing Enter) returns immediately
        # and aborts, instead of click re-prompting forever.
        typed = typer.prompt(
            f"Type '{target}' to confirm", default="", show_default=False
        )
        if typed.strip() != target:
            typer.secho(
                "Confirmation did not match. Aborting.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)
        return

    if assume_yes:
        return
    if not typer.confirm(f"{action} targeting '{target}' (env={env}). Proceed?"):
        typer.secho("Aborted.", fg=typer.colors.YELLOW)
        raise typer.Exit(1)


def confirm_prod_strict(action: str, env: str) -> None:
    """Warn and require typing the env name before any action against production.

    Production is any env whose name contains ``prod`` (see :func:`is_prod`).
    Non-production environments return immediately (no prompt). The confirmation
    cannot be bypassed, so automation can never silently act on prod.

    Used by the ``synth`` commands, which may target any configured environment.
    """
    if not is_prod(env):
        return
    typer.secho(
        f"PRODUCTION {action} targeting env '{env}'.",
        fg=typer.colors.RED,
        bold=True,
    )
    # default="" so an empty entry (just pressing Enter) returns immediately and
    # aborts, instead of click re-prompting forever.
    typed = typer.prompt(f"Type '{env}' to confirm", default="", show_default=False)
    if typed.strip() != env:
        typer.secho(
            "Confirmation did not match. Aborting.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
