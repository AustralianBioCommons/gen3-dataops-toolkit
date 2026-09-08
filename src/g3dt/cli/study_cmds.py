"""``g3dt study`` — manage the env's study registry (SSM-backed, 4.1.0).

The registry defines exactly what ``g3dt metadata upload`` sends to a
commons, so this group is deliberately careful: writes validate before
touching anything, ``repoint`` proves every target prefix is uploadable
before writing a single record, and every mutating command carries the
typed production gate. Reads are forgiving (case-insensitive, legacy
key forms accepted); writes are strict (canonical lowercase names only).

Storage: one JSON parameter per study at ``/{project}/{env}/studies/<name>``
(core: :mod:`g3dt.studies`; design doc docs/design/studies.md). The legacy
``s3://<metadata-bucket>/config/studies.yaml`` is read-only fallback until
5.0 — ``g3dt study migrate`` imports it and retires the file.
"""
from __future__ import annotations

from typing import Optional

import typer

from g3dt import config, studies
from g3dt.cli._internal import resolve, safety
from g3dt.cli._internal.helptext import ENV_OPT

app = typer.Typer(
    no_args_is_help=True,
    help="Manage the env's study registry (what metadata upload acts on).\n\n"
    "One SSM parameter per study under /{project}/{env}/studies/ — the "
    "environment lives in the path, so staging and prod can never "
    "cross-resolve. Typical release flow: `g3dt study repoint --latest` "
    "then `g3dt metadata upload`.",
)

_PATH_HELP = (
    "s3:// prefix holding this study's release JSONs, e.g. "
    "s3://<gold-bucket>/release_jsons/v1.2.0/mystudy/."
)
# Shared with the `g3dt config study-set` alias so both spellings read alike.
SET_PROJECT_ID_HELP = "New Gen3 project_id."
SET_PROGRAM_ID_HELP = "New Gen3 program."
SET_PATH_HELP = (
    "New S3 location of the study's release JSONs (the prefix `metadata "
    "upload` reads from): " + _PATH_HELP + " Checked for DataImportOrder.txt "
    "and node JSONs before anything is written."
)
SET_DRY_RUN_HELP = "Show field: old -> new (and the path check); write nothing."
SET_NO_VERIFY_HELP = (
    "Skip the S3 path check — e.g. registering a release that is not "
    "exported yet."
)


