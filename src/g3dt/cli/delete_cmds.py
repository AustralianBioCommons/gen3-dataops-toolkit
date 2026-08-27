"""`g3dt delete` — destructive metadata removal (data-plane).

A single ``delete metadata`` command handles one or many studies, sequentially,
in a single job. Every study needs a version: either write it inline as
``name:version`` in ``--studies``, or pass ``--version`` as the fallback for
the bare names (a specific version like ``0.9.8``, resolved via an Athena GUID
lookup, or ``all`` for every version). A bare study with no version anywhere
is refused (exit 2).

``--synthetic`` switches to registry-free mode for synthetic data: each
``--studies`` name is the Gen3 project code itself (no SSM study registry —
synthetic projects are never registered), bare names default to version
``all``, and a specific version is matched verbatim against the records'
``data_version`` property via GraphQL rather than Athena receipts (synthetic
uploads write none).

The node deletion order comes from, in order: an explicit ``--import-order``
(path or s3:// URI; failures are fatal, never silently skipped), the
registered study's release bucket, a ``DataImportOrder.txt`` in the current
directory, or a topological sort derived from the dictionary itself — the
``--dict-version`` bundle when given, else the env's deployed dictionary.

Every command confirms before acting. Production always requires typing the
target id, even with ``--yes``. Deleting ALL versions always prompts, even with
``--yes``. Confirmation happens locally before any EC2 dispatch (SSM has no
TTY), after which the remote job runs non-interactively.
"""
from __future__ import annotations

import re

from typing import Optional

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


def _synthetic_version(raw: str) -> str:
    """Canonicalise a synthetic version token: ``all`` (any case) or verbatim.

    Synthetic versions are matched exactly against the records' ``data_version``
    property, and the natural label there is the dictionary version WITH its
    leading ``v`` (batch dirs are ``~/.g3dt/synth_metadata/v1.3.0/...``) — so
    unlike ``_normalise_version`` nothing is stripped. A version that matches
    no records is reported by the worker (skip + hint), not silently absorbed.
    """
    token = raw.strip()
    return "all" if token.lower() == "all" else token


