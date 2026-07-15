"""Root ``g3dt`` Typer application.

Assembles every command group and the top-level ``version`` / ``docs`` helpers.
The console-script entry point in pyproject.toml points at :func:`main`.
"""
from __future__ import annotations

import typer

from g3dt.cli import (
    config_cmds,
    delete_cmds,
    dict_cmds,
    ec2_cmds,
    indexd_cmds,
    jobs,
    k8s,
    metadata,
    synth,
)

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Gen3 DataOps toolkit. Run [bold]g3dt docs[/bold] for an overview.",
)

app.add_typer(dict_cmds.app, name="dict")
app.add_typer(synth.app, name="synth")
app.add_typer(metadata.app, name="metadata")
app.add_typer(delete_cmds.app, name="delete")
app.add_typer(k8s.app, name="k8s")
app.add_typer(indexd_cmds.app, name="indexd")
app.add_typer(ec2_cmds.app, name="ec2")
app.add_typer(jobs.app, name="jobs")
app.add_typer(config_cmds.app, name="config")


_DOCS = """\
Gen3 DataOps toolkit (g3dt) — operations overview
=================================================

Configuration: two kinds, nothing else
  - INPUTS live in the CDK repo (gen3-aws-data-pipeline) as
    config/<project>.<env>.json, read only by `cdk deploy`.
  - Everything else is resolved live from SSM (/{project}/{env}/...), which
    `cdk deploy` publishes. The only local file is the g3dt.yaml marker
    (project/region/default_env, optional profiles:/studies: maps), searched
    at ./g3dt.yaml, ~/.g3dt/g3dt.yaml, /etc/g3dt/g3dt.yaml.

Mental model: two execution planes
  - Control plane (LOCAL): dict deploy, k8s restarts. These use the interactive
    `argocd login --sso` browser flow and AWS named profiles, so they run on
    your laptop only.
  - Data plane (LONG jobs): metadata upload/delete, indexd register. Add
    `--on ec2` to run them on the env's job box via SSM (disconnect-safe);
    watch with `g3dt jobs status|logs <run-id> --follow`.

Discover everything
  g3dt --help                      list all command groups
  g3dt <group> --help              commands + options for a group
  g3dt config envs                 environments with a deployed SSM tree
  g3dt config studies              studies from your g3dt.yaml marker
  g3dt config show --env test      resolved settings (safe, read-only)

Typical release runbook (staging shown; repeat for prod with care)
  1. g3dt dict deploy   --env staging
  2. g3dt metadata upload --study <study> --env staging --on ec2
  3. g3dt jobs logs <run-id> --follow
  4. g3dt k8s restart-etl --env staging

Synthetic data (test only, all local)
  g3dt synth deploy --env test

EC2 / SSM prerequisites
  - The env's job box is created by the CDK (ec2-job-runner stack): SSM-managed,
    toolkit pre-installed by user-data, instance id published to SSM.
  - Local profile needs: ssm:SendCommand / ssm:GetCommandInvocation,
    s3:GetObject on the log prefix, ec2:Start/Stop/DescribeInstances.

NOT run by this CLI: the Glue jobs (validation, release-JSON) and the CodeBuild
dbt pipelines. Those are deployed and triggered via the CDK repo and the
project's dbt repo.
"""


@app.command()
def docs() -> None:
    """Print the operations overview (mental model, runbook, prerequisites)."""
    typer.echo(_DOCS)


@app.command()
def version() -> None:
    """Print the installed gen3-dataops-toolkit version."""
    try:
        from importlib.metadata import version as _v

        typer.echo(_v("gen3-dataops-toolkit"))
    except Exception:  # pragma: no cover - fallback when not installed
        typer.echo("unknown")


def main() -> None:
    """Console-script entry point."""
    app()


if __name__ == "__main__":
    main()
