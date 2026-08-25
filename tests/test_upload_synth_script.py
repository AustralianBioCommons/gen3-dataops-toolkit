"""Regression tests for the synth upload service script.

``upload_synth_metadata_sheepdog.py`` is step [6] of
``full_deploy_dd_and_synth.sh``: it walks the batch directory
(``~/.g3dt/synth_metadata/<version>/``) and submits each project
subdirectory's node JSONs to Sheepdog via ``MetadataSubmitter``.

The script is a standalone entrypoint (run with ``python3``, never
imported by the package), so nothing else in the suite exercises it and
its call into ``MetadataSubmitter`` can silently drift from the class's
real ``__init__`` signature. That happened live: a refactor replaced the
submitter's ``dataset_root`` parameter with ``athena_s3_output``, and the
first real ``g3dt synth deploy`` run afterwards died at step [6] with
``TypeError: unexpected keyword argument 'dataset_root'`` — after the
(billable) LLM generation had already succeeded.

The same run exposed a second bug: the script took every entry of the
batch dir as a project, including the ``.g3dt-provenance.json`` file that
``g3dt synth generate`` writes next to the study folders, and would have
tried to submit it as a project.

Both tests load the script by file path (there is no package ``__init__``
on the services tree) and stub out AWS/Gen3 so no network is touched.
"""
import importlib.util
import inspect
import os
import sys
from pathlib import Path

SCRIPT = (
    Path(__file__).resolve().parent.parent
    / "src" / "g3dt" / "services" / "synthetic_data"
    / "upload_synth_metadata_sheepdog.py"
)


def _load_script():
    """Import the standalone script as a module so its functions are testable."""
    spec = importlib.util.spec_from_file_location("upload_synth_script", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_submitter_kwargs_match_the_real_init_signature(monkeypatch, tmp_path):
    """
    Inputs:  submit_synthetic_metadata() run with AWS helpers stubbed and
             MetadataSubmitter replaced by a recorder that captures the
             keyword arguments the script actually passes.
    Expected: those exact kwargs bind cleanly to the REAL
             MetadataSubmitter.__init__ signature. If the class API changes
             again (as it did when dataset_root became athena_s3_output),
             this test fails offline instead of a live deploy failing at
             step [6].
    """
    from g3dt.upload.metadata_submitter import MetadataSubmitter

    monkeypatch.chdir(tmp_path)  # the script chdirs; restore cwd afterwards
    mod = _load_script()

    recorded = {}

    class RecordingSubmitter:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

        def submit_metadata(self):
            pass

    monkeypatch.setattr(mod, "MetadataSubmitter", RecordingSubmitter)
    monkeypatch.setattr(mod, "create_boto3_session", lambda profile: object())
    monkeypatch.setattr(
        mod, "get_gen3_api_key_aws_secret", lambda name, region, session: {"api_key": "k"}
    )
    monkeypatch.setattr(
        mod, "find_data_import_order_file", lambda d: f"{d}/DataImportOrder.txt"
    )
    monkeypatch.setattr(mod, "list_metadata_jsons", lambda d: [f"{d}/subject.json"])

    mod.submit_synthetic_metadata(
        base_dir=str(tmp_path / "v1.3.0" / "study_a"),
        project_id="study_a",
        aws_secret_name="secret",
    )

    # bind() raises TypeError on any unknown, missing, or misnamed argument.
    inspect.signature(MetadataSubmitter.__init__).bind(None, **recorded)


def test_core_metadata_collection_is_excluded_from_submission(monkeypatch, tmp_path):
    """
    Inputs:  submit_synthetic_metadata() run with the same stubs as above,
             capturing the exclude_nodes the script hands to MetadataSubmitter.
    Expected: core_metadata_collection is excluded, alongside the structural
             nodes (program/project/acknowledgement/publication).

             MetadataSubmitter's own default only excludes the structural
             four; the simulator generates core_metadata_collection with
             random words in its date-time fields, so submitting it made a
             live deploy fail with "Transaction aborted due to 50 invalid
             entities" (400) — and synthetic batches have no use for that
             node in the first place.
    """
    monkeypatch.chdir(tmp_path)
    mod = _load_script()

    recorded = {}

    class RecordingSubmitter:
        def __init__(self, **kwargs):
            recorded.update(kwargs)

        def submit_metadata(self):
            pass

    monkeypatch.setattr(mod, "MetadataSubmitter", RecordingSubmitter)
    monkeypatch.setattr(mod, "create_boto3_session", lambda profile: object())
    monkeypatch.setattr(
        mod, "get_gen3_api_key_aws_secret", lambda name, region, session: {"api_key": "k"}
    )
    monkeypatch.setattr(
        mod, "find_data_import_order_file", lambda d: f"{d}/DataImportOrder.txt"
    )
    monkeypatch.setattr(mod, "list_metadata_jsons", lambda d: [f"{d}/subject.json"])

    mod.submit_synthetic_metadata(
        base_dir=str(tmp_path / "v1.3.0" / "study_a"),
        project_id="study_a",
        aws_secret_name="secret",
    )

    excluded = set(recorded["exclude_nodes"])
    assert "core_metadata_collection" in excluded
    assert {"program", "project", "acknowledgement", "publication"} <= excluded


def test_main_submits_only_project_directories(monkeypatch, tmp_path):
    """
    Inputs:  a batch directory laid out exactly as `g3dt synth generate`
             leaves it — one study folder plus the .g3dt-provenance.json
             marker file (and a stray regular file for good measure).
    Expected: main() submits only the study folder. The live failure mode
             was "Found 2 project subdirectories: ['synthetic_dataset_1',
             '.g3dt-provenance.json']" — a file queued for submission as a
             project.
    """
    monkeypatch.chdir(tmp_path)
    mod = _load_script()

    base = tmp_path / "v1.3.0"
    (base / "synthetic_dataset_1").mkdir(parents=True)
    (base / ".g3dt-provenance.json").write_text("{}")
    (base / "notes.txt").write_text("not a project")

    submitted = []
    monkeypatch.setattr(
        mod,
        "submit_synthetic_metadata",
        lambda **kwargs: submitted.append(kwargs["project_id"]),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "upload_synth_metadata_sheepdog.py",
            "--base-dir", str(base),
            "--aws-secret-name", "secret",
        ],
    )

    mod.main()

    assert submitted == ["synthetic_dataset_1"]
    # The stub received the study's own folder as its base_dir, not the batch root.
    assert os.path.isdir(base / "synthetic_dataset_1")