def _parse_study_specs(studies: str, fallback, env: str, synthetic: bool = False):
    """Turn ``--studies`` into ``[(resolved_study_key, version), ...]``.

    Each comma-separated entry is ``name`` or ``name:version``. A bare name
    takes *fallback* (the ``--version`` default); *fallback* is ``None`` when
    ``--version`` was not given, which makes a bare name a usage error —
    except with *synthetic*, where a bare name defaults to ``all`` (the whole
    point of the flag is "wipe the synthetic project").

    With *synthetic* the raw name IS the Gen3 project code: no study-registry
    lookup, and version tokens pass through :func:`_synthetic_version`.

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
            version = (
                _synthetic_version(raw_version)
                if synthetic
                else _normalise_version(raw_version, f"for study '{name}'")
            )
        elif fallback is not None:
            version = fallback
        elif synthetic:
            version = "all"
        else:
            typer.secho(
                f"No version for study '{name}': add ':<version>' to it "
                f"(e.g. '{name}:0.9.8'), or pass --version as the default for "
                "every study. Use 'all' to delete every version.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)

        specs.append((name if synthetic else study_of(name, env).key, version))

    if not specs:
        typer.secho("--studies is empty.", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    return specs


@app.command()
def metadata(
    studies: str = typer.Option(
        ...,
        "--studies",
        "-s",
        help="Comma-separated studies, each optionally 'name:version', "
             "e.g. ausdiab:0.7.5,cdah:0.8.1,edcad.",
    ),
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
    version: Optional[str] = typer.Option(
        None,
        "--version",
        "-v",
        help="Default version for studies written without their own "
             "':version', e.g. 0.9.8, or 'all' for every version.",
    ),
    node: Optional[str] = typer.Option(None, "--node", help="Delete only this node type."),
    synthetic: bool = typer.Option(
        False,
        "--synthetic",
        help="Registry-free synthetic-data mode: each --studies name is the "
             "Gen3 project id itself (no SSM study registry). Bare names "
             "default to version 'all'; a specific version matches records' "
             "data_version property verbatim.",
    ),
    program_id: Optional[str] = typer.Option(
        None,
        "--program-id",
        help="Gen3 program for --synthetic (default: program1). "
             "Invalid without --synthetic.",
    ),
    import_order: Optional[str] = typer.Option(
        None,
        "--import-order",
        help="Path or s3:// URI of DataImportOrder.txt. Default: auto — the "
             "study's release bucket (registered studies), then "
             "./DataImportOrder.txt, then derived from the dictionary. "
             "With --on ec2 only s3:// URIs are accepted.",
    ),
    dict_version: Optional[str] = typer.Option(
        None,
        "--dict-version",
        help="Dictionary git tag to derive the node order from (verbatim, "
             "e.g. v1.3.0). Default: the env's deployed dictionary. Only "
             "used when the order is derived.",
    ),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the non-prod prompt (specific-version only)."
    ),
    on: Target = typer.Option(Target.local, "--on", "-o", help="Run local or on ec2."),
) -> None:
    """Delete study metadata for one or more studies, sequentially, in one job.

    Studies are processed one at a time. A study that exists but has no data at
    the requested version is skipped, and the job continues to the next study.

    Each study may carry its own version as ``name:version``; ``--version``
    supplies the default for any study written bare. Examples:

      g3dt delete metadata --studies "ausdiab:0.7.5,cdah:0.8.1" --env staging
      g3dt delete metadata --studies "ausdiab:all,cdah" --version 0.9.8 --env staging
      g3dt delete metadata --studies "synthetic_dataset_1,synthetic_dataset_2" --env test --synthetic
    """
    env = resolve.active_env(env)
    if program_id is not None and not synthetic:
        typer.secho(
            "--program-id is only valid with --synthetic (registered studies "
            "carry their program in the study registry).",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    if import_order and dict_version:
        typer.secho(
            "--import-order names the exact file; --dict-version derives one "
            "— pass only one.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    # A laptop path forwarded to the EC2 box would resolve against the box's
    # filesystem — at best a crash after confirmation, at worst a same-named
    # DIFFERENT file ordering the delete. s3:// URIs (and --dict-version)
    # resolve identically anywhere, so only those may travel.
    if on == Target.ec2 and import_order and not import_order.startswith("s3://"):
        typer.secho(
            "--import-order with --on ec2 must be an s3:// URI (a local path "
            "does not exist on the box). Upload the file, or run locally.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    if version is None:
        fallback = None
    elif synthetic:
        fallback = _synthetic_version(version)
    else:
        fallback = _normalise_version(version, "for --version")
    specs = _parse_study_specs(studies, fallback, env, synthetic=synthetic)
    versions = [v for _, v in specs]

    # The typed production confirmation stays the study keys alone: short
    # enough to retype accurately, while the per-study versions are spelled
    # out in the action line printed directly above the prompt.
    target = ",".join(key for key, _ in specs)
    uniform = len(set(versions)) == 1
    any_all = "all" in versions

    prefix = "synthetic " if synthetic else ""
    if uniform and versions[0] == "all":
        action = f"{prefix}deletion of ALL VERSIONS"
    elif uniform:
        # Synthetic versions are verbatim data_version values (often already
        # v-prefixed); Athena versions are canonical x.y.z, displayed with v.
        shown = versions[0] if synthetic else f"v{versions[0]}"
        action = f"{prefix}deletion of {shown}"
    else:
        plan = ", ".join(f"{key}:{v}" for key, v in specs)
        action = f"{prefix}deletion of per-study versions [{plan}]"

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
        if synthetic:
            # Program is always passed explicitly: the shell default makes it
            # optional on the wire, but an explicit value keeps the contract
            # visible in logs and SSM command history.
            a += ["--synthetic", "--program-id", program_id or "program1"]
        if import_order:
            a += ["--import-order", import_order]
        if dict_version:
            a += ["--dict-version", dict_version]
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
        if synthetic:
            # Without this the remote re-entry would re-parse --studies
            # against the study registry on the box and exit 2.
            a.append("--synthetic")
            if program_id is not None:
                a += ["--program-id", program_id]
        if import_order:
            # Guaranteed s3:// by the pre-dispatch gate above.
            a += ["--import-order", import_order]
        if dict_version:
            a += ["--dict-version", dict_version]
        return a

    dispatch.run_or_dispatch(
        on, env, _DELETE_METADATA, build_args, "delete-metadata",
        interpreter="bash", remote_cli=remote_cli,
    )
