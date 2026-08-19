"""`g3dt k8s` — restart Gen3 microservices / ETL via ArgoCD (LOCAL only).

These use ``argocd login --sso`` (a browser flow), so they cannot run headless
on EC2. The wrapped scripts receive their settings as ``G3DT_*`` environment
variables resolved from SSM — they read no config files.

The restart targets resolve with precedence CLI flags > SSM > default: the
CDK config's optional ``k8s`` block publishes ``app/restart_services`` (a
comma-separated list, restarted in order) and ``app/etl_cronjob``;
``--restart-services`` / ``--etl-cronjob`` override them for one run, and
environments deployed without the block keep the classic Gen3 set
(sheepdog, peregrine, guppy, portal / etl-cronjob).
"""
from __future__ import annotations

from typing import Optional

import typer

from g3dt.config import script_env
from g3dt.cli._internal import runner
from g3dt.cli._internal.resolve import env_of

app = typer.Typer(no_args_is_help=True, help="ArgoCD / Kubernetes restarts (local).")

_SCHEMA = "services/k8s_ops/argocd_restart_schema.sh"
_ETL = "services/k8s_ops/argocd_restart_etl.sh"
_ETL_AND_MS = "services/k8s_ops/restart_etl_and_ms.sh"

_RESTART_SERVICES_HELP = (
    "Comma-separated deployment names to restart, in order; default: the "
    "env's SSM app/restart_services (the CDK config's k8s.schemaRestartServices)."
)
_ETL_CRONJOB_HELP = (
    "ETL cronjob name; default: the env's SSM app/etl_cronjob "
    "(the CDK config's k8s.etlCronjob)."
)


def restart_env(e, restart_services: Optional[str] = None,
                etl_cronjob: Optional[str] = None) -> dict:
    """script_env plus per-run restart-target overrides (flags beat SSM)."""
    env_vars = script_env(e)
    if restart_services:
        env_vars["G3DT_RESTART_SERVICES"] = restart_services
    if etl_cronjob:
        env_vars["G3DT_ETL_CRONJOB"] = etl_cronjob
    return env_vars


@app.command(name="restart-schema")
def restart_schema(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    sync: bool = typer.Option(False, "--sync", "-s", help="argocd app sync first."),
    restart_services: Optional[str] = typer.Option(
        None, "--restart-services", help=_RESTART_SERVICES_HELP
    ),
) -> None:
    """Restart the schema microservices, in the env's configured order."""
    e = env_of(env)
    args = ["-d", e.domain, "-a", e.app_name, "-n", e.namespace]
    if sync:
        args.append("-s")
    runner.run(
        runner.bash_script(_SCHEMA, *args),
        env=restart_env(e, restart_services=restart_services),
    )


@app.command(name="restart-etl")
def restart_etl(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    sync: bool = typer.Option(False, "--sync", "-s", help="argocd app sync first."),
    etl_cronjob: Optional[str] = typer.Option(
        None, "--etl-cronjob", help=_ETL_CRONJOB_HELP
    ),
) -> None:
    """Create + run the ETL cronjob and wait for completion."""
    e = env_of(env)
    args = ["-e", env]
    if sync:
        args.append("-s")
    runner.run(
        runner.bash_script(_ETL, *args),
        env=restart_env(e, etl_cronjob=etl_cronjob),
    )


@app.command(name="restart-ms")
def restart_ms(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    restart_services: Optional[str] = typer.Option(
        None, "--restart-services", help=_RESTART_SERVICES_HELP
    ),
    etl_cronjob: Optional[str] = typer.Option(
        None, "--etl-cronjob", help=_ETL_CRONJOB_HELP
    ),
) -> None:
    """Restart both ETL and schema microservices (wraps restart_etl_and_ms.sh)."""
    e = env_of(env)
    runner.run(
        runner.bash_script(_ETL_AND_MS, env),
        env=restart_env(e, restart_services=restart_services, etl_cronjob=etl_cronjob),
    )
