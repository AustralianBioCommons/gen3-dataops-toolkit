"""`acdc jobs` — track EC2-dispatched runs by their friendly run id."""
from __future__ import annotations

from typing import Optional

import typer

from g3dt.cli._internal import dispatch, registry

app = typer.Typer(no_args_is_help=True, help="Track EC2-dispatched jobs.")


def _status_color(state: str) -> str:
    """Map an SSM/derived status to a terminal colour for the all-runs listing."""
    if state == "Success":
        return typer.colors.GREEN
    if state in ("Failed", "Cancelled", "TimedOut"):
        return typer.colors.RED
    if state in ("Pending", "InProgress", "Delayed", "pending"):
        return typer.colors.YELLOW
    return typer.colors.BRIGHT_BLACK  # n/a (ssh) / unknown


@app.command(name="list")
def list_runs() -> None:
    """List recorded EC2 dispatches (most recent first)."""
    runs = registry.all_runs()
    if not runs:
        typer.echo("No dispatched runs recorded.")
        return
    for run_id in sorted(runs, reverse=True):
        rec = runs[run_id]
        typer.echo(
            f"{run_id}  env={rec.get('env')}  "
            f"via={rec.get('mechanism')}  instance={rec.get('instance_id')}"
        )


@app.command()
def status(
    run_id: Optional[str] = typer.Argument(
        None, help="Run id from dispatch. Omit to show all runs with their status."
    ),
) -> None:
    """Show SSM status for one run, or all recorded runs if no run id is given."""
    if run_id is None:
        runs = registry.all_runs()
        if not runs:
            typer.echo("No dispatched runs recorded.")
            return
        for rid in sorted(runs, reverse=True):
            rec = runs[rid]
            state = dispatch.status_label(rec)
            line = f"{rid}  env={rec.get('env')}  via={rec.get('mechanism')}  "
            typer.echo(line, nl=False)
            typer.secho(state, fg=_status_color(state))
        return

    inv = dispatch.status(run_id)
    state = inv.get("Status", "unknown") if isinstance(inv, dict) else "unknown"
    typer.secho(f"{run_id}: {state}", bold=True)
    if isinstance(inv, dict):
        out = inv.get("StandardOutputContent")
        err = inv.get("StandardErrorContent")
        if out:
            typer.echo(out)
        if err:
            typer.secho(err, fg=typer.colors.RED)


@app.command()
def stop(run_id: str = typer.Argument(..., help="Run id from dispatch.")) -> None:
    """Stop (cancel) a running EC2-dispatched job."""
    dispatch.stop(run_id)


@app.command()
def logs(
    run_id: str = typer.Argument(..., help="Run id from dispatch."),
    follow: bool = typer.Option(False, "--follow", "-f", help="Stream new output as it arrives."),
) -> None:
    """Print (and optionally follow) the CloudWatch logs for a dispatched run."""
    dispatch.logs(run_id, follow=follow)
