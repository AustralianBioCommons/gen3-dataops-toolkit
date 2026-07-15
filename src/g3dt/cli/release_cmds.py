"""`g3dt release` — write the dbt data-release manifest to Athena.

The data half of the release decoupling: a `data-v*` tag on a project's dbt
repo drives CodePipeline → CodeBuild → `g3dt release write`, which records one
idempotent row per dbt model in the env's `releases` Iceberg table. Every name
it needs — release DB/table, the metadata bucket the table lives under, the
Athena workgroup output, the region — resolves from the env's SSM tree, so the
buildspec passes only `--env`, the version, and the commit SHA.
"""
from __future__ import annotations

import typer

from g3dt import config
from g3dt.utils import release_writer

app = typer.Typer(no_args_is_help=True, help="Write/inspect dbt data releases.")


@app.command()
def write(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    data_release_version: str = typer.Option(
        ...,
        "--data-release-version",
        help="Data version, e.g. 1.4.0 (from the data-v* tag, prefix stripped).",
    ),
    commit_id: str = typer.Option("", "--commit-id", help="Git SHA for auditing."),
    dbt_schema_path: str = typer.Option(
        "models/schema.yml",
        "--dbt-schema-path",
        help="dbt schema file listing the models to track (relative to the dbt project root).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", help="Print the resolved DB/table/SQL and write nothing."
    ),
) -> None:
    """Resolve release/db, release/table, athena/* from SSM, then write the rows.

    Idempotent: a (release_tag, model, db) row that already exists is skipped,
    so re-running the same tag is safe.
    """
    from g3dt import resolver

    marker = config.load_marker()
    project = config.require_project(marker)
    base = config.env_base(env)
    profile = None if env.endswith("_ec2") else config.aws_profile_for(base, marker)

    try:
        rc = resolver.resolve(project, base, profile=profile)
        typer.secho(
            f"Release target (from SSM /{project}/{base}): "
            f"{rc.release_db}.{rc.release_table} "
            f"(workgroup output {rc.athena_output_location})",
            bold=True,
        )
        release_writer.run(
            dbt_schema_path=dbt_schema_path,
            release_db=rc.release_db,
            release_table=rc.release_table,
            release_s3_location=f"s3://{rc.metadata_bucket}/",
            data_release_version=data_release_version,
            commit_id=commit_id,
            aws_region=rc.region,
            athena_s3_output=rc.athena_output_location,
            aws_profile=profile,
            dry_run=dry_run,
        )
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if dry_run:
        typer.secho("Dry run complete — nothing was written.", fg=typer.colors.GREEN)
