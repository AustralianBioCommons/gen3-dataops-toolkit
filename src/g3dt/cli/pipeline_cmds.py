"""`g3dt pipeline` — watch the dbt CodePipeline / CodeBuild, mirroring `g3dt jobs`.

One way to watch any long thing: `g3dt jobs` tails EC2-dispatched runs;
`g3dt pipeline` does the same for the dbt pipelines. Pipeline and CodeBuild
project names resolve from the env's SSM tree, so the operator never types an
ARN or a console name.
"""
from __future__ import annotations

import time
from typing import Optional

import typer

from g3dt import config
from g3dt.upload.metadata_submitter import create_boto3_session

app = typer.Typer(no_args_is_help=True, help="Watch the dbt CodePipeline/CodeBuild.")

#: CodeBuild build statuses that mean the build is over.
_TERMINAL_BUILD_STATUSES = ("SUCCEEDED", "FAILED", "FAULT", "STOPPED", "TIMED_OUT")


def _resolved(env: str):
    """Return ``(rc, session)`` for the env (names from SSM, auth from marker)."""
    from g3dt import resolver

    marker = config.load_marker()
    project = config.require_project(marker)
    base = config.env_base(env)
    profile = None if env.endswith("_ec2") else config.aws_profile_for(base, marker)
    rc = resolver.resolve(project, base, profile=profile)
    session = create_boto3_session(aws_profile=profile, aws_region=rc.region)
    return rc, session


@app.command()
def status(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    which: str = typer.Option(
        "writeReleaseInfo",
        "--which",
        help="writeReleaseInfo | dbtTestAndRun",
    ),
) -> None:
    """Show the latest execution state per stage of the pipeline."""
    try:
        rc, session = _resolved(env)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    pipeline_name = rc.get(f"codepipeline/{which}")
    if not pipeline_name:
        typer.secho(
            f"No SSM parameter codepipeline/{which} — valid values: "
            f"writeReleaseInfo, dbtTestAndRun.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)
    state = session.client("codepipeline").get_pipeline_state(name=pipeline_name)
    typer.secho(f"{pipeline_name}", bold=True)
    for stage in state.get("stageStates", []):
        latest = stage.get("latestExecution", {})
        typer.echo(f"  {stage['stageName']:<32} {latest.get('status', 'n/a')}")


@app.command()
def logs(
    env: str = typer.Option(..., "--env", "-e", help="Environment, e.g. test."),
    which: str = typer.Option(
        "dbtReleaseBuilder",
        "--which",
        help="dbtReleaseBuilder | dbtTestAndRun",
    ),
    follow: bool = typer.Option(False, "--follow", "-f", help="Tail until the build ends."),
    poll_seconds: int = typer.Option(5, "--poll-seconds", help="Follow poll interval."),
) -> None:
    """Print (and optionally follow) the latest build's CloudWatch output.

    The log group is CodeBuild's default ``/aws/codebuild/<project>``; the
    project name comes from SSM ``codebuild/*``. The follow loop is the same
    filter_log_events + de-dup pattern as `g3dt jobs logs`.
    """
    try:
        rc, session = _resolved(env)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    project_name = rc.get(f"codebuild/{which}")
    if not project_name:
        typer.secho(
            f"No SSM parameter codebuild/{which} — valid values: "
            f"dbtReleaseBuilder, dbtTestAndRun.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(2)

    cb = session.client("codebuild")
    build_ids = cb.list_builds_for_project(
        projectName=project_name, sortOrder="DESCENDING"
    ).get("ids", [])
    if not build_ids:
        typer.secho(f"No builds yet for {project_name}.", fg=typer.colors.YELLOW)
        return
    build_id = build_ids[0]
    # CodeBuild names the stream <project>/<build-uuid> in /aws/codebuild/<project>.
    stream_prefix = build_id.split(":", 1)[1] if ":" in build_id else build_id
    group = f"/aws/codebuild/{project_name}"
    logs_client = session.client("logs")

    def build_status() -> Optional[str]:
        builds = cb.batch_get_builds(ids=[build_id]).get("builds", [])
        return builds[0].get("buildStatus") if builds else None

    typer.secho(f"{build_id}  ({group})", bold=True)
    seen_ids: set = set()
    last_ts = 0

    def drain() -> None:
        nonlocal last_ts
        kwargs = {
            "logGroupName": group,
            "logStreamNamePrefix": stream_prefix,
            "startTime": last_ts,
        }
        events = []
        try:
            while True:
                resp = logs_client.filter_log_events(**kwargs)
                events.extend(resp.get("events", []))
                token = resp.get("nextToken")
                if not token:
                    break
                kwargs["nextToken"] = token
        except logs_client.exceptions.ResourceNotFoundException:
            return
        for ev in sorted(events, key=lambda e: e["timestamp"]):
            if ev["eventId"] in seen_ids:
                continue
            seen_ids.add(ev["eventId"])
            last_ts = max(last_ts, ev["timestamp"])
            typer.echo(ev["message"].rstrip("\n"))

    while True:
        drain()
        if not follow:
            break
        current = build_status()
        if current in _TERMINAL_BUILD_STATUSES:
            drain()  # catch events ingested between the last drain and completion
            typer.secho(f"\n[build finished: {current}]", fg=typer.colors.BRIGHT_BLACK)
            break
        time.sleep(poll_seconds)
