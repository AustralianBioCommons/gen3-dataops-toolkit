"""`g3dt delete` — destructive metadata removal (data-plane).

A single ``delete metadata`` command handles one or many studies, sequentially,
in a single job. ``--version`` is required: pass a specific version (e.g.
``0.9.8``) to remove just that version (resolved via an Athena GUID lookup), or
``all`` to remove every version.

Every command confirms before acting. Production always requires typing the
target id, even with ``--yes``. Deleting ALL versions always prompts, even with
``--yes``. Confirmation happens locally before any EC2 dispatch (SSM has no
TTY), after which the remote job runs non-interactively.
"""
from __future__ import annotations

import typer

from g3dt.cli._internal import dispatch, safety
from g3dt.cli._internal.dispatch import Target
from g3dt.cli._internal.resolve import study_of

app = typer.Typer(no_args_is_help=True, help="Delete metadata from Gen3 (destructive).")

_DELETE_METADATA = "services/delete/delete_metadata.sh"


@app.command()
def metadata(
    studies: str = typer.Option(
        ..., "--studies", help="Comma-separated studies, e.g. ausdiab,caughtcad."
    ),
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    version: str = typer.Option(
        None,
        "--version",
        help="Metadata version to delete, e.g. 0.9.8, or 'all' for every version.",
    ),
    node: str = typer.Option(None, "--node", help="Delete only this node type."),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the non-prod prompt (specific-version only)."
    ),
    on: Target = typer.Option(Target.local, "--on", help="Run local or on ec2."),
) -> None:
    """Delete study metadata for one or more studies, sequentially, in one job.

    Studies are processed one at a time. A study that exists but has no data at
    the requested version is skipped, and the job continues to the next study.
    """
    if version is None:
        typer.secho(
            "--version is required: specify a version (e.g. 0.9.8) or 'all' "
            "to delete every version.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    names = [s.strip() for s in studies.split(",") if s.strip()]
    keys = [study_of(name, env).key for name in names]
    target = ",".join(keys)
    all_versions = version.strip().lower() == "all"

    if all_versions:
        # Deleting every version is the most destructive path: always prompt
        # (pass assume_yes=False so --yes can't bypass it; prod still types the
        # target).
        safety.confirm_destructive("deletion of ALL VERSIONS", target, env, False)
    else:
        safety.confirm_destructive(f"deletion of v{version}", target, env, yes)

    def build_args(env_name):
        a = [
            "--studies",
            target,
            "--env",
            env_name,
            "--version",
            "all" if all_versions else version,
        ]
        if node:
            a += ["--node", node]
        return a

    def remote_cli(env_name):
        # --yes: confirmation already happened locally; the remote job must
        # not prompt (SSM has no TTY). The remote re-check is version-specific
        # only, and 'all' was already confirmed above.
        a = [
            "delete", "metadata",
            "--studies", studies,
            "--env", env_name,
            "--version", "all" if all_versions else version,
            "--yes",
        ]
        if node:
            a += ["--node", node]
        return a

    dispatch.run_or_dispatch(
        on, env, _DELETE_METADATA, build_args, "delete-metadata",
        interpreter="bash", remote_cli=remote_cli,
    )