def test_main_projects_scopes_to_requested_dirs(monkeypatch, tmp_path, caplog):
    """
    Inputs:  a batch dir holding the two requested studies PLUS a stale
             'synth50' directory left over from an earlier batch, and
             --projects naming only the two studies.
    Expected: only the requested studies are submitted, in the requested
             order, and the stale directory is skipped with a log line.

    Background (live incident): `g3dt synth deploy --studies s1,s2,s3` found
    a stale 'synth50' dir from a previous run against a DIFFERENT commons.
    It sorted first, 404'd ("Project synth50 not found"), and set -e killed
    the deploy before any requested study uploaded. --projects is the fix:
    the deploy flow now always passes its --studies here.
    """
    import logging

    monkeypatch.chdir(tmp_path)
    mod = _load_script()

    base = tmp_path / "v1.3.0"
    for d in ("study_b", "study_a", "synth50"):
        (base / d).mkdir(parents=True)

    submitted = []
    monkeypatch.setattr(
        mod,
        "submit_synthetic_metadata",
        lambda **kwargs: submitted.append(kwargs["project_id"]),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "upload_synth_metadata_sheepdog.py",
            "--base-dir", str(base),
            "--aws-secret-name", "secret",
            "--projects", "study_a,study_b",
        ],
    )

    with caplog.at_level(logging.INFO):
        mod.main()

    assert submitted == ["study_a", "study_b"]
    assert "Skipping synth50 (not in --projects)" in caplog.text


def test_main_projects_missing_dir_fails_before_any_submission(
    monkeypatch, tmp_path
):
    """
    Inputs:  --projects naming a study whose directory does not exist.
    Expected: FileNotFoundError naming the missing directory BEFORE anything
             is submitted — a wrong scope (typo, wrong version dir) must not
             half-upload a batch.
    """
    import pytest

    monkeypatch.chdir(tmp_path)
    mod = _load_script()

    base = tmp_path / "v1.3.0"
    (base / "study_a").mkdir(parents=True)

    submitted = []
    monkeypatch.setattr(
        mod,
        "submit_synthetic_metadata",
        lambda **kwargs: submitted.append(kwargs["project_id"]),
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "upload_synth_metadata_sheepdog.py",
            "--base-dir", str(base),
            "--aws-secret-name", "secret",
            "--projects", "study_a,nope",
        ],
    )

    with pytest.raises(FileNotFoundError, match="nope"):
        mod.main()
    assert submitted == []


def test_main_continues_past_a_failing_project_and_fails_at_end(
    monkeypatch, tmp_path
):
    """
    Inputs:  two study dirs; submission raises for the first one.
    Expected: the second study is still submitted, then the script exits 1
             naming the failed project.

    Background: submissions run AFTER the (billable) LLM generation. With
    the old fail-fast loop, one bad project threw away every healthy study's
    upload; now healthy studies land first and only the re-run of the failed
    one is needed.
    """
    import pytest

    monkeypatch.chdir(tmp_path)
    mod = _load_script()

    base = tmp_path / "v1.3.0"
    (base / "study_a").mkdir(parents=True)
    (base / "study_b").mkdir(parents=True)

    submitted = []

    def _submit(**kwargs):
        if kwargs["project_id"] == "study_a":
            raise RuntimeError("404 Project study_a not found")
        submitted.append(kwargs["project_id"])

    monkeypatch.setattr(mod, "submit_synthetic_metadata", _submit)
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "upload_synth_metadata_sheepdog.py",
            "--base-dir", str(base),
            "--aws-secret-name", "secret",
        ],
    )

    with pytest.raises(SystemExit) as excinfo:
        mod.main()
    assert excinfo.value.code == 1
    assert submitted == ["study_b"]
