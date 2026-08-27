"""Flag-forwarding tests for the synthetic-data generator shell script.

``services/synthetic_data/generate_synth_metadata.sh`` is the layer between
`g3dt synth` and gen3-metadata-simulator. Since v3.4.0 the LLM vendor and
model arrive as ``$G3DT_LLM_PROVIDER`` / ``$G3DT_LLM_MODEL`` (resolved by g3dt
with precedence flags > SSM > default) and must be forwarded to the simulator
as CLI flags — the simulator's own precedence puts flags above any ``.env``,
which is exactly what makes the deployment's values authoritative. The script
must also pass ``--env-file /dev/null`` so a stray ``.env`` in the caller's
working directory can never hijack resolution, and must no longer read the
retired ``~/.g3dt/.env``.

None of that branching is reachable from the Python tests (the CLI stops at
building argv), so these run the real script with a stubbed
``gen3-metadata-simulator`` on PATH that records the command line it was
handed.
"""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src" / "g3dt" / "services" / "synthetic_data" / "generate_synth_metadata.sh"
)


@pytest.fixture
def stub_simulator(tmp_path):
    """Put a fake ``gen3-metadata-simulator`` on PATH that logs its arguments."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "record.txt"
    stub = bin_dir / "gen3-metadata-simulator"
    stub.write_text('#!/usr/bin/env bash\necho "$*" >> "$STUB_RECORD"\nexit 0\n')
    stub.chmod(0o755)
    return bin_dir, record


def _run(stub_simulator, tmp_path, extra_args=(), extra_env=None):
    bin_dir, record = stub_simulator
    schema = tmp_path / "schema.json"
    schema.write_text("{}")
    env = dict(
        os.environ,
        PATH=f"{bin_dir}:{os.environ['PATH']}",
        STUB_RECORD=str(record),
        G3DT_SYNTH_DIR=str(tmp_path / "out"),
    )
    if extra_env:
        env.update(extra_env)
    result = subprocess.run(
        ["bash", str(SCRIPT), "--schema", str(schema), "--version", "v1",
         "--studies", "Study_A", *extra_args],
        env=env, capture_output=True, text=True,
    )
    assert result.returncode == 0, result.stderr
    return record.read_text()


def test_llm_provider_and_model_forwarded_as_flags(stub_simulator, tmp_path):
    """
    Inputs:  --provider llm with G3DT_LLM_PROVIDER/G3DT_LLM_MODEL in the env
             (what g3dt exports after resolving flags > SSM > default)
    Expected: the simulator receives them as --llm-provider/--llm-model flags
             plus --env-file /dev/null, so the resolved values are
             authoritative and no filesystem .env can override them.
    """
    recorded = _run(
        stub_simulator, tmp_path,
        extra_args=("--provider", "llm"),
        extra_env={"G3DT_LLM_PROVIDER": "anthropic", "G3DT_LLM_MODEL": "some-model"},
    )
    assert "--llm-provider anthropic" in recorded
    assert "--llm-model some-model" in recorded
    assert "--env-file /dev/null" in recorded


def test_llm_without_env_vars_still_neutralizes_cwd_env(stub_simulator, tmp_path):
    """
    Inputs:  --provider llm with NO G3DT_LLM_* env vars (a caller invoking the
             script directly rather than through g3dt)
    Expected: no --llm-provider/--llm-model flags (the simulator's own
             defaults apply), but --env-file /dev/null is still passed — the
             retired ~/.g3dt/.env must not resurface through the simulator's
             CWD default.
    """
    recorded = _run(stub_simulator, tmp_path, extra_args=("--provider", "llm"))
    assert "--llm-provider" not in recorded
    assert "--llm-model" not in recorded
    assert "--env-file /dev/null" in recorded


def test_random_provider_passes_no_llm_flags(stub_simulator, tmp_path):
    """
    Inputs:  the default keyless random provider (G3DT_LLM_* vars present, as
             script_env always exports the provider)
    Expected: none of the LLM flags are passed — the random path stays exactly
             as before, unaffected by any LLM configuration.
    """
    recorded = _run(
        stub_simulator, tmp_path,
        extra_env={"G3DT_LLM_PROVIDER": "anthropic", "G3DT_LLM_MODEL": "some-model"},
    )
    assert "--provider random" in recorded
    assert "--llm-provider" not in recorded
    assert "--env-file" not in recorded


def test_data_version_is_forwarded_as_set_override(stub_simulator, tmp_path):
    """
    Background:
        Synthetic records carry no version marker, which is why versioned
        deletion ('delete metadata --synthetic --version X') matches nothing
        on old batches. --data-version closes that loop: the simulator's --set
        pins a declared property to a constant on every record, making the
        batch version-deletable later.

    Inputs:  --data-version v1.3.0
    Expected: the simulator receives --set data_version=v1.3.0.
    """
    recorded = _run(
        stub_simulator, tmp_path, extra_args=("--data-version", "v1.3.0")
    )
    assert "--set data_version=v1.3.0" in recorded


def test_no_data_version_passes_no_set_override(stub_simulator, tmp_path):
    """
    Inputs:  no --data-version (the default)
    Expected: no --set flag at all — dictionaries that do not declare
             data_version would make the simulator error pre-generation, so
             stamping must be strictly opt-in.
    """
    recorded = _run(stub_simulator, tmp_path)
    assert "--set" not in recorded
