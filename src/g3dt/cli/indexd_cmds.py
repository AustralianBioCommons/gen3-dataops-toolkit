"""`g3dt indexd` — register S3 files with Gen3 indexd (data-plane).

Long-running, so it supports ``--on ec2``.
"""
from __future__ import annotations

from typing import List

import typer

from g3dt.cli._internal import dispatch
from g3dt.cli._internal.dispatch import Target
from g3dt.cli._internal.resolve import study_of

app = typer.Typer(no_args_is_help=True, help="Register files with Gen3 indexd.")

_REGISTER = "services/indexd/register_indexd.py"


@app.command()
def register(
    s3_paths: List[str] = typer.Option(
        ..., "--s3-paths", help="One or more S3 prefixes to scan (repeatable)."
    ),
    study: str = typer.Option(..., "--study", "-s", help="Study, e.g. edcad."),
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Scan + write file_metadata only; skip indexd."
    ),
    on: Target = typer.Option(Target.local, "--on", help="Run local or on ec2."),
) -> None:
    """Scan S3 prefixes and register the files with Gen3 indexd.

    Examples:
      g3dt indexd register --s3-paths s3://bucket/edcad/ --study edcad --env staging
      g3dt indexd register --s3-paths s3://b/a/ --s3-paths s3://b/c/ --study edcad --env staging --on ec2
    """
    s = study_of(study, env)

    def build_args(env_name):
        a = ["--s3-paths", *s3_paths, "--study", s.key, "--env", env_name]
        if dry_run:
            a.append("--dry-run")
        return a

    def remote_cli(env_name):
        a: list = ["indexd", "register"]
        for p in s3_paths:
            a += ["--s3-paths", p]
        a += ["--study", study, "--env", env_name]
        if dry_run:
            a.append("--dry-run")
        return a

    dispatch.run_or_dispatch(
        on, env, _REGISTER, build_args, "indexd-register", remote_cli=remote_cli,
    )
