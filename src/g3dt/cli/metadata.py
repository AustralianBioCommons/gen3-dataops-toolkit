"""`g3dt metadata` — upload real study metadata to Gen3 (data-plane).

These are the multi-hour jobs, so they support ``--on ec2`` to run on the
env's EC2 job box via SSM Run Command (disconnect-safe) instead of the laptop.
"""
from __future__ import annotations

import typer

from g3dt.cli._internal import dispatch, resolve, safety
from g3dt.cli._internal.dispatch import Target
from g3dt.cli._internal.resolve import study_of
from g3dt.cli._internal.helptext import ENV_OPT

app = typer.Typer(no_args_is_help=True, help="Upload study metadata to Gen3.")

_UPLOAD = "services/upload/metadata/upload_metadata.py"
_UPLOAD_ALL = "services/upload/metadata/upload_all_studies.sh"


@app.command()
def upload(
    study: str = typer.Option(..., "--study", "-s", help="Study, e.g. ausdiab."),
    env: str = typer.Option(None, "--env", "-e", help=ENV_OPT),
    node: str = typer.Option(None, "--node", help="Submit only this node type."),
    force_reupload: bool = typer.Option(
        False, "--force-reupload",
        help="Proceed even if this project+version was already uploaded to "
        "this commons (uploads are additive: re-running duplicates records).",
    ),
    on: Target = typer.Option(Target.local, "--on", help="Run local or on ec2."),
) -> None:
    """Upload a study's release metadata to Gen3 sheepdog.

    The worker refuses (exit 2) when the audit table already records an
    upload of the same project + version + endpoint — re-running would
    duplicate every record. ``--force-reupload`` overrides.

    Examples:
      g3dt metadata upload --study ausdiab --env staging
      g3dt metadata upload --study ausdiab --env staging --on ec2
    """
    env = resolve.active_env(env)
    s = study_of(study, env)

    def build_args(env_name):
        a = ["--study", s.key, "--env", env_name]
        if node:
            a += ["--specific-node", node]
        if force_reupload:
            a.append("--force-reupload")
        return a

    def remote_cli(env_name):
        a = ["metadata", "upload", "--study", study, "--env", env_name]
        if node:
            a += ["--node", node]
        if force_reupload:
            a.append("--force-reupload")
        return a

    dispatch.run_or_dispatch(
        on, env, _UPLOAD, build_args, "metadata-upload", remote_cli=remote_cli,
    )


@app.command(name="upload-all")
def upload_all(
    studies: str = typer.Option(
        ..., "--studies", help="Comma-separated studies, e.g. ausdiab,caughtcad."
    ),
    env: str = typer.Option(None, "--env", "-e", help=ENV_OPT),
    allow_prod: bool = typer.Option(
        False, "--allow-prod",
        help="Allow bulk upload against production (typed confirmation required).",
    ),
    prod_confirmed: bool = typer.Option(
        False, "--prod-confirmed", hidden=True,
        help="Internal: set by the remote re-entry after the typed "
        "confirmation already happened locally. Never pass by hand.",
    ),
    force_reupload: bool = typer.Option(
        False, "--force-reupload",
        help="Proceed even for project+versions the audit table says were "
        "already uploaded to this commons.",
    ),
    on: Target = typer.Option(Target.local, "--on", help="Run local or on ec2."),
) -> None:
    """Upload several studies sequentially (wraps upload_all_studies.sh).

    Production needs ``--allow-prod`` AND a typed confirmation of the env
    name. The confirmation happens locally, before any EC2 dispatch (SSM has
    no TTY, so a remote prompt would abort); the remote re-entry carries the
    hidden ``--prod-confirmed`` marker instead of re-prompting, and
    ``--allow-prod`` is forwarded so the wrapped script's own guard passes.
    """
    env = resolve.active_env(env)
    names = [s.strip() for s in studies.split(",") if s.strip()]
    keys = [study_of(name, env).key for name in names]

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
        if allow_prod:
            a.append("--allow-prod")
        if force_reupload:
            a.append("--force-reupload")
        return a

    def remote_cli(env_name):
        a = ["metadata", "upload-all", "--studies", studies, "--env", env_name]
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
