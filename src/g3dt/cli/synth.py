"""`g3dt synth` — synthetic data lifecycle for any configured environment.

Generation uses **gen3-metadata-simulator** (schema-valid). It runs locally:
synthetic metadata is generated on the laptop (writing under
``~/.g3dt/synth_metadata/<version>/<study>/``) and uploaded/deleted from
there, so there is nothing to run on EC2.

Every command accepts ``--env``; targeting a **production** environment (any env
whose name contains ``prod``) shows a warning and requires typing the env name to
confirm — it cannot be bypassed.

Generation defaults to keyless ``random`` data (no API calls). Pass ``--llm`` for
LLM-realistic values, which needs an API key configured in a ``.env`` in the
working directory (``LLM_PROVIDER`` / ``LLM_MODEL`` / ``LLM_API_KEY_FILE``).
"""
from __future__ import annotations

from pathlib import Path

import typer

from g3dt.config import script_env
from g3dt.cli._internal import runner, safety
from g3dt.cli._internal.resolve import env_of
from g3dt.cli.dict_cmds import SCHEMA_DIR, dict_url

app = typer.Typer(
    no_args_is_help=True,
    help="Synthetic data lifecycle (local generation; prod requires typed confirmation).",
)

SYNTH_DIR = Path("~/.g3dt/synth_metadata").expanduser()


@app.command()
def deploy(
    env: str = typer.Option(
        "test", "--env", "-e", help="Target environment (prod requires typed confirmation)."
    ),
) -> None:
    """Full end-to-end synthetic deploy (dict + LLM-generate + upload + restarts).

    Wraps services/synthetic_data/full_deploy_dd_and_synth.sh (LLM-backed
    generation). Requires an LLM key configured in .env.
    """
    e = env_of(env)
    safety.confirm_prod_strict("synthetic full deploy", env)
    runner.run(
        runner.bash_script(
            "services/synthetic_data/full_deploy_dd_and_synth.sh", env
        ),
        env=script_env(e),
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
        help="Generate LLM-realistic values; reads LLM config from a .env in the "
        "working directory. Default is keyless random data (no API key, no API calls).",
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
    schema_path = schema or str(SCHEMA_DIR / f"acdc_schema_{ver}.json")

    # Ensure the schema is available locally; pull it if missing.
    if not Path(schema_path).exists():
        typer.secho(f"Schema not found locally; pulling {ver}...", fg=typer.colors.YELLOW)
        runner.run(
            runner.bash_script("services/dictionary/pull_dict.sh", dict_url(e, ver)),
            env=script_env(e),
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
    runner.run(
        runner.bash_script(
            "services/synthetic_data/generate_synth_metadata.sh", *args
        ),
        env=script_env(e),
    )


@app.command()
def upload(
    env: str = typer.Option(
        "test", "--env", "-e", help="Target environment (prod requires typed confirmation)."
    ),
    version: str = typer.Option(
        None, "--version", help="Dictionary version dir (default: the env's version)."
    ),
) -> None:
    """Upload generated synthetic metadata to Gen3 (reads local files)."""
    e = env_of(env)
    safety.confirm_prod_strict("synthetic metadata upload", env)
    v = version or e.dictionary_version
    base_dir = str(SYNTH_DIR / v) + "/"
    args = ["--base-dir", base_dir, "--aws-secret-name", e.aws_secret_name]
    if e.aws_profile:
        args += ["--aws-profile", e.aws_profile]
    runner.run(
        runner.python_script(
            "services/synthetic_data/upload_synth_metadata_sheepdog.py", *args
        ),
        env=script_env(e),
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
