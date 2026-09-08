"""`g3dt metadata` — upload real study metadata to Gen3 (data-plane).

These are the multi-hour jobs, so they support ``--on ec2`` to run on the
env's EC2 job box via SSM Run Command (disconnect-safe) instead of the laptop.
"""
from __future__ import annotations

from typing import Optional

import typer

from g3dt import config, studies
from g3dt.cli._internal import dispatch, resolve, safety
from g3dt.cli._internal.dispatch import Target
from g3dt.cli._internal.resolve import study_of
from g3dt.cli._internal.helptext import ENV_OPT

app = typer.Typer(no_args_is_help=True, help="Upload study metadata to Gen3.")

_UPLOAD = "services/upload/metadata/upload_metadata.py"
_UPLOAD_ALL = "services/upload/metadata/upload_all_studies.sh"

RELEASE_HELP = (
    "Upload the release JSONs at this version instead of the registry path: "
    "swaps the vX.Y.Z segment of the study's s3_metadata_path and checks "
    "DataImportOrder.txt plus node JSONs exist before submitting. Accepts "
    "v2.1.0 or 2.1.0. Does not change the registry — use `g3dt study "
    "repoint` for that."
)
_PROD_CONFIRMED_HELP = (
    "Internal: set by the remote re-entry after the typed confirmation "
    "already happened locally. Never pass by hand."
)


def _normalise_release(release: str) -> str:
    """``--release`` value -> bare ``x.y.z`` tag, or a usage error (exit 2)."""
    try:
        return studies.normalise_release_tag(release)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(2)


