"""`g3dt dict` — data dictionary operations (pull / upload / deploy).

All local: dictionary deploy restarts Gen3 schema microservices via the ArgoCD
SSO browser flow, which only works interactively on the laptop.

The schema repo is an env input (``app/schema_repo`` in SSM), so any project
can point at its own dictionary repo. Downloads land in ``~/.g3dt/schemas/``
(the toolkit is installable-only — nothing is written into the package).
"""
from __future__ import annotations

from pathlib import Path

import typer

from g3dt.config import script_env
from g3dt.cli._internal import runner
from g3dt.cli._internal.resolve import env_of

app = typer.Typer(no_args_is_help=True, help="Data dictionary operations (local).")

#: Raw-GitHub URL template; the trailing path is the schema repo's layout
#: convention (see AustralianBioCommons/acdc-schema-json), not a project name.
_DICT_URL_TMPL = (
    "https://raw.githubusercontent.com/{repo}/"
    "refs/tags/{version}/dictionary/prod_dict/acdc_schema.json"
)

SCHEMA_DIR = Path("~/.g3dt/schemas").expanduser()


def _version(env_cfg, override):
    return override or env_cfg.dictionary_version


def dict_url(env_cfg, version: str) -> str:
    return _DICT_URL_TMPL.format(repo=env_cfg.schema_repo, version=version)


@app.command()
def pull(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    version: str = typer.Option(
        None, "--version", help="Dictionary git tag (default: the env's version)."
    ),
) -> None:
    """Download the dictionary JSON from the env's schema repo.

    Examples:
      g3dt dict pull --env test
      g3dt dict pull --env staging --version v1.1.5
    """
    e = env_of(env)
    url = dict_url(e, _version(e, version))
    runner.run(
        runner.bash_script("services/dictionary/pull_dict.sh", url),
        env=script_env(e),
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
    v = _version(e, version)
    local_file = str(SCHEMA_DIR / f"acdc_schema_{v}.json")
    s3_uri = f"s3://{e.schema_s3_uri}"
    args = [local_file, s3_uri]
    if e.aws_profile:
        args.append(e.aws_profile)
    runner.run(
        runner.python_script("services/dictionary/upload_dictionary.py", *args),
        env=script_env(e),
    )


@app.command()
def deploy(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
) -> None:
    """Pull + upload the dictionary and restart Gen3 schema microservices.

    Wraps services/dictionary/deploy_dd.sh. Requires an interactive ArgoCD SSO
    login, so it runs locally only.

    The deployed version is the env's `dictionary_version` — a CDK INPUT. To
    change it, edit config/<project>.<env>.json in gen3-aws-data-pipeline and
    `cdk deploy` (the value flows to SSM), then re-run this command.

    Examples:
      g3dt dict deploy --env test
    """
    e = env_of(env)
    runner.run(
        runner.bash_script("services/dictionary/deploy_dd.sh", env),
        env=script_env(e),
    )
