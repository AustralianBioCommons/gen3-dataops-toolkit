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

from g3dt.cli._internal import dispatch, resolve, safety
from g3dt.cli._internal.dispatch import Target
from g3dt.cli._internal.resolve import study_of
from g3dt.cli._internal.helptext import ENV_OPT

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


def _parse_study_specs(studies: str, fallback, env: str):
    """Turn ``--studies`` into ``[(resolved_study_key, version), ...]``.

    Each comma-separated entry is ``name`` or ``name:version``. A bare name
    takes *fallback* (the ``--version`` default); *fallback* is ``None`` when
    ``--version`` was not given, which makes a bare name a usage error.

    Every entry is validated before anything is dispatched, so a typo in the
    last study cannot leave the earlier ones already deleted.
    """
    specs = []
    for entry in studies.split(","):
        entry = entry.strip()
        if not entry:
            continue

        # partition() rather than split(), so a trailing colon ("ausdiab:") is
        # distinguishable from a bare name and can be rejected instead of
        # silently taking the fallback.
        name, sep, raw_version = entry.partition(":")
        name = name.strip()

        if not name:
            typer.secho(
                f"Invalid --studies entry '{entry}': missing study name.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        if sep and not raw_version.strip():
            typer.secho(
                f"Invalid --studies entry '{entry}': ':' with no version. "
                f"Use '{name}:0.9.8', '{name}:all', or a bare '{name}' to take "
                "the --version default.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        if sep:
            version = _normalise_version(raw_version, f"for study '{name}'")
        elif fallback is not None:
            version = fallback
        else:
            typer.secho(
                f"No version for study '{name}': add ':<version>' to it "
                f"(e.g. '{name}:0.9.8'), or pass --version as the default for "
                "every study. Use 'all' to delete every version.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        specs.append((study_of(name, env).key, version))

    if not specs:
        typer.secho("--studies is empty.", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    return specs


@app.command()
def metadata(
    studies: str = typer.Option(
        ...,
        "--studies",
        help="Comma-separated studies, each optionally 'name:version', "
             "e.g. ausdiab:0.7.5,cdah:0.8.1,edcad.",
    ),
    env: str = typer.Option(None, "--env", "-e", help=ENV_OPT),
    version: str = typer.Option(
        None,
        "--version",
        help="Default version for studies written without their own "
             "':version', e.g. 0.9.8, or 'all' for every version.",
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

    Each study may carry its own version as ``name:version``; ``--version``
    supplies the default for any study written bare. Examples:

      g3dt delete metadata --studies "ausdiab:0.7.5,cdah:0.8.1" --env staging
      g3dt delete metadata --studies "ausdiab:all,cdah" --version 0.9.8 --env staging
    """
    env = resolve.active_env(env)
    fallback = (
        _normalise_version(version, "for --version") if version is not None else None
    )
    specs = _parse_study_specs(studies, fallback, env)
    versions = [v for _, v in specs]

    # The typed production confirmation stays the study keys alone: short
    # enough to retype accurately, while the per-study versions are spelled
    # out in the action line printed directly above the prompt.
    target = ",".join(key for key, _ in specs)
    uniform = len(set(versions)) == 1
    any_all = "all" in versions

    if uniform and versions[0] == "all":
        action = "deletion of ALL VERSIONS"
    elif uniform:
        action = f"deletion of v{versions[0]}"
    else:
        plan = ", ".join(f"{key}:{v}" for key, v in specs)
        action = f"deletion of per-study versions [{plan}]"

    # Deleting every version is the most destructive path: always prompt (pass
    # assume_yes=False so --yes can't bypass it; prod still types the target).
    # One 'all' anywhere in the list is enough to force the prompt, so an 'all'
    # buried mid-list cannot ride along on a batch marked unattended.
    safety.confirm_destructive(action, target, env, False if any_all else yes)

    def build_args(env_name):
        if uniform:
            # Canonical (and historical) shape: one --version for every study.
            # Emitting it keeps a newer CLI compatible with an older installed
            # service script on the box, which can lag a pip upgrade.
            a = ["--studies", target, "--env", env_name, "--version", versions[0]]
        else:
            a = [
                "--studies",
                ",".join(f"{key}:{v}" for key, v in specs),
                "--env",
                env_name,
            ]
        if node:
            a += ["--node", node]
        return a

    def remote_cli(env_name):
        # --yes: confirmation already happened locally; the remote job must
        # not prompt (SSM has no TTY). The raw --studies string is forwarded
        # verbatim — the remote re-entry re-parses and re-validates it.
        a = ["delete", "metadata", "--studies", studies, "--env", env_name]
        if version is not None:
            a += ["--version", version]
        a.append("--yes")
        if node:
            a += ["--node", node]
        return a

    dispatch.run_or_dispatch(
        on, env, _DELETE_METADATA, build_args, "delete-metadata",
        interpreter="bash", remote_cli=remote_cli,
    )