def _release_preflight(s, tag: str, env: str) -> str:
    """Resolve and verify the S3 prefix for ``tag`` before anything runs.

    Swaps the release segment of the study's registry path and runs the
    same checks the worker will (DataImportOrder.txt + node JSONs). Done on
    the laptop so a wrong tag fails here even for ``--on ec2``, instead of
    minutes later on the box. Returns the resolved prefix.
    """
    try:
        path = studies.replace_version_segment(s.s3_metadata_path, tag)
    except config.ConfigError as exc:
        typer.secho(f"{s.key}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(2)
    _, session = resolve.rc_session_of(env)
    try:
        count = studies.validate_upload_prefix(path, session)
    except config.ConfigError as exc:
        typer.secho(
            f"Release {tag} is not uploadable for {s.key} — nothing was "
            f"submitted:\n  - {exc}",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)
    typer.secho(
        f"{s.key}: release {tag} -> {path} ({count} node JSONs)",
        fg=typer.colors.BRIGHT_BLACK, err=True,
    )
    return path


@app.command()
def upload(
    study: str = typer.Option(..., "--study", "-s", help="Study, e.g. ausdiab."),
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
    release: Optional[str] = typer.Option(None, "--release", help=RELEASE_HELP),
    node: Optional[str] = typer.Option(None, "--node", help="Submit only this node type."),
    force_reupload: bool = typer.Option(
        False, "--force-reupload",
        help="Proceed even if this project+version was already uploaded to "
        "this commons (uploads are additive: re-running duplicates records).",
    ),
    prod_confirmed: bool = typer.Option(
        False, "--prod-confirmed", hidden=True, help=_PROD_CONFIRMED_HELP
    ),
    on: Target = typer.Option(Target.local, "--on", "-o", help="Run local or on ec2."),
) -> None:
    """Upload a study's release metadata to Gen3 sheepdog.

    The files come from the study's registry path (`g3dt study show <name>`);
    its vX.Y.Z segment is the version that is recorded. Pass --release to
    upload a different version of the same study without moving the
    registry, or move it first with `g3dt study repoint`.

    The worker refuses (exit 2) when the audit table already records an
    upload of the same project + version + endpoint — re-running would
    duplicate every record. ``--force-reupload`` overrides.

    Examples:
      g3dt metadata upload --study ausdiab --env staging
      g3dt metadata upload --study ausdiab --env staging --on ec2
      g3dt metadata upload --study ausdiab --env staging --release v2.1.0

    Targeting production requires typing the context/env name to confirm.
    The confirmation happens locally, before any EC2 dispatch (the box has no
    TTY).
    """
    env = resolve.active_env(env)
    s = study_of(study, env)
    tag = _normalise_release(release) if release else None
    if tag:
        _release_preflight(s, tag, env)
    # Prod is detected on the resolved study key as well as on --env:
    # `--env staging --study ausdiab_prod` is a production write.
    gated = safety.is_prod(env) or safety.is_prod(s.key)
    if not prod_confirmed:
        safety.confirm_prod_strict("metadata upload", env)

    def build_args(env_name):
        a = ["--study", s.key, "--env", env_name]
        if tag:
            a += ["--release", tag]
        if node:
            a += ["--specific-node", node]
        if force_reupload:
            a.append("--force-reupload")
        return a

    def remote_cli(env_name):
        a = ["metadata", "upload", "--study", study, "--env", env_name]
        if tag:
            a += ["--release", tag]
        if node:
            a += ["--node", node]
        if force_reupload:
            a.append("--force-reupload")
        if gated:
            # The typed confirmation already happened locally above; the box
            # has no TTY, so the re-entry must not prompt again.
            a.append("--prod-confirmed")
        return a

    dispatch.run_or_dispatch(
        on, env, _UPLOAD, build_args, "metadata-upload", remote_cli=remote_cli,
    )


@app.command(name="upload-all")
def upload_all(
    studies: str = typer.Option(
        ..., "--studies", "-s",
        help="Comma-separated studies, e.g. ausdiab,caughtcad."
    ),
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
    release: Optional[str] = typer.Option(None, "--release", help=RELEASE_HELP),
    allow_prod: bool = typer.Option(
        False, "--allow-prod",
        help="Allow bulk upload against production (typed confirmation required).",
    ),
    prod_confirmed: bool = typer.Option(
        False, "--prod-confirmed", hidden=True, help=_PROD_CONFIRMED_HELP
    ),
    force_reupload: bool = typer.Option(
        False, "--force-reupload",
        help="Proceed even for project+versions the audit table says were "
        "already uploaded to this commons.",
    ),
    on: Target = typer.Option(Target.local, "--on", "-o", help="Run local or on ec2."),
) -> None:
    """Upload several studies sequentially (wraps upload_all_studies.sh).

    Each study uploads from its registry path, or from the same release of
    every study with --release (every prefix is checked in S3 before the
    first upload starts). Failures are reported at the end, per study, and
    logged under ~/.g3dt/logs/ on the machine that ran it.

    Production needs ``--allow-prod`` AND a typed confirmation of the env
    name. The confirmation happens locally, before any EC2 dispatch (the box
    has no TTY, so a remote prompt would abort).

    Examples:
      g3dt metadata upload-all --studies ausdiab,caughtcad --env staging --on ec2
      g3dt metadata upload-all --studies ausdiab,caughtcad --env staging --release v2.1.0
    """
    env = resolve.active_env(env)
    names = [s.strip() for s in studies.split(",") if s.strip()]
    records = [study_of(name, env) for name in names]
    keys = [r.key for r in records]
    tag = _normalise_release(release) if release else None
    if tag:
        for r in records:
            _release_preflight(r, tag, env)

    # Prod is detected on the resolved study keys as well as on --env:
    # `--env staging --studies ausdiab_prod` is a production write.
    if safety.is_prod(env) or any(safety.is_prod(k) for k in keys):
        if not allow_prod:
            typer.secho(
                "Refusing bulk upload against a production environment. "
                "Re-run with --allow-prod to confirm interactively.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(2)
        if not prod_confirmed:
            safety.confirm_prod_strict("bulk metadata upload", env)

    def build_args(env_name):
        a = ["--studies", ",".join(keys), "--env", env_name]
        if tag:
            a += ["--release", tag]
        if allow_prod:
            a.append("--allow-prod")
        if force_reupload:
            a.append("--force-reupload")
        return a

    def remote_cli(env_name):
        a = ["metadata", "upload-all", "--studies", studies, "--env", env_name]
        if tag:
            a += ["--release", tag]
        if allow_prod:
            # The typed confirmation already happened locally above; the box
            # has no TTY, so the re-entry must not prompt again.
            a += ["--allow-prod", "--prod-confirmed"]
        if force_reupload:
            a.append("--force-reupload")
        return a

    dispatch.run_or_dispatch(
        on, env, _UPLOAD_ALL, build_args, "metadata-upload-all",
        interpreter="bash", remote_cli=remote_cli,
    )
