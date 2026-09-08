"""`g3dt dict` — data dictionary operations (pull / upload / deploy).

All local: dictionary deploy restarts Gen3 schema microservices via the ArgoCD
SSO browser flow, which only works interactively on the laptop.

The schema repo is an env input (``app/schema_repo`` in SSM), so any project
can point at its own dictionary repo. Downloads land in ``~/.g3dt/schemas/``
(the toolkit is installable-only — nothing is written into the package).
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

import typer

from g3dt.config import (
    ConfigError,
    dictionary_filename,
    dictionary_url,
    normalize_s3_location,
    script_env,
)
from g3dt.cli._internal import resolve, runner, safety
from g3dt.cli._internal.resolve import env_of
from g3dt.cli._internal.helptext import ENV_OPT

app = typer.Typer(
    no_args_is_help=True,
    help="Data dictionary operations: pull, upload, deploy, status (local).",
)

SCHEMA_DIR = Path("~/.g3dt/schemas").expanduser()


def _version(env_cfg, override):
    return override or env_cfg.dictionary_version


def _bare_version(v) -> str:
    """``v1.2.0`` / ``1.2.0`` / ``V1.2.0`` -> ``1.2.0`` (None -> "")."""
    return (v or "").strip().lstrip("vV")


def warn_if_overridden(env_cfg, version: Optional[str]) -> None:
    """Say so, loudly, when the deployed version isn't the one SSM declares.

    An override is legitimate -- promoting one dictionary through environments
    shouldn't need a `cdk deploy` per env -- but it leaves SSM (and so
    `g3dt config show`) describing a different version than the bucket holds.
    Naming both keeps that discoverable instead of silent; `g3dt config diff`
    is what reconciles it once the CDK config catches up.
    """
    if version and version != env_cfg.dictionary_version:
        typer.secho(
            f"Overriding the declared version: SSM says "
            f"{env_cfg.dictionary_version}, using {version}. `config show` will "
            f"keep reporting {env_cfg.dictionary_version} until "
            f"config/<project>.{env_cfg.name}.json is updated and redeployed.",
            fg=typer.colors.YELLOW,
        )


@app.command()
def pull(
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
    version: Optional[str] = typer.Option(
        None, "--version", "-v",
        help="Dictionary git tag (default: the env's version).",
    ),
) -> None:
    """Download the dictionary JSON from the env's schema repo.

    Where it comes from is config: `app/schema_repo` plus the optional
    `app/dictionary_base_url` and `app/dictionary_path`.

    Examples:
      g3dt dict pull --env test
      g3dt dict pull --env staging --version v1.1.5
    """
    env = resolve.active_env(env)
    e = env_of(env)
    warn_if_overridden(e, version)
    v = _version(e, version)
    runner.run(
        runner.bash_script(
            "services/dictionary/pull_dict.sh",
            dictionary_url(e, v),
            dictionary_filename(e, v),
        ),
        env=script_env(e, v),
    )


@app.command()
def status(
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
    strict: bool = typer.Option(
        False, "--strict",
        help="Exit 1 when the deployed version differs from the declared one.",
    ),
) -> None:
    """Show which dictionary version is deployed, next to the declared one.

    Declared is the env's `dictionary_version` (a CDK input — what
    `g3dt config show` reports). Deployed is the `version` stamped on the S3
    object at `schema_s3_uri` by `dict upload`, i.e. what the commons'
    services actually read on their next restart. The two drift after
    `dict deploy --version <tag>` until the CDK config is updated and
    redeployed (`g3dt config diff` shows that half). Read-only.

    Examples:
      g3dt dict status --env staging
      g3dt dict status --env staging --strict    # drift gate for scripts
    """
    from botocore.exceptions import ClientError

    env = resolve.active_env(env)
    e = env_of(env)
    _, session = resolve.rc_session_of(env)
    try:
        location = normalize_s3_location(e.schema_s3_uri, param="schema_s3_uri")
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if "/" not in location:
        typer.secho(
            f"schema_s3_uri '{e.schema_s3_uri}' has no object key — expected "
            f"<bucket>/<key>.", fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)
    bucket, key = location.split("/", 1)
    deployed, uploaded = None, None
    try:
        head = session.client("s3").head_object(Bucket=bucket, Key=key)
        deployed = head.get("Metadata", {}).get("version")
        uploaded = head.get("LastModified")
    except ClientError as exc:
        code = exc.response.get("Error", {}).get("Code", "")
        if code not in ("404", "NoSuchKey", "NotFound"):
            typer.secho(
                f"Cannot read s3://{location} ({code or exc}).",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(1)

    declared = e.dictionary_version
    typer.echo(f"declared : {declared}   (SSM app/dictionary_version)")
    if uploaded is None:
        typer.echo(f"deployed : (nothing at s3://{location} — run `g3dt dict deploy`)")
        state = "MISSING"
    else:
        stamp = deployed or "(no version stamp on the object)"
        when = uploaded.strftime("%Y-%m-%d %H:%M %Z") if hasattr(uploaded, "strftime") else uploaded
        typer.echo(f"deployed : {stamp}   (s3://{location}, uploaded {when})")
        # The S3 stamp is the JSON's _settings _dict_version (often "1.2.0")
        # while the declared tag carries the git "v" prefix; the same version.
        state = "in sync" if _bare_version(deployed) == _bare_version(declared) else "DRIFT"
    if state == "in sync":
        typer.secho("status   : in sync", fg=typer.colors.GREEN)
        return
    typer.secho(
        f"status   : {state} — the services will read {deployed or 'nothing'} "
        f"while the CDK config declares {declared}. Deploy with "
        f"`g3dt dict deploy`, or update dictionaryVersion in the wrapper "
        f"config and redeploy.",
        fg=typer.colors.YELLOW,
    )
    if strict:
        raise typer.Exit(1)


@app.command()
def upload(
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
    version: Optional[str] = typer.Option(
        None, "--version", "-v",
        help="Dictionary git tag (default: the env's version).",
    ),
) -> None:
    """Upload the (already pulled) dictionary JSON to the env's S3 location.

    Stamps the object's S3 metadata `version` from the JSON's _settings, which
    is what `g3dt dict status` reads back.

    Examples:
      g3dt dict upload --env staging
      g3dt dict upload --env staging --version v1.1.7

    Targeting production requires typing the context/env name to confirm.
    """
    env = resolve.active_env(env)
    e = env_of(env)
    safety.confirm_prod_strict("dictionary upload", env)
    warn_if_overridden(e, version)
    v = _version(e, version)
    local_file = str(SCHEMA_DIR / dictionary_filename(e, v))
    s3_uri = f"s3://{e.schema_s3_uri}"
    args = [local_file, s3_uri]
    if e.aws_profile:
        args.append(e.aws_profile)
    runner.run(
        runner.python_script("services/dictionary/upload_dictionary.py", *args),
        env=script_env(e, v),
    )


@app.command()
def deploy(
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
    version: Optional[str] = typer.Option(
        None, "--version", "-v",
        help="Dictionary git tag (default: the env's version).",
    ),
    restart_services: Optional[str] = typer.Option(
        None, "--restart-services",
        help="Comma-separated deployment names restarted after the upload, in "
        "order; default: the env's SSM app/restart_services (the CDK "
        "config's k8s.schemaRestartServices).",
    ),
    sync: bool = typer.Option(
        False, "--sync",
        help="Run 'argocd app sync' on the commons app before the restart "
        "(off by default; add it when the app is behind the merged revision).",
    ),
) -> None:
    """Pull + upload the dictionary and restart Gen3 schema microservices.

    Wraps services/dictionary/deploy_dd.sh. Requires an interactive ArgoCD SSO
    login, so it runs locally only. The restart never syncs the ArgoCD app
    unless --sync is given.

    The version defaults to the env's `dictionary_version`, a CDK INPUT: edit
    config/<project>.<env>.json in your deployment wrapper (the repo that pins
    the aws-gen3-pipeline template) and `cdk deploy` to change what an env
    declares. Pass --version to deploy a different tag now
    without that round trip — which is how one dictionary gets promoted across
    environments. `g3dt config diff` reports the resulting drift until the CDK
    config catches up.

    Examples:
      g3dt dict deploy --env test
      g3dt dict deploy --env test --version v1.1.7
      g3dt dict deploy --env staging --version v1.1.7   # promote the same tag

    Targeting production requires typing the context/env name to confirm —
    this uploads a new schema to the live commons and restarts its services.
    """
    env = resolve.active_env(env)
    e = env_of(env)
    safety.confirm_prod_strict("dictionary deploy", env)
    warn_if_overridden(e, version)
    env_vars = script_env(e, _version(e, version))
    if restart_services:
        env_vars["G3DT_RESTART_SERVICES"] = restart_services
    if sync:
        env_vars["G3DT_SYNC"] = "1"
    runner.run(
        runner.bash_script("services/dictionary/deploy_dd.sh", env),
        env=env_vars,
    )
