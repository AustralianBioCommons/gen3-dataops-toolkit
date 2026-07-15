"""`g3dt config` — discover environments, studies, and resolved settings.

Everything the toolkit uses at runtime is resolved from SSM
(``/{project}/{env}/...``, published by ``cdk deploy`` in
gen3-aws-data-pipeline). These commands make that tree browsable so an
operator can answer "what environments exist?" and "what will this actually
do?" without touching the AWS console. The only local file is the tiny
``g3dt.yaml`` bootstrap marker (project/region/default_env, optional per-env
``profiles:`` and ``studies:`` maps) — ``g3dt config set`` edits that marker,
nothing else.
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from g3dt import config
from g3dt.cli._internal.resolve import env_of, study_of

app = typer.Typer(
    no_args_is_help=True,
    help="Inspect the resolved SSM config; edit the local bootstrap marker.",
)


@app.command()
def envs() -> None:
    """List the environments with a deployed SSM tree for this project."""
    try:
        for name in config.list_envs():
            typer.echo(name)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)


@app.command()
def studies(
    env: str = typer.Option(
        None,
        "--env",
        "-e",
        help="Also check the env's S3 registry (s3://<metadata-bucket>/config/studies.yaml).",
    ),
) -> None:
    """List the configured studies (bare names).

    The registry comes from the marker's studies: block, or — pass --env —
    from the env's S3 registry, which is what the EC2 job box uses.
    """
    names = config.list_studies(env=env)
    if not names:
        typer.secho(
            "No studies configured. Add a studies: block to your g3dt.yaml "
            "marker, or upload config/studies.yaml to the env's metadata "
            "bucket (and pass --env).",
            fg=typer.colors.YELLOW,
        )
        return
    for name in names:
        typer.echo(name)


@app.command()
def show(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    study: str = typer.Option(
        None, "--study", "-s", help="Optional study to resolve against the env."
    ),
    full: bool = typer.Option(
        False, "--full", help="Also dump the raw SSM subtree (every parameter)."
    ),
) -> None:
    """Print fully-resolved settings for an env (and optionally a study).

    Use this before any job to confirm the exact names the tooling will use —
    the staging-vs-prod safety check. Everything shown is read live from the
    env's SSM tree; nothing is local except the marker's project/profiles.
    """
    e = env_of(env)
    typer.secho(f"Environment: {e.name}", bold=True)
    typer.echo(f"  is_ec2             : {e.is_ec2}")
    typer.echo(f"  region             : {e.region}")
    typer.echo(f"  aws_profile        : {e.aws_profile or '(ambient credentials)'}")
    typer.echo(f"  aws_secret_name    : {e.aws_secret_name}")
    typer.echo(f"  dictionary_version : {e.dictionary_version}")
    typer.echo(f"  schema_s3_uri      : {e.schema_s3_uri}")
    typer.echo(f"  schema_repo        : {e.schema_repo}")
    typer.echo(f"  domain             : {e.domain}")
    typer.echo(f"  app_name           : {e.app_name}")
    typer.echo(f"  namespace          : {e.namespace}")
    typer.echo(f"  cluster_name       : {e.cluster_name}")
    typer.echo(f"  ec2_instance_id    : {e.ec2_instance_id}")
    if study:
        s = study_of(study, env)
        typer.secho(f"Study: {study} -> {s.key}", bold=True)
        typer.echo(f"  project_id       : {s.project_id}")
        typer.echo(f"  program_id       : {s.program_id}")
        typer.echo(f"  s3_metadata_path : {s.s3_metadata_path}")
    if full:
        from g3dt import resolver

        marker = config.load_marker()
        project = config.require_project(marker)
        rc = resolver.resolve(
            project, config.env_base(env),
            profile=config.aws_profile_for(env, marker),
        )
        typer.secho(
            f"\n/{rc.project}/{rc.env}  ({len(rc.params)} parameters)", bold=True
        )
        for key in sorted(rc.params):
            typer.echo(f"  {key:<32} {rc.params[key]}")


@app.command()
def diff(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        help="The env's INPUT file in the CDK repo, e.g. "
        "../gen3-aws-data-pipeline/config/etl.test.json.",
    ),
) -> None:
    """Flag drift between SSM and the committed CDK INPUT file.

    Compares the mirrored app facts (``app/*``) and the toolkit pin
    (``meta/toolkitVersion``) in SSM against ``config/<project>.<env>.json``.
    A difference means "someone edited the JSON but didn't `cdk deploy`" (or
    vice-versa). Exits 1 on drift, so it can gate CI.
    """
    from g3dt import resolver

    marker = config.load_marker()
    project = config.require_project(marker)
    try:
        rc = resolver.resolve(
            project, config.env_base(env),
            profile=config.aws_profile_for(env, marker),
        )
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    inputs = json.loads(file.read_text())
    gen3 = inputs.get("gen3", {})
    # camelCase input field -> snake_case SSM leaf (the CDK's mirror contract)
    camel_to_leaf = {
        "dictionaryVersion": "dictionary_version",
        "awsSecretName": "aws_secret_name",
        "schemaS3Uri": "schema_s3_uri",
        "domain": "domain",
        "appName": "app_name",
        "namespace": "namespace",
        "clusterName": "cluster_name",
        "schemaRepo": "schema_repo",
    }
    drift = False

    def check(label: str, file_value, ssm_value) -> None:
        nonlocal drift
        if file_value != ssm_value:
            drift = True
            typer.secho(
                f"  DRIFT {label}: file={file_value!r}  ssm={ssm_value!r}",
                fg=typer.colors.YELLOW,
            )

    for camel, leaf in camel_to_leaf.items():
        check(f"gen3.{camel}", gen3.get(camel), rc.get(f"app/{leaf}"))
    check("toolkitVersion", inputs.get("toolkitVersion"), rc.get("meta/toolkitVersion"))

    if not drift:
        typer.secho(
            f"No drift: SSM /{project}/{config.env_base(env)} matches {file}.",
            fg=typer.colors.GREEN,
        )
    raise typer.Exit(1 if drift else 0)


@app.command("dbt-env")
def dbt_env(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
) -> None:
    """Emit `export` lines for the env's dbt settings (resolved from SSM).

    The dbt template's profiles.yml / dbt_project.yml read their derived names
    from env_var(); this command is the one source of those values, for both
    CodeBuild and a laptop:

        eval "$(g3dt config dbt-env --env test)" && dbt build
    """
    import shlex

    from g3dt import resolver

    marker = config.load_marker()
    project = config.require_project(marker)
    base = config.env_base(env)
    profile = None if env.endswith("_ec2") else config.aws_profile_for(base, marker)
    try:
        rc = resolver.resolve(project, base, profile=profile)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    values = {
        "G3DT_REGION": rc.region,
        "G3DT_ATHENA_WORKGROUP": rc.athena_workgroup,
        "G3DT_ATHENA_OUTPUT": rc.athena_output_location,
        "G3DT_DB_RAW_BRONZE": rc.get("glue/db/rawBronze"),
        "G3DT_DB_RAW_SILVER": rc.get("glue/db/rawSilver"),
        "G3DT_DB_RAW_GOLD": rc.get("glue/db/rawGold"),
        "G3DT_S3_SILVER_DATA_DIR": f"s3://{rc.get('buckets/rawSilver')}/dbt/",
        "G3DT_S3_GOLD_DATA_DIR": f"s3://{rc.get('buckets/rawGold')}/dbt/",
    }
    if profile:
        values["G3DT_AWS_PROFILE"] = profile
    for key, value in values.items():
        if value is not None:
            typer.echo(f"export {key}={shlex.quote(str(value))}")


@app.command("set")
def set_value(
    key: str = typer.Argument(..., help="Bootstrap key: project, region, default_env."),
    value: str = typer.Argument(..., help="New value, e.g. etl."),
) -> None:
    """Set one bootstrap key in the local g3dt.yaml marker.

    Only the bootstrap (project/region/default_env) lives locally. Deployed
    settings — dictionary_version, domain, buckets, ... — are CDK INPUTS: edit
    config/<project>.<env>.json in gen3-aws-data-pipeline and `cdk deploy`;
    the values flow to SSM, which is what every consumer reads.
    """
    try:
        old, new, path = config.set_marker_value(key, value)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho(f"Updated {key}: {old} -> {new} ({path})", fg=typer.colors.GREEN)
