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

import re

import typer

from g3dt.cli._internal import dispatch, safety
from g3dt.cli._internal.dispatch import Target
from g3dt.cli._internal.resolve import study_of

app = typer.Typer(no_args_is_help=True, help="Delete metadata from Gen3 (destructive).")

_DELETE_METADATA = "services/delete/delete_metadata.sh"

#: A version token in the form the Athena ``version`` column stores it. The
#: uploader writes ``group(1)`` of this same pattern (metadata_submitter's
#: ``_find_version_from_path``), i.e. WITHOUT any leading ``v``. The delete
#: query interpolates the string straight into SQL, so a ``v``-prefixed version
#: matches zero rows and is reported as "skipped" rather than as an error — a
#: silent no-op that reads as a clean run. Normalising here closes that.
_VERSION_RE = re.compile(r"^v?(\d+\.\d+\.\d+)$", re.IGNORECASE)


def _normalise_version(raw: str, where: str) -> str:
    """Canonicalise one version token to the form stored in Athena.

    ``all`` in any case becomes ``all``; ``v1.5.4`` and ``1.5.4`` both become
    ``1.5.4``. Anything else is a usage error: the column only ever holds
    three-part semver, so a truncated version like ``0.9`` would match nothing
    and be counted as a skip.
    """
    token = raw.strip()
    if token.lower() == "all":
        return "all"
    match = _VERSION_RE.match(token)
    if not match:
        typer.secho(
            f"Invalid version '{raw}' {where}: expected x.y.z (e.g. 0.9.8) "
            "or 'all'.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    return match.group(1)


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

    version = _normalise_version(version, "for --version")

    names = [s.strip() for s in studies.split(",") if s.strip()]
    keys = [study_of(name, env).key for name in names]
    target = ",".join(keys)
    all_versions = version == "all"

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
