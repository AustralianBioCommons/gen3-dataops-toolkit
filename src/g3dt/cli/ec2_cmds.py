"""`g3dt ec2` — manage the env's EC2 job box (up / down / status).

The box is created per environment by the CDK (ec2-job-runner stack) and is
SSM-managed: no SSH key or bootstrap script is needed. Its instance id is
resolved from the env's own SSM tree (``ec2/instanceId``), so targeting
another environment's box is structurally impossible.
"""
from __future__ import annotations

import time

import typer

from g3dt import config
from g3dt.cli._internal.resolve import env_of
from g3dt.upload.metadata_submitter import create_boto3_session

app = typer.Typer(no_args_is_help=True, help="Manage the env's EC2 job box.")


def _resolve(env: str):
    """Return ``(auth_env, instance_id)`` for the env's box."""
    e = env_of(config.env_base(env))
    if not e.ec2_instance_id:
        typer.secho(
            f"No ec2/instanceId published for env '{e.name}'. Has the "
            f"ec2-job-runner stack been deployed? (cdk deploy in "
            f"gen3-aws-data-pipeline)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    return e, e.ec2_instance_id


def _session(e):
    return create_boto3_session(aws_profile=e.aws_profile, aws_region=e.region)


@app.command()
def up(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    wait: bool = typer.Option(
        True, "--wait/--no-wait", help="Wait until the box registers with SSM."
    ),
) -> None:
    """Start the env's job box and print how to reach it.

    Plain ``start_instances`` — the box was fully bootstrapped by CDK
    user-data (toolkit installed, marker written), so there is nothing to
    re-provision. Normal access is SSM (no SSH); a session command is printed
    as soon as the box is reachable.
    """
    e, instance_id = _resolve(env)
    session = _session(e)
    session.client("ec2").start_instances(InstanceIds=[instance_id])
    typer.secho(f"Start requested for {instance_id} ({e.name}).", fg=typer.colors.GREEN)
    if not wait:
        return

    ssm = session.client("ssm")
    typer.echo("Waiting for the box to register with SSM...")
    for _ in range(60):  # up to ~5 minutes
        info = ssm.describe_instance_information(
            Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
        ).get("InstanceInformationList", [])
        if info and info[0].get("PingStatus") == "Online":
            profile = f" --profile {e.aws_profile}" if e.aws_profile else ""
            typer.secho(f"{instance_id} is reachable.", fg=typer.colors.GREEN)
            typer.echo("  session : aws ssm start-session "
                       f"--target {instance_id}{profile}")
            typer.echo(f"  dispatch: g3dt <cmd> --env {config.env_base(env)} --on ec2")
            return
        time.sleep(5)
    typer.secho(
        f"{instance_id} started but has not registered with SSM yet; "
        f"check `g3dt ec2 status --env {env}` in a minute.",
        fg=typer.colors.YELLOW,
    )


@app.command()
def down(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
) -> None:
    """Stop the env's job box.

    (An idle box also stops itself: the CDK auto-stop alarm fires after 24h
    under 1% CPU.)
    """
    e, instance_id = _resolve(env)
    _session(e).client("ec2").stop_instances(InstanceIds=[instance_id])
    typer.secho(f"Stop requested for {instance_id}.", fg=typer.colors.GREEN)


@app.command()
def status(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
) -> None:
    """Show the box's EC2 state and SSM reachability."""
    e, instance_id = _resolve(env)
    session = _session(e)
    resp = session.client("ec2").describe_instances(InstanceIds=[instance_id])
    state = "unknown"
    for res in resp.get("Reservations", []):
        for inst in res.get("Instances", []):
            state = inst.get("State", {}).get("Name", "unknown")
    ssm_state = "not registered"
    info = session.client("ssm").describe_instance_information(
        Filters=[{"Key": "InstanceIds", "Values": [instance_id]}]
    ).get("InstanceInformationList", [])
    if info:
        ssm_state = f"ssm {info[0].get('PingStatus', 'unknown').lower()}"
    typer.echo(f"{instance_id}: {state} ({ssm_state})")
