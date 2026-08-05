"""Routing tests for the bulk metadata-delete shell script.

``services/delete/delete_metadata.sh`` is the layer that fans one job out over
several studies, and since each study may carry its own version, it is also
the layer that decides *which worker* each study goes to: a specific version
uses the Athena GUID lookup, ``all`` wipes whole nodes. Getting that branch
wrong would delete far more than intended, and it is not reachable from the
Python tests — the CLI stops at building argv.

So these tests run the real script with a stubbed ``python3`` on PATH that
records the command line it was handed and exits with a chosen code. That lets
us assert the routing, the version each worker receives, and the
deleted/skipped/failed accounting without touching Gen3 or AWS.
"""
import os
import subprocess
from pathlib import Path

import pytest

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src" / "g3dt" / "services" / "delete" / "delete_metadata.sh"
)


@pytest.fixture
def stub_python(tmp_path):
    """Put a fake ``python3`` on PATH that logs its arguments.

    The stub exits 0 normally, 3 for any study whose name contains ``skipme``
    (the worker's "no data at this version" code), and 4 for ``failme`` (a
    genuine error). Returning the record file lets a test assert exactly which
    worker each study was routed to.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    record = tmp_path / "record.txt"
    stub = bin_dir / "python3"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        'echo "$*" >> "$STUB_RECORD"\n'
        'case "$*" in\n'
        "  *skipme*) exit 3 ;;\n"
        "  *failme*) exit 4 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    stub.chmod(0o755)

    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["STUB_RECORD"] = str(record)
    # Keep the script's failure log inside the tmpdir, not the real ~/.g3dt.
    env["HOME"] = str(tmp_path)
    return env, record


def _run(env, *args):
    return subprocess.run(
        ["bash", str(SCRIPT), *args],
        capture_output=True,
        text=True,
        env=env,
    )


def test_each_study_is_routed_to_its_own_worker_and_version(stub_python):
    """
    Background:
        The whole point of per-study versions is that one job can mix paths —
        retire one study entirely while removing a single version from
        another. The script picks the worker per study, so this asserts the
        branch is inside the loop rather than decided once for the batch.

    Inputs:  --studies "ausdiab_staging:0.7.5,cdah_staging:all"
    Expected Output:
      - ausdiab goes to delete_metadata_by_guid.py with --version 0.7.5
      - cdah goes to delete_all_metadata_for_project.py with no --version
    """
    env, record = stub_python

    _run(env, "--studies", "ausdiab_staging:0.7.5,cdah_staging:all",
         "--env", "staging")

    calls = record.read_text().strip().splitlines()
    assert len(calls) == 2
    assert "delete_metadata_by_guid.py" in calls[0]
    assert "--study ausdiab_staging" in calls[0]
    assert "--version 0.7.5" in calls[0]
    assert "delete_all_metadata_for_project.py" in calls[1]
    assert "--study cdah_staging" in calls[1]
    assert "--version" not in calls[1]


def test_bare_entries_still_take_the_version_flag(stub_python):
    """
    Background:
        The EC2 box's installed script can lag or lead the operator's local
        CLI, so the historical argv shape — plain study keys plus one
        --version — must keep working unchanged.

    Inputs:  --studies "a_staging,b_staging" --version 0.9.8
    Expected Output: both studies routed to the GUID worker with 0.9.8.
    """
    env, record = stub_python

    _run(env, "--studies", "a_staging,b_staging",
         "--env", "staging", "--version", "0.9.8")

    calls = record.read_text().strip().splitlines()
    assert len(calls) == 2
    for call in calls:
        assert "delete_metadata_by_guid.py" in call
        assert "--version 0.9.8" in call


def test_mixed_bare_and_qualified_entries(stub_python):
    """
    Inputs:  --studies "a_staging:0.7.5,b_staging" --version 1.0.0
    Expected Output: a gets its own 0.7.5; bare b falls back to 1.0.0.
    """
    env, record = stub_python

    _run(env, "--studies", "a_staging:0.7.5,b_staging",
         "--env", "staging", "--version", "1.0.0")

    calls = record.read_text().strip().splitlines()
    assert "--version 0.7.5" in calls[0]
    assert "--version 1.0.0" in calls[1]


def test_missing_version_aborts_before_anything_is_deleted(stub_python):
    """
    Background:
        Validation is all-up-front: if the SECOND entry lacks a version, the
        first must not already have been dispatched — otherwise a typo'd batch
        is half-executed.

    Inputs:  --studies "a_staging:0.7.5,b_staging" with NO --version
    Expected Output: non-zero exit, and no worker was ever invoked.
    """
    env, record = stub_python

    result = _run(env, "--studies", "a_staging:0.7.5,b_staging",
                  "--env", "staging")

    assert result.returncode != 0
    assert not record.exists()


def test_skip_exit_code_counts_as_skip_not_failure(stub_python):
    """
    Background:
        Worker exit 3 means "study has no data at this version" — the normal,
        healthy outcome for an already-clean study. The batch must continue
        and finish with exit 0, counting it as skipped rather than failed.

    Inputs:  one skipping study and one deleting study
    Expected Output: overall exit 0; summary shows 1 deleted / 1 skipped.
    """
    env, _ = stub_python

    result = _run(env, "--studies", "skipme_staging:0.9.8,ok_staging:0.9.8",
                  "--env", "staging")

    assert result.returncode == 0
    assert "Deleted       : 1" in result.stdout
    assert "Skipped       : 1" in result.stdout


def test_one_failure_fails_the_batch_and_logs_the_version(stub_python):
    """
    Background:
        A genuine worker error must fail the whole run (exit 1) — and the
        failure log must record WHICH version failed, because one job can now
        delete two versions of the same study.

    Inputs:  one failing study (worker exit 4) and one succeeding
    Expected Output: exit 1; the log line carries version=0.7.5; the other
    study still ran.
    """
    env, record = stub_python

    result = _run(env, "--studies", "failme_staging:0.7.5,ok_staging:0.9.8",
                  "--env", "staging")

    assert result.returncode == 1
    calls = record.read_text().strip().splitlines()
    assert len(calls) == 2  # the failure did not stop the loop
    logs = list((Path(env["HOME"]) / ".g3dt" / "logs").glob("*_delete_failed.log"))
    assert len(logs) == 1
    assert "failme_staging version=0.7.5" in logs[0].read_text()
