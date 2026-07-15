"""`g3dt metadata` — upload real study metadata to Gen3 (data-plane).

These are the multi-hour jobs, so they support ``--on ec2`` to run on the
env's EC2 job box via SSM Run Command (disconnect-safe) instead of the laptop.
"""
from __future__ import annotations

import typer

from g3dt.cli._internal import dispatch
from g3dt.cli._internal.dispatch import Target
from g3dt.cli._internal.resolve import study_of

app = typer.Typer(no_args_is_help=True, help="Upload study metadata to Gen3.")

_UPLOAD = "services/upload/metadata/upload_metadata.py"
_UPLOAD_ALL = "services/upload/metadata/upload_all_studies.sh"


@app.command()
def upload(
    study: str = typer.Option(..., "--study", "-s", help="Study, e.g. ausdiab."),
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    node: str = typer.Option(None, "--node", help="Submit only this node type."),
    on: Target = typer.Option(Target.local, "--on", help="Run local or on ec2."),
) -> None:
    """Upload a study's release metadata to Gen3 sheepdog.

    Examples:
      g3dt metadata upload --study ausdiab --env staging
      g3dt metadata upload --study ausdiab --env staging --on ec2
    """
    s = study_of(study, env)

    def build_args(env_name):
        a = ["--study", s.key, "--env", env_name]
        if node:
            a += ["--specific-node", node]
        return a

    def remote_cli(env_name):
        a = ["metadata", "upload", "--study", study, "--env", env_name]
        if node:
            a += ["--node", node]
        return a

    dispatch.run_or_dispatch(
        on, env, _UPLOAD, build_args, "metadata-upload", remote_cli=remote_cli,
    )


@app.command(name="upload-all")
def upload_all(
    studies: str = typer.Option(
        ..., "--studies", help="Comma-separated studies, e.g. ausdiab,caughtcad."
    ),
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    on: Target = typer.Option(Target.local, "--on", help="Run local or on ec2."),
) -> None:
    """Upload several studies sequentially (wraps upload_all_studies.sh).

    The wrapped script aborts on any 'prod' environment.
    """
    names = [s.strip() for s in studies.split(",") if s.strip()]
    keys = [study_of(name, env).key for name in names]

    def build_args(env_name):
        return ["--studies", ",".join(keys), "--env", env_name]

    def remote_cli(env_name):
        return ["metadata", "upload-all", "--studies", studies, "--env", env_name]

    dispatch.run_or_dispatch(
        on, env, _UPLOAD_ALL, build_args, "metadata-upload-all",
        interpreter="bash", remote_cli=remote_cli,
    )
