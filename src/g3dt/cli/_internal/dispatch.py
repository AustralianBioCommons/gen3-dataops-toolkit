"""Hybrid local/EC2 execution for long-running data-plane commands.

Short, interactive steps run locally (see :mod:`runner`). Long jobs (metadata
upload/delete, synth upload/delete, indexd register) can instead run on the
project's per-env EC2 job box via ``--on ec2``. The mechanism is **AWS SSM Run
Command**, which is disconnect-safe by construction: the SSM agent executes the
job server-side, so the laptop can sleep and you re-attach by ``run_id``.

Everything is resolved from the env's own SSM tree (``/{project}/{env}/...``):
the target instance (``ec2/instanceId``), the S3 log destination
(``ec2/logBucket`` + ``ec2/logPrefix``), and the CloudWatch log group
(``ec2/logGroup``). Dispatching to another environment's box is structurally
impossible — there is nothing local to misconfigure.

Authentication split (the crux):
    * the laptop authenticates the SSM call with the *local* env's named
      profile (from the marker's ``profiles:`` map);
    * the job on the box runs under the ``*_ec2`` pseudo-env, which uses the
      ambient instance profile.

The box needs no repository, git credentials, or poetry: CDK user-data
pip-installs the pinned toolkit, so the remote command is a bare ``g3dt ...``
console-script invocation.
"""
from __future__ import annotations

import datetime
import shlex
import time
from enum import Enum
from typing import Callable, List, Optional, Sequence, Tuple

import typer
from botocore.exceptions import ClientError

from g3dt.config import (
    ConfigError,
    EnvConfig,
    aws_profile_for,
    env_base,
    load_marker,
    require_project,
    resolve_env,
    script_env,
)
from g3dt.upload.metadata_submitter import create_boto3_session
from g3dt.cli._internal import registry, runner


class Target(str, Enum):
    """Where a command should execute."""

    local = "local"
    ec2 = "ec2"


def new_run_id(label: str) -> str:
    """Build a sortable, human-friendly run id: ``20260615T1430-metadata-upload``."""
    ts = datetime.datetime.now().strftime("%Y%m%dT%H%M%S")
    return f"{ts}-{label}"


def resolve_dispatch_envs(env: str) -> Tuple[EnvConfig, EnvConfig]:
    """Return ``(auth_env, remote_env)`` for an EC2 dispatch.

    ``auth_env`` is the non-ec2 form used to authenticate the SSM call from the
    laptop (named profile from the marker); ``remote_env`` is the ``*_ec2``
    variant the remote job runs under (ambient instance profile). Both resolve
    the same SSM tree, so they can never disagree on names. Accepts either the
    base or the ``_ec2`` form for ``env``.
    """
    base = env_base(env)
    return resolve_env(base), resolve_env(f"{base}_ec2")


def build_remote_command(run_id: str, remote_argv: Sequence[str]) -> str:
    """Build the shell snippet SSM runs on the EC2 box.

    ``remote_argv`` is a ``g3dt`` CLI argv (e.g. ``["metadata", "upload", ...]``)
    — the box has the toolkit pip-installed by CDK user-data, so no repo clone,
    ``git pull``, or poetry is involved. Output tees to an on-box archive under
    ``~/.g3dt/logs/`` and to stdout, which SSM forwards to CloudWatch Logs for
    live ``g3dt jobs logs --follow``. PYTHONUNBUFFERED keeps the stream
    line-by-line; ``set -o pipefail`` keeps the job's exit status flowing
    through ``tee`` so failures still mark the invocation Failed.
    """
    inner = " ".join(shlex.quote(str(a)) for a in remote_argv)
    return " && ".join(
        [
            "set -euo pipefail",
            "mkdir -p ~/.g3dt/logs",
            f"PYTHONUNBUFFERED=1 g3dt {inner} 2>&1 | tee ~/.g3dt/logs/{run_id}.log",
        ]
    )


