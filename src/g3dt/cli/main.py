"""Root ``g3dt`` Typer application.

Assembles every command group and the top-level ``version`` / ``docs`` helpers.
The console-script entry point in pyproject.toml points at :func:`main`.
"""
from __future__ import annotations

from typing import Optional

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
    pipeline_cmds,
    release_cmds,
    study_cmds,
    synth,
)

app = typer.Typer(
    no_args_is_help=True,
    rich_markup_mode="rich",
    help="Gen3 DataOps toolkit. Commands act on the current [bold]context[/bold] "
         "— a named (project, env, AWS profile, region) tuple; see "
         "[bold]g3dt config[/bold]. Run [bold]g3dt docs[/bold] for an overview.",
)


@app.callback()
def _root(
    ctx_name: Optional[str] = typer.Option(
        None,
        "--ctx",
        "-c",
        help="One-shot context override, e.g. myproj/staging "
             "(see 'g3dt config contexts').",
    ),
) -> None:
    """Record the global --ctx override before any sub-command runs."""
    from g3dt import contexts

    contexts.set_override(ctx_name)

app.add_typer(dict_cmds.app, name="dict")
app.add_typer(synth.app, name="synth")
app.add_typer(study_cmds.app, name="study")
app.add_typer(metadata.app, name="metadata")
app.add_typer(delete_cmds.app, name="delete")
app.add_typer(k8s.app, name="k8s")
app.add_typer(indexd_cmds.app, name="indexd")
app.add_typer(ec2_cmds.app, name="ec2")
app.add_typer(jobs.app, name="jobs")
app.add_typer(config_cmds.app, name="config")
app.add_typer(release_cmds.app, name="release")
app.add_typer(pipeline_cmds.app, name="pipeline")


_DOCS = """\
Gen3 DataOps toolkit (g3dt) — operations overview
=================================================

Configuration: three kinds, nothing else
  - INPUTS live in your deployment wrapper repo as config/<project>.<env>.json,
    read only by `cdk deploy` (via the aws-gen3-pipeline template).
  - OUTPUTS are resolved live from SSM (/{project}/{env}/...), which
    `cdk deploy` publishes. The only local file is the g3dt.yaml marker,
    searched at ./g3dt.yaml, ~/.g3dt/g3dt.yaml, /etc/g3dt/g3dt.yaml.
  - OPERATIONAL state is written by the toolkit itself: the study registry
    (SSM /{project}/{env}/studies/*, managed with `g3dt study`) and the
    Iceberg ledgers (releases, upload receipts, indexd registry).

Contexts: what am I pointed at?
  A context is a named (project, env, profile, region) tuple. Every command
  prints the active one first (to stderr) — read that line before anything
  else. Production contexts are marked [PROD] and gate destructive actions
  behind typing the context name; --yes never bypasses that.
    g3dt config discover <aws-profile> --add   find + register deployed infra
    g3dt config contexts                        list them (current marked *)
    g3dt config use <name>                      act there from now on
    g3dt --ctx <name> <command>                 one-shot override
  Legacy markers (project/default_env/profiles keys) keep working unchanged;
  `--env <name>` selects the matching context.

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
  g3dt config contexts             your contexts (current marked *; add
                                   --verify to check what is deployed)
  g3dt config envs                 environments with a deployed SSM tree
  g3dt study list                  the env's study registry (config studies
                                   is an alias)
  g3dt config show                 resolved settings for the current context

Typical release runbook (staging shown; repeat for prod with care)
  1. g3dt dict deploy   --env staging
  2. g3dt study repoint --latest --env staging   point the registry at the
                                                 newest release (validates
                                                 every target first)
  3. g3dt metadata upload --study <study> --env staging --on ec2
  4. g3dt jobs logs <run-id> --follow
  5. g3dt k8s restart-etl --env staging

Promoting one dictionary across environments
  The source repo/path are env inputs; usually only the tag changes, and it
  changes far more often than infrastructure does. So --version deploys a tag
  without a `cdk deploy` per env:
    g3dt dict deploy --env test    --version v1.1.7
    g3dt dict deploy --env staging --version v1.1.7
  The override does not persist: `g3dt config show` keeps reporting the declared
  version, and `g3dt config diff --env <env> --file <wrapper>/config/<project>.<env>.json`
  reports the gap (exit 1) until that INPUT file is updated to match.

Data releases (the dbt pipeline; see the project's dbt repo)
  git tag data-v1.4.0 && git push origin data-v1.4.0
  g3dt pipeline status --env staging           which stage is running/failed
  g3dt pipeline logs   --env staging --follow  live dbt + release-writer output
  (the pipeline itself runs `g3dt release write` — no names needed anywhere)

Synthetic data (test only, all local)
  g3dt synth deploy --env test --studies synthetic_dataset_1 -n 100
  Batches are only schema-valid against the dictionary that generated them, so
  each one records its dictionary version and `g3dt synth upload` refuses a
  batch that does not match the version being uploaded.

EC2 / SSM prerequisites
  - The env's job box is created by the CDK (ec2-job-runner stack): SSM-managed,
    toolkit pre-installed by user-data, instance id published to SSM.
  - Local profile needs: ssm:SendCommand / ssm:GetCommandInvocation,
    s3:GetObject on the log prefix, ec2:Start/Stop/DescribeInstances.

NOT run by this CLI: the Glue jobs (validation, release-JSON). The CodeBuild
dbt pipelines are triggered from the project's dbt repo (branch push = CI,
data-v* tag = release) and watched with `g3dt pipeline status|logs`.
"""


@app.command()
def docs() -> None:
    """Print the operations overview (mental model, runbook, prerequisites)."""
    from g3dt.cli._internal import resolve

    resolve.announce_context()
    typer.echo(_DOCS)


@app.command()
def version() -> None:
    """Print the installed gen3-dataops-toolkit version."""
    from g3dt.cli._internal import resolve

    resolve.announce_context()
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
