"""`g3dt config` — contexts, discovery, and resolved settings.

A **context** is a named (project, env, profile, region) tuple — the answer to
"what is this command pointed at?" (design: docs/design/contexts.md). The
commands here read as plain English:

    g3dt config discover --all-profiles --add   # find deployed infra, register it
    g3dt config contexts                        # list what I have
    g3dt config use myproj/staging                # point g3dt at one of them
    g3dt config show                            # what will commands actually do?

Everything the toolkit uses at runtime still resolves from SSM
(``/{project}/{env}/...``, published by ``cdk deploy``); the only local file is
the ``g3dt.yaml`` marker, which now stores the contexts. Legacy markers
(``project``/``default_env``/``profiles``) keep working unchanged — contexts
are synthesized from them in memory.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional

import typer

from g3dt import config, contexts
from g3dt.cli._internal import resolve
from g3dt.cli._internal.resolve import env_of, study_of

app = typer.Typer(
    no_args_is_help=True,
    help="Contexts (use/discover/contexts), plus the resolved SSM settings.",
)


def _req_key(rc, key: str) -> str:
    """Return the SSM leaf ``key`` from ``rc``, failing loudly if absent.

    The medallion names (``buckets/silver|gold``, ``glue/db/bronze|silver|gold``)
    are published under these raw-free keys by pipeline deployments >= v2.0.0.
    Before this guard, a missing key propagated as ``None``: dbt-env silently
    dropped the G3DT_DB_* vars and emitted ``s3://None/dbt/`` data dirs, and
    the release search silently fell back to an account-wide catalog walk.
    """
    value = rc.get(key)
    if value is None:
        raise config.ConfigError(
            f"SSM parameter /{rc.project}/{rc.env}/{key} is missing. "
            f"gen3-dataops-toolkit >= 3 reads the raw-free medallion keys "
            f"published by gen3-aws-data-pipeline >= v2.0.0; a pipeline "
            f"deployment older than v2.0.0 still publishes raw-prefixed keys. "
            f"Upgrade the pipeline deployment (or pin gen3-dataops-toolkit<3)."
        )
    return value


# --------------------------------------------------------------------------- #
# Context plumbing helpers                                                     #
# --------------------------------------------------------------------------- #
def _deployed_version(ctx: contexts.Context) -> str:
    """One cheap SSM head per context: '✓ <ver>' / '—' / '?' — never raises."""
    import boto3
    from botocore.config import Config as _BotoConfig

    try:
        session = boto3.Session(profile_name=ctx.profile, region_name=ctx.region)
        client = session.client(
            "ssm",
            config=_BotoConfig(connect_timeout=2, read_timeout=4,
                               retries={"max_attempts": 1}),
        )
        value = client.get_parameter(
            Name=f"/{ctx.project}/{ctx.env}/meta/toolkitVersion"
        )["Parameter"]["Value"]
        return f"✓ {value}"
    except Exception as exc:  # ParameterNotFound / auth / SSO / network
        name = type(exc).__name__
        if "ParameterNotFound" in name or "ParameterNotFound" in str(exc):
            return "—"
        return "?"


def _print_context_row(ctx: contexts.Context, current: Optional[str],
                       deployed: Optional[str]) -> None:
    star = "*" if ctx.name == current else " "
    prod = contexts.is_production(ctx)
    tag = " [PROD]" if prod else ""
    src = " (legacy)" if ctx.source == "legacy" else ""
    line = (f" {star} {ctx.name}{tag}{src}  project={ctx.project} "
            f"env={ctx.env} profile={ctx.profile or '(ambient)'} "
            f"region={ctx.region}")
    if deployed is not None:
        line += f"  deployed={deployed}"
        if deployed == "?" and ctx.profile:
            line += f"  (try: aws sso login --profile {ctx.profile})"
    typer.secho(line, fg=typer.colors.RED if prod else None, bold=prod)


# --------------------------------------------------------------------------- #
# The context commands                                                         #
# --------------------------------------------------------------------------- #
@app.command("contexts")
def contexts_list(
    verify: bool = typer.Option(
        False,
        "--verify/--no-verify",
        help="Check each context's deployed toolkit version (one SSM read "
             "per context; needs live AWS credentials). Default: list the "
             "local marker only, fully offline.",
    ),
) -> None:
    """List the locally registered contexts (current marked with *).

    Reads only the local g3dt.yaml marker — no AWS/SSO calls are made
    unless --verify is passed.
    """
    resolve.announce_context()
    ctxs = contexts.list_contexts()
    if not ctxs:
        typer.secho(
            "No contexts configured. Run 'g3dt config discover <aws-profile> "
            "--add', or 'g3dt config add <name> --project <p> --env <e> "
            "--profile <profile>'.",
            fg=typer.colors.YELLOW,
        )
        return
    current = contexts.current_context_name()
    if verify:
        from g3dt.cli._internal.aws_quiet import quiet_botocore

        with quiet_botocore():
            for ctx in ctxs.values():
                _print_context_row(ctx, current, _deployed_version(ctx))
    else:
        for ctx in ctxs.values():
            _print_context_row(ctx, current, None)


@app.command("current")
def current_cmd() -> None:
    """Print the current context name (script-friendly; exit 1 if none)."""
    resolve.announce_context()
    name = contexts.current_context_name()
    if name is None:
        typer.secho(
            "No context selected — run 'g3dt config use <name>' "
            "(see 'g3dt config contexts').",
            fg=typer.colors.YELLOW,
            err=True,
        )
        raise typer.Exit(1)
    typer.echo(name)


@app.command("use")
def use(
    name: str = typer.Argument(..., help="Context name, e.g. myproj/staging."),
) -> None:
    """Point g3dt at a context: every following command acts there.

    Switching to a production-classified context warns loudly and asks for
    confirmation. Legacy markers are migrated in place on first use (all
    legacy keys are preserved).
    """
    resolve.announce_context()
    ctxs = contexts.list_contexts()
    target = ctxs.get(name)
    if target is None:
        typer.secho(
            f"Unknown context '{name}'. Configured: "
            f"{', '.join(ctxs) or '(none)'}. "
            f"Run `g3dt config contexts` or `g3dt config discover --add`.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)
    if contexts.is_production(target):
        typer.secho(
            f"\n  You are switching to a PRODUCTION context: {name}\n"
            f"  project={target.project} env={target.env} "
            f"profile={target.profile or '(ambient)'}\n"
            f"  Every following command acts on production until you switch "
            f"away.\n",
            fg=typer.colors.RED, bold=True, err=True,
        )
        if not typer.confirm("Switch to this production context?", default=False):
            typer.secho("Aborted — context unchanged.", fg=typer.colors.YELLOW)
            raise typer.Exit(1)
    try:
        path = config.set_current_context(name)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho(f"Now using context {name} ({path})", fg=typer.colors.GREEN)


@app.command("add")
def add(
    name: str = typer.Argument(..., help="Context name, e.g. myproj/staging."),
    project: str = typer.Option(..., "--project", help="Project id, e.g. myproj."),
    env: str = typer.Option(..., "--env", help="Base environment, e.g. staging."),
    profile: str = typer.Option(
        None, "--profile", help="AWS named profile (omit for the ambient chain)."
    ),
    region: str = typer.Option(None, "--region", help="AWS region override."),
    production: bool = typer.Option(
        False, "--production",
        help="Mark as production: strict typed confirmation on every "
             "destructive action.",
    ),
) -> None:
    """Register one context by hand (`config discover --add` is the usual path)."""
    resolve.announce_context()
    marker = config.load_marker()
    ctx = contexts.Context(
        name=name, project=project, env=config.env_base(env), profile=profile,
        region=region or marker.get("region") or config.DEFAULT_REGION,
        production=production or None,
    )
    try:
        contexts._validate_context(name, {"project": project, "env": ctx.env})
        path, added, skipped = config.upsert_contexts([ctx])
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    if skipped:
        typer.secho(
            f"Context '{name}' already exists — not overwritten. "
            f"`g3dt config forget {name}` first if you mean to replace it.",
            fg=typer.colors.YELLOW,
        )
        raise typer.Exit(1)
    typer.secho(f"Added context {name} ({path})", fg=typer.colors.GREEN)


@app.command("forget")
def forget(
    name: str = typer.Argument(..., help="Context name to forget."),
    force: bool = typer.Option(
        False, "--force", help="Allow forgetting the current context."
    ),
) -> None:
    """Remove a context from the local marker. Nothing in AWS is touched."""
    resolve.announce_context()
    if name == contexts.current_context_name() and not force:
        typer.secho(
            f"'{name}' is the current context. Switch away first "
            f"(`g3dt config use <other>`) or pass --force.",
            fg=typer.colors.RED, err=True,
        )
        raise typer.Exit(1)
    try:
        path = config.forget_context(name)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho(f"Forgot context {name} ({path})", fg=typer.colors.GREEN)


@app.command("discover")
def discover(
    profile_arg: str = typer.Argument(
        None, metavar="[PROFILE]",
        help="Scan just this AWS profile's account. If its SSO session is "
             "stale you are offered `aws sso login` first.",
    ),
    all_profiles: bool = typer.Option(
        False, "--all-profiles",
        help="Scan every profile in ~/.aws/config for deployed "
             "/{project}/{env} trees (profiles with stale sessions are "
             "skipped, not logged in).",
    ),
    add_found: bool = typer.Option(
        False, "--add", help="Register discovered pairs as contexts."
    ),
    profile: List[str] = typer.Option(
        [], "--profile", help="Restrict --all-profiles to these profiles."
    ),
    region: str = typer.Option(
        None, "--region", help="Region for profiles that configure none."
    ),
) -> None:
    """Find deployable infrastructure.

    With a PROFILE argument: log that one profile in if needed, then list
    every deployed environment its account holds — the recommended flow.
    With --all-profiles: sweep every configured profile (stale sessions are
    skipped). With neither: verify each already-configured context against
    its own account. Add --add to register findings as contexts.
    """
    resolve.announce_context()
    # The scan probes credentials that may be stale; botocore logs its own
    # WARNING tracebacks for failed SSO refreshes, which would drown the
    # per-profile "skipped (run: aws sso login ...)" lines this command
    # prints deliberately. Quiet botocore for the duration.
    import logging

    logging.getLogger("botocore").setLevel(logging.ERROR)
    marker = config.load_marker()
    default_region = region or marker.get("region") or config.DEFAULT_REGION

    if profile_arg and all_profiles:
        typer.secho("Pass either a PROFILE argument or --all-profiles, not both.",
                    fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    if not profile_arg and not all_profiles:
        ctxs = contexts.list_contexts(marker)
        if not ctxs:
            typer.secho(
                "No contexts configured to verify. Try "
                "`g3dt config discover <aws-profile> --add`.",
                fg=typer.colors.YELLOW,
            )
            return
        for ctx in ctxs.values():
            state = _deployed_version(ctx)
            if state.startswith("✓"):
                label, color = f"OK ({state[2:]})", typer.colors.GREEN
            elif state == "—":
                label, color = "NOT DEPLOYED", typer.colors.YELLOW
            else:
                label, color = (
                    f"AUTH FAILED (try: aws sso login --profile {ctx.profile})",
                    typer.colors.RED,
                )
            typer.secho(f"  {ctx.name:<24} {label}", fg=color)
        return

    import botocore.session

    profiles_cfg = botocore.session.Session().full_config.get("profiles", {})

    if profile_arg:
        if profile_arg not in profiles_cfg:
            typer.secho(
                f"No profile '{profile_arg}' in ~/.aws/config. Configured: "
                f"{', '.join(sorted(profiles_cfg)) or '(none)'}.",
                fg=typer.colors.RED, err=True,
            )
            raise typer.Exit(1)
        if not _profile_session_valid(profile_arg):
            typer.secho(
                f"The SSO session for '{profile_arg}' is not valid.",
                fg=typer.colors.YELLOW,
            )
            if not typer.confirm(
                f"Run `aws sso login --profile {profile_arg}` now?",
                default=True,
            ):
                typer.secho("Aborted — log in and re-run discover.",
                            fg=typer.colors.YELLOW)
                raise typer.Exit(1)
            import subprocess

            login = subprocess.run(
                ["aws", "sso", "login", "--profile", profile_arg]
            )
            if login.returncode != 0 or not _profile_session_valid(profile_arg):
                typer.secho(
                    f"Still no valid session for '{profile_arg}'. Aborting.",
                    fg=typer.colors.RED, err=True,
                )
                raise typer.Exit(1)
        names = [profile_arg]
    else:
        names = profile or sorted(profiles_cfg)
        if not names:
            typer.secho("No AWS profiles found in ~/.aws/config.",
                        fg=typer.colors.YELLOW)
            return

    found: List[contexts.Context] = []
    seen_names = set()
    for prof in names:
        prof_region = (profiles_cfg.get(prof, {}).get("region")
                       or default_region)
        try:
            pairs = _scan_profile(prof, prof_region)
        except Exception as exc:
            typer.secho(
                f"  {prof}: skipped ({type(exc).__name__} — "
                f"try: aws sso login --profile {prof})",
                fg=typer.colors.YELLOW,
            )
            continue
        if not pairs:
            typer.secho(f"  {prof}: no deployed environments", err=False)
            continue
        for project, env in pairs:
            name = f"{project}/{env}"
            if name in seen_names:
                # Same account visible through several profiles (e.g. a
                # 'default' alias): the first profile wins; don't print
                # duplicate suggestions.
                continue
            seen_names.add(name)
            ctx = contexts.Context(
                name=name, project=project, env=env, profile=prof,
                region=prof_region,
            )
            found.append(ctx)
            prod = contexts.is_production(ctx)
            typer.secho(
                f"  {prof}: found {name}" + (" [PROD]" if prod else ""),
                fg=typer.colors.RED if prod else typer.colors.GREEN,
                bold=prod,
            )

    if not found:
        return
    if add_found:
        path, added, skipped = config.upsert_contexts(found)
        typer.secho(
            f"Registered {len(added)} context(s) in {path}"
            + (f"; kept existing: {', '.join(skipped)}" if skipped else ""),
            fg=typer.colors.GREEN,
        )
        typer.echo("Select one with: g3dt config use <name>")
    else:
        typer.echo("\nRegister these with --add, or individually:")
        for ctx in found:
            typer.echo(
                f"  g3dt config add {ctx.name} --project {ctx.project} "
                f"--env {ctx.env} --profile {ctx.profile}"
            )


def _profile_session_valid(profile: str) -> bool:
    """Cheap credential check: can this profile sign a request right now?"""
    import boto3
    from botocore.config import Config as _BotoConfig

    try:
        session = boto3.Session(profile_name=profile)
        session.client(
            "sts",
            config=_BotoConfig(connect_timeout=3, read_timeout=5,
                               retries={"max_attempts": 1}),
        ).get_caller_identity()
        return True
    except Exception:
        return False


def _scan_profile(profile: str, region: str) -> List[tuple]:
    """All deployed (project, env) pairs visible to one profile's account.

    One filtered DescribeParameters walk: every deployed environment publishes
    exactly one ``/{project}/{env}/meta/toolkitVersion`` leaf.
    """
    import boto3
    from botocore.config import Config as _BotoConfig

    session = boto3.Session(profile_name=profile, region_name=region)
    client = session.client(
        "ssm",
        config=_BotoConfig(connect_timeout=3, read_timeout=8,
                           retries={"max_attempts": 1}),
    )
    pairs = []
    paginator = client.get_paginator("describe_parameters")
    for page in paginator.paginate(
        ParameterFilters=[{
            "Key": "Name", "Option": "Contains",
            "Values": ["/meta/toolkitVersion"],
        }]
    ):
        for param in page["Parameters"]:
            parts = param["Name"].split("/")  # ['', project, env, 'meta', ...]
            if len(parts) >= 5 and parts[3] == "meta":
                pairs.append((parts[1], parts[2]))
    return sorted(set(pairs))


# --------------------------------------------------------------------------- #
# Environment / study introspection (context-aware)                            #
# --------------------------------------------------------------------------- #
@app.command()
def envs() -> None:
    """List the environments with a deployed SSM tree for this project."""
    resolve.announce_context()
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
    if env is not None:
        env = resolve.active_env(env)
    else:
        resolve.announce_context()
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
    env: str = typer.Option(
        None, "--env", "-e",
        help="Environment; defaults to the current context.",
    ),
    study: str = typer.Option(
        None, "--study", "-s", help="Optional study to resolve against the env."
    ),
    full: bool = typer.Option(
        False, "--full", help="Also dump the raw SSM subtree (every parameter)."
    ),
) -> None:
    """Print fully-resolved settings for the current context (or --env).

    Use this before any job to confirm the exact names the tooling will use —
    the staging-vs-prod safety check. Everything shown is read live from the
    env's SSM tree; nothing is local except the marker's contexts.
    """
    env = resolve.active_env(env)
    e = env_of(env)
    typer.secho(f"Environment: {e.name}", bold=True)
    typer.echo(f"  is_ec2             : {e.is_ec2}")
    typer.echo(f"  region             : {e.region}")
    typer.echo(f"  aws_profile        : {e.aws_profile or '(ambient credentials)'}")
    typer.echo(f"  aws_secret_name    : {e.aws_secret_name}")
    typer.echo(f"  dictionary_version : {e.dictionary_version}")
    # The composed URL, not the three parts: "which dictionary will this env
    # actually fetch?" now spans schema_repo plus two optional inputs, so showing
    # the resolved result is what makes the answer checkable before a deploy.
    typer.echo(f"  dictionary_url     : {config.dictionary_url(e)}")
    typer.echo(f"  schema_s3_uri      : {e.schema_s3_uri}")
    typer.echo(f"  schema_repo        : {e.schema_repo}")
    typer.echo(f"  domain             : {e.domain}")
    typer.echo(f"  app_name           : {e.app_name}")
    typer.echo(f"  namespace          : {e.namespace}")
    typer.echo(f"  cluster_name       : {e.cluster_name}")
    typer.echo(f"  ec2_instance_id    : {e.ec2_instance_id}")
    # Synthetic-data LLM facts: provider/model from SSM (the CDK's optional
    # llm block); only the key *path* is local, from the marker.
    typer.echo(f"  llm_provider       : {e.llm_provider}")
    typer.echo(f"  llm_model          : {e.llm_model or '(not set — pass --llm-model or add the llm block to the CDK config)'}")
    typer.echo(f"  llm_api_key_file   : {config.llm_api_key_file() or '(not set — g3dt synth set-key <path>)'}")
    # k8s restart targets: from SSM (the CDK's optional k8s block), restarted
    # in the listed order by restart-schema/restart-ms/dict deploy/synth deploy.
    typer.echo(f"  restart_services   : {e.restart_services}")
    typer.echo(f"  etl_cronjob        : {e.etl_cronjob}")
    if study:
        s = study_of(study, env)
        typer.secho(f"Study: {study} -> {s.key}", bold=True)
        typer.echo(f"  project_id       : {s.project_id}")
        typer.echo(f"  program_id       : {s.program_id}")
        typer.echo(f"  s3_metadata_path : {s.s3_metadata_path}")
    if full:
        rc = resolve.rc_of(env)
        typer.secho(
            f"\n/{rc.project}/{rc.env}  ({len(rc.params)} parameters)", bold=True
        )
        for key in sorted(rc.params):
            typer.echo(f"  {key:<32} {rc.params[key]}")


@app.command()
def diff(
    env: str = typer.Option(
        None, "--env", "-e",
        help="Environment; defaults to the current context.",
    ),
    file: Path = typer.Option(
        ...,
        "--file",
        "-f",
        help="The env's INPUT file in the deployment wrapper, e.g. "
        "../myproj-pipeline-deploy/config/myproj.staging.json.",
    ),
) -> None:
    """Flag drift between SSM and the committed CDK INPUT file.

    Compares the mirrored app facts (``app/*``) and the toolkit pin
    (``meta/toolkitVersion``) in SSM against ``config/<project>.<env>.json``.
    A difference means "someone edited the JSON but didn't `cdk deploy`" (or
    vice-versa). Exits 1 on drift, so it can gate CI.
    """
    env = resolve.active_env(env)
    rc = resolve.rc_of(env)
    project = rc.project

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

    # Optional inputs: compared only when the file defines them — an absent
    # input legitimately publishes no SSM parameter (or, for the dictionary
    # fields, predates their addition), so absence on both sides is not drift.
    for camel, leaf in {
        "dictionaryBaseUrl": "dictionary_base_url",
        "dictionaryPath": "dictionary_path",
    }.items():
        if camel in gen3:
            check(f"gen3.{camel}", gen3.get(camel), rc.get(f"app/{leaf}"))
    llm = inputs.get("llm") or {}
    for camel, leaf in {"provider": "llm_provider", "model": "llm_model"}.items():
        if camel in llm:
            check(f"llm.{camel}", llm.get(camel), rc.get(f"app/{leaf}"))
    k8s = inputs.get("k8s") or {}
    if "schemaRestartServices" in k8s:
        # The CDK publishes the list comma-joined; compare the same shape.
        check(
            "k8s.schemaRestartServices",
            ",".join(k8s.get("schemaRestartServices") or []),
            rc.get("app/restart_services"),
        )
    if "etlCronjob" in k8s:
        check("k8s.etlCronjob", k8s.get("etlCronjob"), rc.get("app/etl_cronjob"))

    if not drift:
        typer.secho(
            f"No drift: SSM /{project}/{config.env_base(env)} matches {file}.",
            fg=typer.colors.GREEN,
        )
    raise typer.Exit(1 if drift else 0)


@app.command("dbt-env")
def dbt_env(
    env: str = typer.Option(
        None, "--env", "-e",
        help="Environment; defaults to the current context. CodeBuild always "
             "passes this explicitly.",
    ),
) -> None:
    """Emit `export` lines for the env's dbt settings (resolved from SSM).

    The dbt template's profiles.yml / dbt_project.yml read their derived names
    from env_var(); this command is the one source of those values, for both
    CodeBuild and a laptop:

        eval "$(g3dt config dbt-env --env test)" && dbt build

    The context banner goes to stderr, so stdout stays eval-clean.
    """
    import shlex

    env = resolve.active_env(env)
    try:
        rc = resolve.rc_of(env)
        silver_db = _req_key(rc, "glue/db/silver")
        gold_db = _req_key(rc, "glue/db/gold")
        silver_bucket = _req_key(rc, "buckets/silver")
        gold_bucket = _req_key(rc, "buckets/gold")
        bronze_db = _req_key(rc, "glue/db/bronze")
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    profile = (None if env.endswith("_ec2")
               else config.aws_profile_for(env, config.load_marker()))

    values = {
        "G3DT_REGION": rc.region,
        "G3DT_ATHENA_WORKGROUP": rc.athena_workgroup,
        "G3DT_ATHENA_OUTPUT": rc.athena_output_location,
        "G3DT_DB_BRONZE": bronze_db,
        "G3DT_DB_SILVER": silver_db,
        "G3DT_DB_GOLD": gold_db,
        "G3DT_S3_SILVER_DATA_DIR": f"s3://{silver_bucket}/dbt/",
        "G3DT_S3_GOLD_DATA_DIR": f"s3://{gold_bucket}/dbt/",
        # CI isolation: the dbt template's `ci` target builds into these
        # instead — same grammar as the CDK's ci_ databases, same buckets
        # under a dbt_ci/ prefix. Real names above are never prefixed.
        # Bronze has no CI variants: bronze is ingest-only (never written
        # by dbt), so only its real database name is exported.
        "G3DT_DB_SILVER_CI": f"ci_{silver_db}",
        "G3DT_DB_GOLD_CI": f"ci_{gold_db}",
        "G3DT_S3_SILVER_DATA_DIR_CI": f"s3://{silver_bucket}/dbt_ci/",
        "G3DT_S3_GOLD_DATA_DIR_CI": f"s3://{gold_bucket}/dbt_ci/",
    }
    if profile:
        # A named profile means a laptop run: select the dbt target that
        # carries aws_profile_name (CodeBuild/EC2 stay on `default`, ambient).
        values["G3DT_AWS_PROFILE"] = profile
        values["G3DT_DBT_TARGET"] = "local"
    for key, value in values.items():
        if value is not None:
            typer.echo(f"export {key}={shlex.quote(str(value))}")


@app.command("set", hidden=True)
def set_value(
    key: str = typer.Argument(..., help="Legacy bootstrap key."),
    value: str = typer.Argument(..., help="New value."),
) -> None:
    """(Deprecated) Set one legacy bootstrap key in the local marker.

    Contexts replaced this: `g3dt config use <name>` selects where commands
    act, `g3dt config add`/`discover --add` register contexts, and the synth
    LLM key path moved to `g3dt synth set-key <path>`. This command remains
    for back-compat with the legacy project/region/default_env keys only.
    """
    resolve.announce_context()
    typer.secho(
        "DEPRECATED: `g3dt config set` is superseded by contexts — see "
        "`g3dt config use` / `g3dt config add` / `g3dt synth set-key`.",
        fg=typer.colors.YELLOW, err=True,
    )
    try:
        old, new, path = config.set_marker_value(key, value)
    except config.ConfigError as exc:
        typer.secho(str(exc), fg=typer.colors.RED, err=True)
        raise typer.Exit(1)
    typer.secho(f"Updated {key}: {old} -> {new} ({path})", fg=typer.colors.GREEN)
