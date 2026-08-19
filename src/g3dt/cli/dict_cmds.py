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

from g3dt.config import dictionary_filename, dictionary_url, script_env
from g3dt.cli._internal import runner
from g3dt.cli._internal.resolve import env_of

app = typer.Typer(no_args_is_help=True, help="Data dictionary operations (local).")

SCHEMA_DIR = Path("~/.g3dt/schemas").expanduser()


def _version(env_cfg, override):
    return override or env_cfg.dictionary_version


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
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    version: str = typer.Option(
        None, "--version", help="Dictionary git tag (default: the env's version)."
    ),
) -> None:
    """Download the dictionary JSON from the env's schema repo.

    Where it comes from is config: `app/schema_repo` plus the optional
    `app/dictionary_base_url` and `app/dictionary_path`.

    Examples:
      g3dt dict pull --env test
      g3dt dict pull --env staging --version v1.1.5
    """
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
def upload(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    version: str = typer.Option(
        None, "--version", help="Dictionary git tag (default: the env's version)."
    ),
) -> None:
    """Upload the (already pulled) dictionary JSON to the env's S3 location."""
    e = env_of(env)
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
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    version: str = typer.Option(
        None, "--version", help="Dictionary git tag (default: the env's version)."
    ),
    restart_services: Optional[str] = typer.Option(
        None, "--restart-services",
        help="Comma-separated deployment names restarted after the upload, in "
        "order; default: the env's SSM app/restart_services.",
    ),
) -> None:
    """Pull + upload the dictionary and restart Gen3 schema microservices.

    Wraps services/dictionary/deploy_dd.sh. Requires an interactive ArgoCD SSO
    login, so it runs locally only.

    The version defaults to the env's `dictionary_version`, a CDK INPUT: edit
    config/<project>.<env>.json in gen3-aws-data-pipeline and `cdk deploy` to
    change what an env declares. Pass --version to deploy a different tag now
    without that round trip — which is how one dictionary gets promoted across
    environments. `g3dt config diff` reports the resulting drift until the CDK
    config catches up.

    Examples:
      g3dt dict deploy --env test
      g3dt dict deploy --env test --version v1.1.7
      g3dt dict deploy --env staging --version v1.1.7   # promote the same tag
    """
    e = env_of(env)
    warn_if_overridden(e, version)
    env_vars = script_env(e, _version(e, version))
    if restart_services:
        env_vars["G3DT_RESTART_SERVICES"] = restart_services
    runner.run(
        runner.bash_script("services/dictionary/deploy_dd.sh", env),
        env=env_vars,
    )