def _usage(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(2)


def _fail(msg: str) -> None:
    typer.secho(msg, fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _registry(env: str):
    """Fetch ``(registry, source, used_fallback)`` with guided failures."""
    try:
        return studies.registry_for_env(env)
    except config.ConfigError as exc:
        _fail(str(exc))
    except Exception as exc:  # botocore auth failures — guided, never a traceback
        _fail(resolve._aws_error_message(exc, env))


def _require_ssm_registry(env: str):
    """The registry, refusing to mutate while the legacy S3 file is live.

    Writing to SSM while the fallback registry is still serving would
    SHADOW the whole legacy file at once (SSM wins as soon as it is
    non-empty) — an easy way to silently drop studies. Migrate first.
    """
    registry, source, used_fallback = _registry(env)
    if used_fallback:
        _fail(
            f"The study registry for this env is still the legacy "
            f"{source}. Run `g3dt study migrate` first — editing via the "
            f"CLI would shadow the legacy file and silently hide its other "
            f"studies."
        )
    return registry, source


def _resolve_in(registry, name: str, env: str, source: str):
    """Case/suffix-forgiving lookup inside an already-fetched registry."""
    base = config.env_base(env)
    for cand in studies.lookup_candidates(name, base):
        sc = registry.get(cand)
        if sc:
            return sc
    _fail(studies.not_found_message(name, env, registry, source))


def list_impl(env: Optional[str]) -> None:
    """Shared by ``g3dt study list`` and the ``g3dt config studies`` alias."""
    env = resolve.active_env(env)
    registry, source, used_fallback = _registry(env)
    if used_fallback:
        studies.warn_s3_fallback_once(source)
    typer.secho(f"registry: {source}", fg=typer.colors.BRIGHT_BLACK, err=True)
    if not registry:
        typer.secho(
            "No studies registered. Add one with `g3dt study add <name> "
            "--project-id <ProjectId> --program-id <program> --path "
            "s3://...`, or import a legacy studies.yaml with "
            "`g3dt study migrate`.",
            fg=typer.colors.YELLOW,
            err=True,
        )
        return
    for name in sorted(registry):
        typer.echo(name)


@app.command("list")
def list_cmd(
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
) -> None:
    """List the studies registered for the environment (bare names, stdout)."""
    list_impl(env)


@app.command()
def show(
    name: str = typer.Argument(..., help="Study name, e.g. mystudy."),
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
) -> None:
    """Show one study's record plus a liveness check of its metadata path."""
    env = resolve.active_env(env)
    rc, session = resolve.rc_session_of(env)
    registry, source, used_fallback = _registry(env)
    if used_fallback:
        studies.warn_s3_fallback_once(source)
    sc = _resolve_in(registry, name, env, source)
    if sc.key != name:
        typer.secho(
            f"note: resolved '{name}' -> '{sc.key}' (study names are "
            f"lowercase).",
            fg=typer.colors.YELLOW,
            err=True,
        )
    typer.echo(f"name             : {sc.key}")
    typer.echo(f"project_id       : {sc.project_id}")
    typer.echo(f"program_id       : {sc.program_id}")
    typer.echo(f"s3_metadata_path : {sc.s3_metadata_path}")
    typer.secho(f"source           : {source}", fg=typer.colors.BRIGHT_BLACK, err=True)
    try:
        n = studies.validate_upload_prefix(sc.s3_metadata_path, session)
        typer.echo(f"liveness         : PASS (DataImportOrder.txt + {n} node JSONs)")
    except config.ConfigError as exc:
        typer.secho(f"liveness         : FAIL — {exc}", fg=typer.colors.YELLOW)


@app.command()
def add(
    name: str = typer.Argument(..., help="New study name (lowercase, e.g. mystudy)."),
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
    project_id: str = typer.Option(
        ...,
        "--project-id",
        help="Gen3 project_id (mixed case is fine here, e.g. 'MyStudy-X').",
    ),
    program_id: str = typer.Option(
        ..., "--program-id", help="Gen3 program the project belongs to."
    ),
    path: str = typer.Option(..., "--path", help=_PATH_HELP),
) -> None:
    """Register a new study in the environment.

    The path is not checked against S3 here (release prefixes are often
    created later) — ``study show`` and ``study repoint`` validate it.

    Example:
      g3dt study add cdah --project-id CAUGHT-CAD --program-id program1 \\
          --path s3://my-gold/release_jsons/v2.0.0/cdah/ --env staging

    Targeting production requires typing the context/env name to confirm.
    """
    env = resolve.active_env(env)
    try:
        studies.validate_new_name(name)
    except config.ConfigError as exc:
        _usage(str(exc))
    if not path.startswith("s3://"):
        _usage(f"--path must be an s3:// URI (got '{path}').")
    _require_ssm_registry(env)
    safety.confirm_prod_strict("study add", env)
    rc, session = resolve.rc_session_of(env)
    try:
        studies.put_study(
            rc,
            session,
            name,
            project_id=project_id,
            program_id=program_id,
            s3_metadata_path=path,
            overwrite=False,
        )
    except config.ConfigError as exc:
        _fail(str(exc))
    typer.secho(
        f"Added study '{name}' -> /{rc.project}/{rc.env}/studies/{name}",
        fg=typer.colors.GREEN,
    )


def set_impl(
    name: str,
    env: Optional[str],
    project_id: Optional[str],
    program_id: Optional[str],
    path: Optional[str],
    *,
    dry_run: bool = False,
    verify: bool = True,
) -> None:
    """Shared by ``g3dt study set`` and the ``g3dt config study-set`` alias.

    Order of operations mirrors ``repoint``: resolve, check the new path in
    S3, print the diff, then (unless dry-run) gate production and write.
    """
    if project_id is None and program_id is None and path is None:
        _usage(
            "Nothing to set — pass at least one of --project-id / "
            "--program-id / --path."
        )
    for flag, value in (
        ("--project-id", project_id),
        ("--program-id", program_id),
        ("--path", path),
    ):
        if value is not None and not value.strip():
            _usage(f"{flag} cannot be empty.")
    env = resolve.active_env(env)
    if path is not None and not path.startswith("s3://"):
        _usage(f"--path must be an s3:// URI (got '{path}').")
    registry, source = _require_ssm_registry(env)
    sc = _resolve_in(registry, name, env, source)
    new = {
        "project_id": project_id if project_id is not None else sc.project_id,
        "program_id": program_id if program_id is not None else sc.program_id,
        "s3_metadata_path": path if path is not None else sc.s3_metadata_path,
    }
    old = {
        "project_id": sc.project_id,
        "program_id": sc.program_id,
        "s3_metadata_path": sc.s3_metadata_path,
    }
    if new == old:
        typer.secho("No changes.", fg=typer.colors.YELLOW)
        return
    rc, session = resolve.rc_session_of(env)
    if new["s3_metadata_path"] != old["s3_metadata_path"]:
        if verify:
            try:
                count = studies.validate_upload_prefix(
                    new["s3_metadata_path"], session
                )
            except config.ConfigError as exc:
                typer.secho(
                    "Path check failed — nothing was written:",
                    fg=typer.colors.RED, err=True,
                )
                typer.secho(f"  - {exc}", fg=typer.colors.RED, err=True)
                typer.secho(
                    "Pass --no-verify to record the path anyway (e.g. before "
                    "the release is exported).",
                    fg=typer.colors.YELLOW, err=True,
                )
                raise typer.Exit(1)
            typer.echo(
                f"liveness : PASS (DataImportOrder.txt + {count} node JSONs)"
            )
        else:
            typer.secho(
                "liveness : SKIPPED (--no-verify)", fg=typer.colors.YELLOW
            )
    for field in studies.FIELDS:
        if new[field] != old[field]:
            typer.echo(f"{field}: {old[field]} -> {new[field]}")
    if dry_run:
        typer.secho(
            f"Dry run — study '{sc.key}' not updated.", fg=typer.colors.YELLOW
        )
        return
    safety.confirm_prod_strict("study set", env)
    try:
        studies.put_study(rc, session, sc.key, overwrite=True, **new)
    except config.ConfigError as exc:
        _fail(str(exc))
    typer.secho(f"Updated study '{sc.key}'.", fg=typer.colors.GREEN)


@app.command("set")
def set_cmd(
    name: str = typer.Argument(..., help="Study name (see `g3dt study list`)."),
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
    project_id: Optional[str] = typer.Option(
        None, "--project-id", help=SET_PROJECT_ID_HELP
    ),
    program_id: Optional[str] = typer.Option(
        None, "--program-id", help=SET_PROGRAM_ID_HELP
    ),
    path: Optional[str] = typer.Option(None, "--path", help=SET_PATH_HELP),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-d", help=SET_DRY_RUN_HELP
    ),
    no_verify: bool = typer.Option(False, "--no-verify", help=SET_NO_VERIFY_HELP),
) -> None:
    """Update a study's Gen3 ids or its S3 release location.

    The record is read, merged and written back atomically; only the fields
    you pass change. A new --path is checked in S3 first (DataImportOrder.txt
    plus node JSONs — the exact checks `metadata upload` makes), so a typo
    cannot point an upload at nothing. To move EVERY study to a new release
    at once use `g3dt study repoint` instead; `g3dt config study-set` is an
    alias of this command.

    Examples:
      g3dt study set cdah --path s3://my-gold/release_jsons/v2.1.0/cdah/ --dry-run
      g3dt study set cdah --path s3://my-gold/release_jsons/v2.1.0/cdah/
      g3dt study set cdah --program-id program2 --env staging

    Targeting production requires typing the context/env name to confirm.
    """
    set_impl(
        name, env, project_id, program_id, path,
        dry_run=dry_run, verify=not no_verify,
    )


@app.command()
def remove(
    name: str = typer.Argument(..., help="Study name (see `g3dt study list`)."),
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
    yes: bool = typer.Option(
        False, "--yes", "-y", help="Skip the y/N prompt (never the prod gate)."
    ),
) -> None:
    """Unregister a study (registry entry only — no data is touched)."""
    env = resolve.active_env(env)
    registry, source = _require_ssm_registry(env)
    sc = _resolve_in(registry, name, env, source)
    # confirm_destructive types the context name on prod (--yes never
    # bypasses that) and is a plain y/N elsewhere — same gate as
    # `g3dt delete metadata`.
    safety.confirm_destructive("Remove study", sc.key, env, yes)
    rc, session = resolve.rc_session_of(env)
    try:
        studies.delete_study(rc, session, sc.key)
    except config.ConfigError as exc:
        _fail(str(exc))
    typer.secho(
        f"Removed study '{sc.key}' from /{rc.project}/{rc.env}/studies "
        f"(its data and any uploads are untouched).",
        fg=typer.colors.GREEN,
    )


@app.command()
def repoint(
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
    release: Optional[str] = typer.Option(
        None, "--release", help="Target release tag, e.g. 1.2.0 or v1.2.0."
    ),
    latest: bool = typer.Option(
        False,
        "--latest",
        help="Use the newest release_tag from the env's releases ledger.",
    ),
    studies_opt: Optional[str] = typer.Option(
        None,
        "--studies",
        "-s",
        help="Comma-separated subset (default: every registered study).",
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-d", help="Show the diff; write nothing."
    ),
) -> None:
    """Point studies' metadata paths at a release — the release-cutover step.

    Pass exactly one of --release <tag> or --latest. Every target prefix is
    validated (DataImportOrder.txt + node JSONs — the exact checks upload
    performs) BEFORE anything is written: one bad target means nothing
    changes. To upload one release without moving the registry, use
    `g3dt metadata upload --release <tag>` instead.

    Examples:
      g3dt study repoint --latest --env staging --dry-run
      g3dt study repoint --release v2.1.0 --env staging
      g3dt study repoint --release v2.1.0 --studies cdah,edcad --env staging

    Targeting production requires typing the context/env name to confirm.
    """
    if bool(release) == latest:
        _usage("Pass exactly one of --release <tag> or --latest.")
    env = resolve.active_env(env)
    rc, session = resolve.rc_session_of(env)
    registry, source = _require_ssm_registry(env)
    if not registry:
        _fail(
            f"No studies registered under {source} — add them with "
            f"`g3dt study add` or import a legacy registry with "
            f"`g3dt study migrate`."
        )
    if latest:
        marker = config.load_marker()
        profile = (
            None if env.endswith("_ec2") else config.aws_profile_for(env, marker)
        )
        tag = studies.latest_release_tag(rc, profile)
        if not tag:
            db = rc.get("release/db", "<release-db>")
            table = rc.get("release/table", "releases")
            _fail(
                f"No releases recorded in {db}.{table} — cut a data release "
                f"first, or pass --release <tag>."
            )
        typer.secho(f"latest release: {tag}", fg=typer.colors.BRIGHT_BLACK, err=True)
    else:
        try:
            tag = studies.normalise_release_tag(release)
        except config.ConfigError as exc:
            _usage(str(exc))

    if studies_opt:
        names = []
        for raw in studies_opt.split(","):
            raw = raw.strip()
            if not raw:
                continue
            sc = _resolve_in(registry, raw, env, source)
            if sc.key not in names:
                names.append(sc.key)
    else:
        names = sorted(registry)

    changes = []  # (name, old_path, new_path)
    errors = []
    for n in names:
        sc = registry[n]
        try:
            new_path = studies.replace_version_segment(sc.s3_metadata_path, tag)
        except config.ConfigError as exc:
            errors.append(f"{n}: {exc}")
            continue
        changes.append((n, sc, new_path))
    to_write = [(n, sc, p) for n, sc, p in changes if p != sc.s3_metadata_path]
    for n, sc, p in to_write:
        try:
            studies.validate_upload_prefix(p, session)
        except config.ConfigError as exc:
            errors.append(f"{n}: {exc}")
    if errors:
        typer.secho(
            "Repoint validation failed — nothing was written:",
            fg=typer.colors.RED,
            err=True,
        )
        for e in errors:
            typer.secho(f"  - {e}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    for n, sc, p in changes:
        if p == sc.s3_metadata_path:
            typer.echo(f"{n}: unchanged ({p})")
        else:
            typer.echo(f"{n}: {sc.s3_metadata_path} -> {p}")
    if not to_write:
        typer.secho(
            f"Nothing to repoint — every path already points at v{tag}.",
            fg=typer.colors.YELLOW,
        )
        return
    if dry_run:
        typer.secho(
            f"Dry run — {len(to_write)} of {len(names)} studies would be "
            f"repointed to v{tag}.",
            fg=typer.colors.YELLOW,
        )
        return
    safety.confirm_prod_strict("study repoint", env)
    for n, sc, p in to_write:
        try:
            studies.put_study(
                rc,
                session,
                n,
                project_id=sc.project_id,
                program_id=sc.program_id,
                s3_metadata_path=p,
                overwrite=True,
            )
        except config.ConfigError as exc:
            _fail(f"{n}: {exc}")
    typer.secho(
        f"Repointed {len(to_write)} of {len(names)} studies to v{tag}.",
        fg=typer.colors.GREEN,
    )


@app.command()
def migrate(
    env: Optional[str] = typer.Option(None, "--env", "-e", help=ENV_OPT),
    force: bool = typer.Option(
        False, "--force", help="Overwrite SSM records that differ from the file."
    ),
    dry_run: bool = typer.Option(
        False, "--dry-run", "-d", help="Show the plan; write and rename nothing."
    ),
) -> None:
    """Import the legacy S3 studies.yaml into SSM, then retire the file.

    Idempotent: entries already in SSM and identical are skipped; a rerun
    after success reports "already migrated". Differing records are refused
    without --force. The file is renamed to studies.yaml.migrated only after
    every write is re-read and verified. The legacy file is a read-only
    fallback until 5.0; after that only SSM is read.

    Targeting production requires typing the context/env name to confirm.
    """
    env = resolve.active_env(env)
    rc, session = resolve.rc_session_of(env)
    try:
        legacy = studies.load_legacy_yaml(session, rc.metadata_bucket)
    except Exception as exc:
        _fail(resolve._aws_error_message(exc, env))
    if legacy is None:
        if studies.legacy_migrated_exists(session, rc.metadata_bucket):
            typer.secho(
                f"Already migrated — s3://{rc.metadata_bucket}/"
                f"{studies.MIGRATED_S3_KEY} exists and no studies.yaml "
                f"remains. Nothing to do.",
                fg=typer.colors.GREEN,
            )
            return
        typer.secho(
            f"No legacy studies.yaml at s3://{rc.metadata_bucket}/"
            f"{config.STUDIES_S3_KEY} — nothing to migrate. Register studies "
            f"with `g3dt study add`.",
            fg=typer.colors.YELLOW,
        )
        return

    base = config.env_base(env)
    plan = []
    problems = []
    for key in sorted(legacy):
        entry = legacy[key] or {}
        name = studies.strip_env_suffix(str(key).lower(), base)
        if any(name.endswith(s) for s in config._STUDY_ENV_SUFFIXES):
            typer.secho(
                f"warning: '{key}' looks suffixed for a different environment "
                f"— importing verbatim as '{name}'.",
                fg=typer.colors.YELLOW,
                err=True,
            )
        entry_problems = []
        if not studies.NAME_RE.match(name):
            entry_problems.append(
                f"invalid name '{name}' (lowercase letters, digits, "
                f"underscores, letter first)"
            )
        missing = [f for f in studies.FIELDS if not entry.get(f)]
        if missing:
            entry_problems.append(f"missing {', '.join(missing)}")
        if entry_problems:
            problems.append(f"'{key}': {'; '.join(entry_problems)}")
        else:
            plan.append((name, entry))
    if problems:
        typer.secho(
            "Legacy studies.yaml has malformed entries — nothing was written:",
            fg=typer.colors.RED,
            err=True,
        )
        for p in problems:
            typer.secho(f"  - {p}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1)

    existing = studies.studies_from_rc(rc)
    to_import, unchanged, conflicts = [], [], []
    for name, entry in plan:
        cur = existing.get(name)
        if cur is None:
            to_import.append((name, entry))
        elif (cur.project_id, cur.program_id, cur.s3_metadata_path) == (
            entry["project_id"],
            entry["program_id"],
            entry["s3_metadata_path"],
        ):
            unchanged.append(name)
        else:
            conflicts.append((name, cur, entry))
    if conflicts and not force:
        typer.secho(
            "These studies already exist in SSM with DIFFERENT values — "
            "nothing was written:",
            fg=typer.colors.RED,
            err=True,
        )
        for name, cur, entry in conflicts:
            for field in studies.FIELDS:
                if getattr(cur, field) != entry[field]:
                    typer.secho(
                        f"  {name}.{field}: ssm='{getattr(cur, field)}' "
                        f"file='{entry[field]}'",
                        fg=typer.colors.RED,
                        err=True,
                    )
        typer.secho(
            "Rerun with `g3dt study migrate --force` to overwrite the SSM "
            "records with the file's values.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)

    writes = to_import + [(name, entry) for name, _cur, entry in conflicts]
    for name, _entry in to_import:
        typer.echo(f"import   : {name}")
    for name, _cur, _entry in conflicts:
        typer.echo(f"overwrite: {name}")
    for name in unchanged:
        typer.echo(f"unchanged: {name}")
    if dry_run:
        typer.secho(
            "Dry run — nothing written, studies.yaml left in place.",
            fg=typer.colors.YELLOW,
        )
        return

    safety.confirm_prod_strict("study migrate", env)
    for name, entry in writes:
        try:
            studies.put_study(
                rc,
                session,
                name,
                project_id=entry["project_id"],
                program_id=entry["program_id"],
                s3_metadata_path=entry["s3_metadata_path"],
                overwrite=True,
            )
        except config.ConfigError as exc:
            _fail(f"{name}: {exc}")

    # Verify every record before retiring the file — a half-applied migrate
    # must leave the legacy registry in place so nothing stops resolving.
    from g3dt import resolver

    resolver.resolve.cache_clear()
    marker = config.load_marker()
    profile = None if env.endswith("_ec2") else config.aws_profile_for(base, marker)
    rc2 = resolver.resolve(rc.project, base, profile=profile)
    now = studies.studies_from_rc(rc2)
    expected = {name: entry for name, entry in writes}
    bad = []
    for name, entry in expected.items():
        cur = now.get(name)
        if cur is None or (cur.project_id, cur.program_id, cur.s3_metadata_path) != (
            entry["project_id"],
            entry["program_id"],
            entry["s3_metadata_path"],
        ):
            bad.append(name)
    bad += [n for n in unchanged if n not in now]
    if bad:
        _fail(
            f"Post-write verification failed for: {', '.join(sorted(bad))} — "
            f"studies.yaml left in place. Investigate, then rerun "
            f"`g3dt study migrate`."
        )
    studies.rename_legacy(session, rc.metadata_bucket)
    typer.secho(
        f"Migrated: {len(to_import)} imported, {len(conflicts)} overwritten, "
        f"{len(unchanged)} unchanged. Renamed s3://{rc.metadata_bucket}/"
        f"{config.STUDIES_S3_KEY} -> {studies.MIGRATED_S3_KEY}.",
        fg=typer.colors.GREEN,
    )
