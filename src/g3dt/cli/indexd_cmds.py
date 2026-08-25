"""`g3dt indexd` — register S3 files with Gen3 indexd, and verify they download.

``register`` is a long data-plane op, so it supports ``--on ec2``.
``check-download`` is a read-only HTTP check that takes seconds and whose
whole value is the PASS/FAIL in your terminal, so it is local-only.
"""
from __future__ import annotations

from typing import List, Optional

import typer

from g3dt.cli._internal import dispatch, resolve, runner
from g3dt.cli._internal.dispatch import Target
from g3dt.cli._internal.resolve import env_of, study_of

app = typer.Typer(
    no_args_is_help=True,
    help="Register files with Gen3 indexd and verify download access.",
)

_REGISTER = "services/indexd/register_indexd.py"
_CHECK_DOWNLOAD = "services/indexd/verify_file_access.py"


@app.command()
def register(
    s3_paths: List[str] = typer.Option(
        ..., "--s3-paths", help="One or more S3 prefixes to scan (repeatable)."
    ),
    study: str = typer.Option(..., "--study", "-s", help="Study, e.g. edcad."),
    env: str = typer.Option(None, "--env", "-e", help="Environment; selects the matching context (or use --ctx / `g3dt config use`)."),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Scan + write file_metadata only; skip indexd."
    ),
    force: bool = typer.Option(
        False, "--force",
        help="Re-register files already in the registry with the same md5.",
    ),
    on: Target = typer.Option(Target.local, "--on", help="Run local or on ec2."),
) -> None:
    """Scan S3 prefixes and register the files with Gen3 indexd.

    Files already registered for this study at this endpoint with an unchanged
    md5 are skipped (each re-registration would create a new indexd revision
    and duplicate the registry). ``--force`` re-registers everything.

    Examples:
      g3dt indexd register --s3-paths s3://bucket/edcad/ --study edcad --env staging
      g3dt indexd register --s3-paths s3://b/a/ --s3-paths s3://b/c/ --study edcad --env staging --on ec2
    """
    env = resolve.active_env(env)
    s = study_of(study, env)

    def build_args(env_name):
        a = ["--s3-paths", *s3_paths, "--study", s.key, "--env", env_name]
        if dry_run:
            a.append("--dry-run")
        if force:
            a.append("--force")
        return a

    def remote_cli(env_name):
        a: list = ["indexd", "register"]
        for p in s3_paths:
            a += ["--s3-paths", p]
        a += ["--study", study, "--env", env_name]
        if dry_run:
            a.append("--dry-run")
        if force:
            a.append("--force")
        return a

    dispatch.run_or_dispatch(
        on, env, _REGISTER, build_args, "indexd-register", remote_cli=remote_cli,
    )


@app.command(name="check-download")
def check_download(
    guids: Optional[List[str]] = typer.Argument(
        None,
        help="Object GUIDs, e.g. PREFIX/<uuid>. Omit to sample the most "
             "recently registered objects from the indexd registry.",
    ),
    env: str = typer.Option(None, "--env", "-e", help="Environment; selects the matching context (or use --ctx / `g3dt config use`)."),
    limit: int = typer.Option(
        25, "--limit", "-n",
        help="How many objects to sample when no GUIDs are given.",
    ),
    key_path: Optional[str] = typer.Option(
        None, "--key-path",
        help="Break-glass: local Gen3 API key JSON file, instead of the "
             "env's secret.",
    ),
) -> None:
    """Prove registered objects are downloadable end to end.

    Walks Indexd -> DRS -> Fence signed URL for each GUID and exits non-zero
    if any object fails, so it can gate a deployment step. Read-only and
    local-only (seconds, not a long job — there is nothing to dispatch to EC2).

    The env selects the API key secret and the key's JWT selects the commons,
    so a staging env checks staging. There is no URL to pass.

    With no GUIDs, the newest --limit objects for this commons are sampled
    from the indexd registry (latest revision per baseid). The registry may
    live in a different AWS account than the commons — sampling needs an env
    whose AWS profile can reach it; otherwise pass GUIDs explicitly.

    Examples:
      g3dt indexd check-download --env staging                 # sample 25 newest
      g3dt indexd check-download --env staging --limit 50
      g3dt indexd check-download --env prod PREFIX/aaa PREFIX/bbb
    """
    env = resolve.active_env(env)
    # Validate the env before spawning a subprocess: an unknown env should
    # fail here with the config error, not deep inside the script.
    e = env_of(env)

    args: List[str] = ["--env", e.name]
    if key_path:
        args += ["--key-path", key_path]
    if guids:
        args += list(guids)
    else:
        args += ["--limit", str(limit)]
    runner.run(runner.python_script(_CHECK_DOWNLOAD, *args))
