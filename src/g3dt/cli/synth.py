"""`g3dt synth` — synthetic data lifecycle for any configured environment.

Generation uses **gen3-metadata-simulator** (schema-valid). It runs locally:
synthetic metadata is generated on the laptop (writing under
``~/.g3dt/synth_metadata/<version>/<study>/``) and uploaded/deleted from
there, so there is nothing to run on EC2.

Every command accepts ``--env``; targeting a **production** environment (any env
whose name contains ``prod``) shows a warning and requires typing the env name to
confirm — it cannot be bypassed.

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
) -> None:
    """Full end-to-end synthetic deploy (dict + LLM-generate + upload + restarts).

    Wraps services/synthetic_data/full_deploy_dd_and_synth.sh (LLM-backed
    generation). Provider/model and the restart targets come from the env's
    SSM tree unless overridden; the API key path comes from
    --llm-api-key-file or the marker.
    """
    e = env_of(env)
    safety.confirm_prod_strict("synthetic full deploy", env)
    env_vars = script_env(e)
    env_vars.update(_llm_env_overrides(e, llm_provider, llm_model, llm_api_key_file))
    if restart_services:
        env_vars["G3DT_RESTART_SERVICES"] = restart_services
    if etl_cronjob:
        env_vars["G3DT_ETL_CRONJOB"] = etl_cronjob
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
      g3dt synth generate AusDiab_Simulated -n 5 --seed 1
      g3dt synth generate AusDiab_Simulated --llm -n 5
      g3dt synth generate "AusDiab_Simulated,Baker-Biobank_Simulated" -n "30,60"
    """
    e = env_of(env)
    safety.confirm_prod_strict("synthetic generation", env)

    # A comma list of per-study counts must line up with the studies given.
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