def dispatch_ssm(
    auth_env: EnvConfig,
    remote_env: EnvConfig,
    remote_argv: Sequence[str],
    label: str,
) -> str:
    """Launch a ``g3dt`` argv on the env's EC2 box via SSM Run Command."""
    from g3dt import resolver

    if not remote_env.ec2_instance_id:
        typer.secho(
            f"No ec2/instanceId published for env '{env_base(remote_env.name)}'. "
            f"Has the ec2-job-runner stack been deployed? "
            f"(cdk deploy in gen3-aws-data-pipeline)",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    # Log destinations come from the same SSM tree as the instance id.
    rc = resolver.resolve(
        require_project(), env_base(remote_env.name), profile=auth_env.aws_profile
    )
    log_bucket, log_prefix = rc.ec2_log_bucket, rc.ec2_log_prefix
    log_group = rc.ec2_log_group

    session = create_boto3_session(
        aws_profile=auth_env.aws_profile, aws_region=auth_env.region
    )
    ssm = session.client("ssm")
    run_id = new_run_id(label)
    # SSM AWS-RunShellScript executes as root, whose PATH and HOME differ from
    # the operator user's. Run the job inside a login shell for the box's user
    # so ~, PATH (incl. the pip console script), and /etc/profile.d/g3dt.sh
    # resolve.
    remote_user = remote_env.ssh_user or "ec2-user"
    command = "runuser -l {user} -c {script}".format(
        user=remote_user,
        script=shlex.quote(build_remote_command(run_id, remote_argv)),
    )

    resp = ssm.send_command(
        InstanceIds=[remote_env.ec2_instance_id],
        DocumentName="AWS-RunShellScript",
        Parameters={"commands": [command], "executionTimeout": ["172800"]},
        OutputS3BucketName=log_bucket,
        OutputS3KeyPrefix=f"{log_prefix}/{run_id}",
        CloudWatchOutputConfig={
            "CloudWatchLogGroupName": log_group,
            "CloudWatchOutputEnabled": True,
        },
        Comment=f"g3dt {run_id}",
    )
    command_id = resp["Command"]["CommandId"]
    s3_log_uri = f"s3://{log_bucket}/{log_prefix}/{run_id}"
    registry.record(
        run_id,
        command_id=command_id,
        instance_id=remote_env.ec2_instance_id,
        env=remote_env.name,
        argv=list(remote_argv),
        mechanism="ssm",
        s3_log_uri=s3_log_uri,
        cw_log_group=log_group,
        started_at=run_id.split("-", 1)[0],
    )
    _print_dispatch_banner(run_id, remote_env)
    return run_id


def run_or_dispatch(
    on: Target,
    env: str,
    script_relpath: str,
    build_args: Callable[[str], Sequence[str]],
    label: str,
    *,
    interpreter: str = "python",
    remote_cli: Optional[Callable[[str], Sequence[str]]] = None,
) -> None:
    """Run a packaged service script locally, or dispatch the CLI form to EC2.

    ``build_args(env_name)`` returns the service-script arguments for the given
    ``--env`` value; locally we run ``<interpreter> <packaged script> <args>``
    with the resolved env exported as ``G3DT_*`` variables.

    ``remote_cli(env_name)`` returns the equivalent ``g3dt`` subcommand argv
    for the ``*_ec2`` env — on the box the CLI re-enters this function with
    ``on=local`` and runs the same packaged script there.

    ``interpreter`` is ``"python"`` (run with the venv interpreter) or ``"bash"``.
    """
    try:
        if on == Target.local:
            local_env = resolve_env(env)
            args = build_args(local_env.name)
            build = runner.bash_script if interpreter == "bash" else runner.python_script
            runner.run(build(script_relpath, *args), env=script_env(local_env))
            return
        if remote_cli is None:
            raise ConfigError(f"{label} does not support --on ec2.")
        auth_env, remote_env = resolve_dispatch_envs(env)
    except ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    dispatch_ssm(auth_env, remote_env, list(remote_cli(remote_env.name)), label)


# --------------------------------------------------------------------------- #
# Watching dispatched runs (g3dt jobs)                                         #
# --------------------------------------------------------------------------- #
def _auth_session_for_run(rec: dict):
    """Authenticate follow-up calls (status/logs/stop) for a recorded run.

    Uses the marker's profile for the run's base env; region from the marker
    (no SSM read needed just to poll an invocation).
    """
    marker = load_marker()
    return create_boto3_session(
        aws_profile=aws_profile_for(rec["env"], marker),
        aws_region=marker["region"],
    )


def status(run_id: str) -> dict:
    """Return the SSM invocation status for a dispatched run."""
    rec = registry.get(run_id)
    if not rec:
        typer.secho(f"Unknown run id: {run_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if not rec.get("command_id"):
        typer.secho(
            f"Run {run_id} has no SSM command id; check it on the box.",
            fg=typer.colors.YELLOW,
        )
        return rec
    ssm = _auth_session_for_run(rec).client("ssm")
    return ssm.get_command_invocation(
        CommandId=rec["command_id"], InstanceId=rec["instance_id"]
    )


def status_label(rec: dict) -> str:
    """Best-effort current status for the all-runs listing (never raises).

    Unlike :func:`status`, this is keyed off the registry record and stays quiet
    so it can be called in a loop.
    """
    if not rec.get("command_id"):
        return "n/a"
    ssm = _auth_session_for_run(rec).client("ssm")
    try:
        inv = ssm.get_command_invocation(
            CommandId=rec["command_id"], InstanceId=rec["instance_id"]
        )
        return inv.get("Status", "unknown")
    except ssm.exceptions.InvocationDoesNotExist:
        return "pending"  # sent but not yet registered, or aged out of SSM history
    except ClientError:
        return "unknown"


def stop(run_id: str) -> None:
    """Cancel a running SSM-dispatched job.

    Asks the SSM agent to terminate the in-flight command on the box.
    Cancellation isn't instantaneous and isn't guaranteed by AWS, but in
    practice it stops the process; ``g3dt jobs status <run_id>`` shows
    ``Cancelled`` once it takes effect.
    """
    rec = registry.get(run_id)
    if not rec:
        typer.secho(f"Unknown run id: {run_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if not rec.get("command_id"):
        typer.secho(
            f"Run {run_id} has no SSM command id and can't be stopped remotely.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(1)
    current = status_label(rec)
    if current in _TERMINAL_STATUSES:
        typer.secho(f"Run {run_id} already finished: {current}", fg=typer.colors.YELLOW)
        return
    ssm = _auth_session_for_run(rec).client("ssm")
    try:
        ssm.cancel_command(
            CommandId=rec["command_id"], InstanceIds=[rec["instance_id"]]
        )
    except ClientError as exc:
        typer.secho(f"Could not stop {run_id}: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho(
        f"Stop requested for {run_id} ({rec['instance_id']}).", fg=typer.colors.GREEN
    )


def _read_s3_logs(rec: dict) -> str:
    """Best-effort fetch of the full stdout/stderr SSM wrote to S3."""
    uri = rec.get("s3_log_uri") or ""
    if not uri.startswith("s3://"):
        return ""
    bucket, _, prefix = uri[len("s3://"):].partition("/")
    s3 = _auth_session_for_run(rec).client("s3")
    listing = s3.list_objects_v2(Bucket=bucket, Prefix=prefix)
    chunks: List[str] = []
    for obj in listing.get("Contents", []):
        key = obj["Key"]
        if key.endswith(("stdout", "stderr")):
            body = s3.get_object(Bucket=bucket, Key=key)["Body"].read()
            chunks.append(body.decode("utf-8", errors="replace"))
    return "\n".join(chunks)


#: SSM invocation states that mean the run is over.
_TERMINAL_STATUSES = ("Success", "Failed", "Cancelled", "TimedOut")


def _read_cw_logs(rec: dict, start_time: int = 0) -> List[dict]:
    """Fetch CloudWatch Logs events for a run's SSM stdout/stderr streams.

    SSM names the streams ``<command-id>/<instance-id>/<plugin>/{stdout,stderr}``,
    so the ``<command-id>/<instance-id>`` prefix captures both. ``start_time``
    (epoch millis) lets the follow loop pull only newer events; callers de-dup
    by ``eventId``. Returns ``[]`` if the group/stream doesn't exist yet (run
    just started, or the instance role lacks CloudWatch Logs write permission).
    """
    group = rec.get("cw_log_group")
    command_id = rec.get("command_id")
    instance_id = rec.get("instance_id")
    if not (group and command_id and instance_id):
        return []
    client = _auth_session_for_run(rec).client("logs")
    kwargs = {
        "logGroupName": group,
        "logStreamNamePrefix": f"{command_id}/{instance_id}",
        "startTime": start_time,
    }
    events: List[dict] = []
    try:
        while True:
            resp = client.filter_log_events(**kwargs)
            events.extend(resp.get("events", []))
            token = resp.get("nextToken")
            if not token:
                break
            kwargs["nextToken"] = token
    except client.exceptions.ResourceNotFoundException:
        return []
    return events


def _stream_cw_logs(run_id: str, rec: dict, *, follow: bool, poll_seconds: int) -> None:
    """Print a run's CloudWatch output, optionally following until it finishes."""
    seen_ids: set = set()
    last_ts = 0

    def drain() -> None:
        nonlocal last_ts
        for ev in sorted(_read_cw_logs(rec, last_ts), key=lambda e: e["timestamp"]):
            if ev["eventId"] in seen_ids:
                continue
            seen_ids.add(ev["eventId"])
            last_ts = max(last_ts, ev["timestamp"])
            typer.echo(ev["message"])

    while True:
        drain()
        if not follow:
            break
        inv = status(run_id)
        if isinstance(inv, dict) and inv.get("Status") in _TERMINAL_STATUSES:
            drain()  # catch events ingested between the last drain and completion
            typer.secho(f"\n[run {run_id} finished: {inv['Status']}]",
                        fg=typer.colors.BRIGHT_BLACK)
            break
        time.sleep(poll_seconds)


def logs(run_id: str, follow: bool = False, poll_seconds: int = 5) -> None:
    """Print (and optionally follow) the logs for a dispatched run."""
    rec = registry.get(run_id)
    if not rec:
        typer.secho(f"Unknown run id: {run_id}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    # Runs dispatched with CloudWatch output stream live; S3 output is only
    # uploaded on completion, so it is the fallback.
    if rec.get("cw_log_group"):
        _stream_cw_logs(run_id, rec, follow=follow, poll_seconds=poll_seconds)
        return

    seen = 0
    while True:
        text = _read_s3_logs(rec)
        if len(text) > seen:
            typer.echo(text[seen:], nl=False)
            seen = len(text)
        if not follow:
            break
        inv = status(run_id)
        if isinstance(inv, dict) and inv.get("Status") in _TERMINAL_STATUSES:
            typer.secho(f"\n[run {run_id} finished: {inv['Status']}]",
                        fg=typer.colors.BRIGHT_BLACK)
            break
        time.sleep(poll_seconds)


def _print_dispatch_banner(run_id: str, remote_env: EnvConfig) -> None:
    typer.secho(
        f"Dispatched to EC2 ({remote_env.ec2_instance_id} / {remote_env.name})",
        fg=typer.colors.GREEN,
    )
    typer.echo(f"  run id : {run_id}")
    typer.echo(f"  logs   : g3dt jobs logs {run_id} --follow")
    typer.echo(f"  status : g3dt jobs status {run_id}")


__all__ = [
    "Target",
    "run_or_dispatch",
    "dispatch_ssm",
    "resolve_dispatch_envs",
    "build_remote_command",
    "new_run_id",
    "status",
    "status_label",
    "stop",
    "logs",
]
