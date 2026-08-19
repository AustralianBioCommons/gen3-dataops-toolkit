"""`g3dt synth` — synthetic data lifecycle for any configured environment.

Generation uses **gen3-metadata-simulator** (schema-valid). It runs locally:
synthetic metadata is generated on the laptop (writing under
``~/.g3dt/synth_metadata/<version>/<study>/``) and uploaded/deleted from
there, so there is nothing to run on EC2.

Every command accepts ``--env``; targeting a **production** environment (any env
whose name contains ``prod``) shows a warning and requires typing the env name to
confirm — it cannot be bypassed.

Studies and record counts are **batch inputs**, passed per command
(``generate STUDIES -n N``, ``deploy --studies ... -n ...``) — they are not
environment facts, so they never come from SSM. The ``deploy`` defaults are
the original ACDC demo set, kept for continuity; other projects pass their
own.

Generation defaults to keyless ``random`` data (no API calls). Pass ``--llm``
for LLM-realistic values. The LLM provider and model come from the
environment's SSM tree (the CDK config's optional ``llm`` block, published as
``app/llm_provider`` / ``app/llm_model``) and can be overridden per run with
``--llm-provider`` / ``--llm-model``. Only the API key stays local: point at
its file once with ``g3dt config set llm_api_key_file <path>`` (or per run
with ``--llm-api-key-file``); the vendor env var ``ANTHROPIC_API_KEY`` /
``OPENAI_API_KEY`` also works. The old ``~/.g3dt/.env`` is no longer read.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from g3dt import config
from g3dt.config import (
    dictionary_filename,
    dictionary_url,
    dictionary_version_of,
    script_env,
)
from g3dt.cli._internal import runner, safety
from g3dt.cli._internal.resolve import env_of
from g3dt.cli.dict_cmds import SCHEMA_DIR, warn_if_overridden

app = typer.Typer(
    no_args_is_help=True,
    help="Synthetic data lifecycle (local generation; prod requires typed confirmation).",
)

SYNTH_DIR = Path("~/.g3dt/synth_metadata").expanduser()

#: Defaults for the full-deploy flow, kept for continuity with the original
#: ACDC demo environment. Any other project should pass --studies (and
#: --num-records) — there is no SSM fact for these because simulated study
#: sets are batch inputs, not environment facts.
DEPLOY_DEFAULT_STUDIES = (
    "AusDiab_Simulated,Baker-Biobank_Simulated,"
    "BioHeart-CT_Simulated,CAUGHT-CAD_Simulated"
)
DEPLOY_DEFAULT_NUM_RECORDS = "30,60,20,55"
DEPLOY_DEFAULT_PREV_VERSION = "v1.0.0"


def _check_per_study_counts(studies: str, num_records: Optional[str]) -> None:
    """Exit 1 when a per-study count list does not line up with the studies."""
    if num_records and "," in num_records:
        n_counts = len(num_records.split(","))
        n_studies = len(studies.split(","))
        if n_counts != n_studies:
            typer.secho(
                f"--num-records has {n_counts} values but {n_studies} studies "
                f"were given (pass one count, or one per study).",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)


def _llm_env_overrides(
    e,
    llm_provider: Optional[str],
    llm_model: Optional[str],
    llm_api_key_file: Optional[Path],
) -> dict:
    """Resolve the effective LLM settings for a run: flags > SSM > default.

    Returns env-var overrides for the generator script, which forwards
    provider/model to gen3-metadata-simulator as CLI flags (the simulator's
    own precedence puts flags first). Exits with guidance when no model is
    configured anywhere; the key-file path is optional — the simulator falls
    back to the vendor env var and raises its own error if neither exists.
    """
    effective_provider = llm_provider or e.llm_provider
    effective_model = llm_model or e.llm_model
    if not effective_model:
        typer.secho(
            "No LLM model configured. Set the llm block in the CDK config "
            "(published to SSM as app/llm_model) and redeploy, or pass "
            "--llm-model for this run.",
            fg=typer.colors.RED,
            err=True,
        )
        raise typer.Exit(1)
    overrides = {
        "G3DT_LLM_PROVIDER": effective_provider,
        "G3DT_LLM_MODEL": effective_model,
    }
    key_file = (
        str(Path(llm_api_key_file).expanduser())
        if llm_api_key_file
        else config.llm_api_key_file()
    )
    if key_file:
        overrides["LLM_API_KEY_FILE"] = key_file
    return overrides

#: Written into each generated batch so `synth upload` can tell which dictionary
#: produced it. A batch is only valid against that dictionary, and the directory
#: name alone cannot be trusted: --schema and --version are separate options, so
#: the label is exactly the thing that can be wrong.
PROVENANCE_FILE = ".g3dt-provenance.json"


def _write_provenance(batch_dir: Path, e, ver: str, schema_path: str) -> None:
    """Record which dictionary produced this batch."""
    batch_dir.mkdir(parents=True, exist_ok=True)
    (batch_dir / PROVENANCE_FILE).write_text(
        json.dumps(
            {
                "dictionary_version": ver,
                "dictionary_url": dictionary_url(e, ver),
                "schema_file": Path(schema_path).name,
                "generated_for_env": e.name,
            },
            indent=2,
        )
        + "\n"
    )


def _check_batch_matches_env(batch_dir: Path, e, ver: str, allow_mismatch: bool) -> None:
    """Refuse to upload a batch generated against a different dictionary.

    Synthetic records are only schema-valid against the dictionary version used
    to generate them, so pushing a v1.0.0 batch into an environment running
    v1.1.0 produces data Gen3 may reject or, worse, silently accept as wrong.
    The environment's own version is the authority here.

    A batch with no provenance file predates this check and cannot be attributed
    after the fact, so it warns rather than blocks.
    """
    marker = batch_dir / PROVENANCE_FILE
    if not marker.is_file():
        typer.secho(
            f"No provenance in {batch_dir} — cannot confirm which dictionary "
            f"generated it. Proceeding; regenerate the batch to record it.",
            fg=typer.colors.YELLOW,
        )
        return
    batch_version = json.loads(marker.read_text()).get("dictionary_version")
    if batch_version == ver:
        return
    if allow_mismatch:
        typer.secho(
            f"Version mismatch allowed by flag: batch was generated against "
            f"{batch_version}, uploading to {e.name} which runs {ver}.",
            fg=typer.colors.YELLOW,
        )
        return
    typer.secho(
        f"Refusing to upload: this batch was generated against dictionary "
        f"{batch_version}, but '{e.name}' runs {ver}. Synthetic data is only "
        f"valid against the dictionary that produced it.\n"
        f"Either regenerate for {ver} (g3dt synth generate ... --version {ver}), "
        f"deploy {batch_version} to '{e.name}' first "
        f"(g3dt dict deploy --env {e.name} --version {batch_version}), "
        f"or override with --allow-version-mismatch.",
        fg=typer.colors.RED,
        err=True,
    )
    raise typer.Exit(1)


@app.command()
def deploy(
    env: str = typer.Option(
        "test", "--env", "-e", help="Target environment (prod requires typed confirmation)."
    ),
    llm_provider: Optional[str] = typer.Option(
        None, "--llm-provider",
        help="LLM vendor override (anthropic|openai); default: the env's SSM app/llm_provider.",
    ),
    llm_model: Optional[str] = typer.Option(
        None, "--llm-model",
        help="LLM model override; default: the env's SSM app/llm_model.",
    ),
    llm_api_key_file: Optional[Path] = typer.Option(
        None, "--llm-api-key-file", exists=True, dir_okay=False,
        help="Path to the file holding the LLM API key; default: the marker's "
        "llm_api_key_file (set once: g3dt config set llm_api_key_file <path>).",
    ),
    restart_services: Optional[str] = typer.Option(
        None, "--restart-services",
        help="Comma-separated deployment names restarted during the deploy, in "
        "order; default: the env's SSM app/restart_services.",
    ),
    etl_cronjob: Optional[str] = typer.Option(
        None, "--etl-cronjob",
        help="ETL cronjob name; default: the env's SSM app/etl_cronjob.",
    ),
    studies: Optional[str] = typer.Option(
        None, "--studies",
        help="Simulated study id(s), comma-separated. These are (re)generated, "
        "uploaded, and their previous batch deleted. Default: the original "
        f"demo set ({DEPLOY_DEFAULT_STUDIES}).",
    ),
    num_records: Optional[str] = typer.Option(
        None, "--num-records", "-n",
        help="Records per study: one number for all, or a comma list (one per "
        "study). Default: 30 per study (or the classic 30,60,20,55 when "
        "--studies is not given).",
    ),
    prev_version: Optional[str] = typer.Option(
        None, "--prev-version",
        help="Dictionary version whose previously-uploaded synthetic batch is "
        f"deleted before uploading the new one. Default: {DEPLOY_DEFAULT_PREV_VERSION}.",
    ),
    skip_dict: bool = typer.Option(
        False, "--skip-dict",
        help="Skip the dictionary upload and schema restarts (steps 1-2) — "
        "synthetic data only: delete previous batch, generate, upload, run "
        "ETL. Use when the deployed dictionary is already current.",
    ),
    skip_delete: bool = typer.Option(
        False, "--skip-delete",
        help="Skip step 3 (deleting the previous synthetic batch) without "
        "prompting. Without this flag, deletion asks for confirmation; "
        "declining skips it and the flow continues.",
    ),
) -> None:
    """Full end-to-end synthetic deploy: the whole cycle in one command.

    Wraps services/synthetic_data/full_deploy_dd_and_synth.sh, which runs:

    \b
      1. pull the dictionary at the env's version and upload it to S3
      2. restart the schema microservices (env's SSM restart_services order)
      3. delete the PREVIOUS synthetic batch for the given studies
         (--prev-version, so stale records don't linger in the commons)
      4. LLM-generate a new batch for the studies (provider/model from SSM)
      5. upload the new batch to Gen3
      6. run the ETL cronjob (env's SSM etl_cronjob)

    With --skip-dict, steps 1-2 are skipped (the schema is still fetched
    locally if missing — generation validates against it) and the flow is
    synthetic-data only. Equivalent by hand: synth delete + synth generate +
    synth upload + k8s restart-etl.

    Step 3 is destructive, so it asks for confirmation (skip it outright with
    --skip-delete, or decline the prompt — the flow continues either way).
    On a first deploy, when no previous batch exists locally, deletion is
    skipped automatically.

    Provider/model, restart targets, and the ETL cronjob come from the env's
    SSM tree unless overridden; the API key path comes from
    --llm-api-key-file or the marker. Studies and record counts are batch
    inputs with ACDC-era defaults — any other project should pass --studies.

    Examples:
      g3dt synth deploy -e test --studies synthetic_dataset_1 -n 100
      g3dt synth deploy -e test --studies synthetic_dataset_1 -n 100 --skip-dict
      g3dt synth deploy -e test --studies "s1,s2" -n "100,50" --prev-version v1.2.0
    """
    e = env_of(env)
    safety.confirm_prod_strict("synthetic full deploy", env)
    effective_studies = studies or DEPLOY_DEFAULT_STUDIES
    _check_per_study_counts(effective_studies, num_records)
    if not skip_delete:
        # Deleting the previous batch is the one destructive step in the
        # flow; declining just skips it — everything else still runs.
        effective_prev = prev_version or DEPLOY_DEFAULT_PREV_VERSION
        skip_delete = not typer.confirm(
            f"Delete the previous synthetic batch ({effective_studies} @ "
            f"{effective_prev}) from the commons before uploading the new one?",
            default=True,
        )
    env_vars = script_env(e)
    env_vars.update(_llm_env_overrides(e, llm_provider, llm_model, llm_api_key_file))
    if restart_services:
        env_vars["G3DT_RESTART_SERVICES"] = restart_services
    if etl_cronjob:
        env_vars["G3DT_ETL_CRONJOB"] = etl_cronjob
    if studies:
        env_vars["G3DT_SYNTH_STUDIES"] = studies
    if num_records:
        env_vars["G3DT_SYNTH_NUM_RECORDS"] = num_records
    if prev_version:
        env_vars["G3DT_SYNTH_PREV_VERSION"] = prev_version
    if skip_dict:
        env_vars["G3DT_SYNTH_SKIP_DICT"] = "1"
    if skip_delete:
        env_vars["G3DT_SYNTH_SKIP_DELETE"] = "1"
    runner.run(
        runner.bash_script(
            "services/synthetic_data/full_deploy_dd_and_synth.sh", env
        ),
        env=env_vars,
    )


@app.command()
def generate(
    studies: str = typer.Argument(
        ...,
        help="Simulated study id(s); comma-separated for many, e.g. AusDiab_Simulated.",
    ),
    env: str = typer.Option(
        "test", "--env", "-e", help="Target environment (prod requires typed confirmation)."
    ),
    num_records: str = typer.Option(
        None,
        "--num-records",
        "-n",
        help="Records per study: one number for all, or a comma list (one per study).",
    ),
    provider: str = typer.Option(
        "random", "--provider", help="Value strategy: 'random' (default, keyless) or 'llm'."
    ),
    llm: bool = typer.Option(
        False,
        "--llm",
        help="Generate LLM-realistic values; provider/model resolve from the "
        "env's SSM tree (override with --llm-provider/--llm-model). Default is "
        "keyless random data (no API key, no API calls).",
    ),
    llm_provider: Optional[str] = typer.Option(
        None, "--llm-provider",
        help="LLM vendor override (anthropic|openai); default: the env's SSM app/llm_provider.",
    ),
    llm_model: Optional[str] = typer.Option(
        None, "--llm-model",
        help="LLM model override; default: the env's SSM app/llm_model.",
    ),
    llm_api_key_file: Optional[Path] = typer.Option(
        None, "--llm-api-key-file", exists=True, dir_okay=False,
        help="Path to the file holding the LLM API key; default: the marker's "
        "llm_api_key_file (set once: g3dt config set llm_api_key_file <path>).",
    ),
    seed: int = typer.Option(None, "--seed", help="RNG seed for reproducible output."),
    schema: str = typer.Option(
        None, "--schema", help="Gen3 schema path (default: pulled for the version)."
    ),
    version: str = typer.Option(
        None, "--version", help="Version label for output dir (default: env dictionary_version)."
    ),
) -> None:
    """Generate synthetic metadata locally with gen3-metadata-simulator.

    STUDIES is one simulated study id, or several comma-separated. Defaults to
    keyless, schema-valid random data (no API key, no API calls). Pass --llm to
    generate LLM-realistic values instead.

    Examples:
      g3dt synth generate synthetic_dataset_1 -n 5 --seed 1
      g3dt synth generate synthetic_dataset_1 --llm -n 100
      g3dt synth generate "dataset_a,dataset_b" -n "30,60"
      g3dt synth generate dataset_a --llm --llm-model gpt-4o-mini \
          --llm-api-key-file ~/keys/openai_api_key.txt
    """
    e = env_of(env)
    safety.confirm_prod_strict("synthetic generation", env)
    _check_per_study_counts(studies, num_records)

    ver = version or e.dictionary_version
    schema_path = schema or str(SCHEMA_DIR / dictionary_filename(e, ver))

    # A batch is only valid against the dictionary that generated it, and the
    # output directory is named for `ver` -- so an explicit --schema carrying a
    # different version stamp would silently mislabel the whole batch.
    if schema:
        schema_version = dictionary_version_of(Path(schema).name)
        if schema_version and schema_version != ver:
            typer.secho(
                f"--schema is {Path(schema).name} (dictionary {schema_version}) "
                f"but the batch would be labelled {ver}. Synthetic data is only "
                f"valid against the dictionary that produced it — pass "
                f"--version {schema_version}, or drop --schema to use {ver}.",
                fg=typer.colors.RED,
                err=True,
            )
            raise typer.Exit(1)

    # Ensure the schema is available locally; pull it if missing.
    if not Path(schema_path).exists():
        typer.secho(f"Schema not found locally; pulling {ver}...", fg=typer.colors.YELLOW)
        runner.run(
            runner.bash_script(
                "services/dictionary/pull_dict.sh",
                dictionary_url(e, ver),
                dictionary_filename(e, ver),
            ),
            env=script_env(e, ver),
        )

    effective_provider = "llm" if llm else provider
    args = [
        "--schema", schema_path,
        "--version", ver,
        "--provider", effective_provider,
        "--studies", studies,
    ]
    if num_records:
        args += ["--num-records", num_records]
    if seed is not None:
        args += ["--seed", str(seed)]
    env_vars = script_env(e, ver)
    if effective_provider == "llm":
        env_vars.update(
            _llm_env_overrides(e, llm_provider, llm_model, llm_api_key_file)
        )
    runner.run(
        runner.bash_script(
            "services/synthetic_data/generate_synth_metadata.sh", *args
        ),
        env=env_vars,
    )
    # Only after a successful generate: runner.run raises on failure, so a
    # half-written batch is never stamped as valid.
    _write_provenance(SYNTH_DIR / ver, e, ver, schema_path)


@app.command()
def upload(
    env: str = typer.Option(
        "test", "--env", "-e", help="Target environment (prod requires typed confirmation)."
    ),
    version: str = typer.Option(
        None, "--version", help="Dictionary version dir (default: the env's version)."
    ),
    allow_version_mismatch: bool = typer.Option(
        False,
        "--allow-version-mismatch",
        help="Upload even if the batch was generated against another dictionary.",
    ),
) -> None:
    """Upload generated synthetic metadata to Gen3 (reads local files).

    The batch is checked against the dictionary version being uploaded for:
    synthetic records are only schema-valid against the dictionary that produced
    them. Defaults to the env's declared version, so the common case verifies
    against what the environment actually runs.
    """
    e = env_of(env)
    safety.confirm_prod_strict("synthetic metadata upload", env)
    # A promoted dictionary (dict deploy --version) leaves SSM behind, so an
    # explicit --version here is how you say "this env really runs that tag".
    warn_if_overridden(e, version)
    v = version or e.dictionary_version
    _check_batch_matches_env(SYNTH_DIR / v, e, v, allow_version_mismatch)
    base_dir = str(SYNTH_DIR / v) + "/"
    args = ["--base-dir", base_dir, "--aws-secret-name", e.aws_secret_name]
    if e.aws_profile:
        args += ["--aws-profile", e.aws_profile]
    runner.run(
        runner.python_script(
            "services/synthetic_data/upload_synth_metadata_sheepdog.py", *args
        ),
        env=script_env(e, v),
    )


@app.command()
def delete(
    env: str = typer.Option(
        "test", "--env", "-e", help="Target environment (prod requires typed confirmation)."
    ),
    projects: str = typer.Option(
        None, "--projects", "-p", help="Comma-separated simulated project ids."
    ),
    import_order: str = typer.Option(
        None,
        "--import-order",
        help="DataImportOrder.txt path (default: DataImportOrder.txt in the cwd).",
    ),
) -> None:
    """Delete previously-uploaded synthetic metadata from Gen3."""
    e = env_of(env)
    safety.confirm_prod_strict("synthetic metadata deletion", env)
    order = import_order or "DataImportOrder.txt"
    args = ["-i", order, "-s", e.aws_secret_name]
    if e.aws_profile:
        args += ["-profile", e.aws_profile]
    if projects:
        args += ["-p", projects]
    runner.run(
        runner.python_script(
            "services/synthetic_data/delete_synth_metadata_sheepdog.py", *args
        ),
        env=script_env(e),
    )


@app.command(name="install-simulator")
def install_simulator() -> None:
    """Install the gen3-metadata-simulator generator (the 'synth' extra)."""
    import sys

    runner.run([sys.executable, "-m", "pip", "install", "gen3-metadata-simulator"])
