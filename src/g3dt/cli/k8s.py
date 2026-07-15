"""`g3dt k8s` — restart Gen3 microservices / ETL via ArgoCD (LOCAL only).

These use ``argocd login --sso`` (a browser flow), so they cannot run headless
on EC2. The wrapped scripts receive their settings as ``G3DT_*`` environment
variables resolved from SSM — they read no config files.
"""
from __future__ import annotations

import typer

from g3dt.config import script_env
from g3dt.cli._internal import runner
from g3dt.cli._internal.resolve import env_of

app = typer.Typer(no_args_is_help=True, help="ArgoCD / Kubernetes restarts (local).")

_SCHEMA = "services/k8s_ops/argocd_restart_schema.sh"
_ETL = "services/k8s_ops/argocd_restart_etl.sh"
_ETL_AND_MS = "services/k8s_ops/restart_etl_and_ms.sh"


@app.command(name="restart-schema")
def restart_schema(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    sync: bool = typer.Option(False, "--sync", "-s", help="argocd app sync first."),
) -> None:
    """Restart sheepdog/peregrine/guppy/portal (schema microservices)."""
    e = env_of(env)
    args = ["-d", e.domain, "-a", e.app_name, "-n", e.namespace]
    if sync:
        args.append("-s")
    runner.run(runner.bash_script(_SCHEMA, *args), env=script_env(e))


@app.command(name="restart-etl")
def restart_etl(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    sync: bool = typer.Option(False, "--sync", "-s", help="argocd app sync first."),
) -> None:
    """Create + run the ETL cronjob and wait for completion."""
    e = env_of(env)
    args = ["-e", env]
    if sync:
        args.append("-s")
    runner.run(runner.bash_script(_ETL, *args), env=script_env(e))


@app.command(name="restart-ms")
def restart_ms(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
) -> None:
    """Restart both ETL and schema microservices (wraps restart_etl_and_ms.sh)."""
    e = env_of(env)
    runner.run(runner.bash_script(_ETL_AND_MS, env), env=script_env(e))
