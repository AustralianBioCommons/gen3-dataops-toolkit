"""Tests that a synthetic-data batch stays bound to the dictionary that made it.

Synthetic records are generated *from* a Gen3 dictionary and are only
schema-valid against that dictionary version. Pushing a batch built from v1.0.0
into an environment running v1.1.0 gets records Gen3 may reject outright or --
worse -- accept while they mean something different.

Two things made that easy to do by accident:

* `synth generate` takes ``--schema`` and ``--version`` as independent options,
  so the batch directory name (from ``--version``) could disagree with the
  dictionary actually used (from ``--schema``). The label is precisely the thing
  that can be wrong, so the directory name cannot be the check.
* `synth upload` looked at nothing but that directory name.

So generate now records provenance in the batch, and upload verifies it.
"""
import json
from unittest.mock import patch

import pytest
from typer.testing import CliRunner

from g3dt.cli.main import app
from g3dt.cli.synth import PROVENANCE_FILE
from g3dt.config import EnvConfig

runner = CliRunner()


def _env_cfg(name: str = "test", version: str = "v1.1.6") -> EnvConfig:
    return EnvConfig(
        name=name,
        is_ec2=False,
        region="ap-southeast-2",
        dictionary_version=version,
        aws_profile=None,
        aws_secret_name="sec",
        schema_s3_uri="u",
        domain="d",
        app_name="a",
        namespace="n",
        cluster_name="c",
        schema_repo="Org/schema-repo",
    )


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """Redirect both the schema cache and the synth output root to temp dirs."""
    schemas, synth = tmp_path / "schemas", tmp_path / "synth"
    schemas.mkdir()
    (schemas / "acdc_schema_v1.1.6.json").write_text("{}")
    monkeypatch.setattr("g3dt.cli.synth.SCHEMA_DIR", schemas)
    monkeypatch.setattr("g3dt.cli.synth.SYNTH_DIR", synth)
    return schemas, synth


def _seed_batch(synth_root, version, generated_from=None):
    """Create a batch dir, optionally with provenance naming its dictionary."""
    batch = synth_root / version
    batch.mkdir(parents=True)
    if generated_from is not None:
        (batch / PROVENANCE_FILE).write_text(
            json.dumps({"dictionary_version": generated_from})
        )
    return batch


# --------------------------------------------------------------------------- #
# generate: record what produced the batch, and reject a contradiction         #
# --------------------------------------------------------------------------- #
@patch("g3dt.cli.synth.env_of", side_effect=lambda e: _env_cfg(e))
@patch("g3dt.cli._internal.runner.run")
def test_generate_records_the_dictionary_it_used(mock_run, _env, dirs):
    """
    Inputs:  g3dt synth generate AusDiab_Simulated -n 5 (env dictionary v1.1.6)
    Expected: <synth>/v1.1.6/.g3dt-provenance.json naming v1.1.6, the URL it
              came from, and the schema file used

    Without this, a batch on disk is just a directory with a version-shaped
    name and no evidence of what actually generated it -- so upload has nothing
    to verify against.
    """
    _, synth = dirs
    result = runner.invoke(
        app, ["synth", "generate", "AusDiab_Simulated", "-n", "5"]
    )
    assert result.exit_code == 0, result.output

    recorded = json.loads((synth / "v1.1.6" / PROVENANCE_FILE).read_text())
    assert recorded["dictionary_version"] == "v1.1.6"
    assert recorded["schema_file"] == "acdc_schema_v1.1.6.json"
    assert "/refs/tags/v1.1.6/" in recorded["dictionary_url"]
    assert recorded["generated_for_env"] == "test"


@patch("g3dt.cli.synth.env_of", side_effect=lambda e: _env_cfg(e))
@patch("g3dt.cli._internal.runner.run")
def test_generate_rejects_schema_that_contradicts_version(mock_run, _env, dirs):
    """
    Inputs:  --schema .../acdc_schema_v1.0.0.json --version v1.1.0
    Expected: exit 1, nothing runs, both versions named in the error

    This is the mislabelling hole: it would generate records from the v1.0.0
    dictionary into a directory called v1.1.0, and every later step -- upload
    included -- would believe the label.
    """
    schemas, _ = dirs
    stale = schemas / "acdc_schema_v1.0.0.json"
    stale.write_text("{}")

    result = runner.invoke(
        app,
        ["synth", "generate", "AusDiab_Simulated", "-n", "5",
         "--schema", str(stale), "--version", "v1.1.0"],
    )
    assert result.exit_code == 1
    assert "v1.0.0" in result.output and "v1.1.0" in result.output
    mock_run.assert_not_called()


@patch("g3dt.cli.synth.env_of", side_effect=lambda e: _env_cfg(e))
@patch("g3dt.cli._internal.runner.run")
def test_generate_allows_an_unversioned_schema(mock_run, _env, dirs):
    """
    Inputs:  --schema .../my_draft.json (no version stamp in the name)
    Expected: runs normally

    A hand-made or draft dictionary makes no claim about its version, so there
    is nothing to contradict. Only a *stamped* name that disagrees is an error --
    otherwise this check would block a legitimate workflow.
    """
    schemas, _ = dirs
    draft = schemas / "my_draft.json"
    draft.write_text("{}")

    result = runner.invoke(
        app,
        ["synth", "generate", "AusDiab_Simulated", "-n", "5", "--schema", str(draft)],
    )
    assert result.exit_code == 0, result.output


# --------------------------------------------------------------------------- #
# upload: refuse a batch built against a different dictionary                  #
# --------------------------------------------------------------------------- #
@patch("g3dt.cli.synth.env_of", side_effect=lambda e: _env_cfg(e))
@patch("g3dt.cli._internal.runner.run")
def test_upload_refuses_a_batch_from_another_dictionary(mock_run, _env, dirs):
    """
    Inputs:  the v1.1.6 batch dir, but its provenance says it came from v1.0.0;
             g3dt synth upload --env test (test runs v1.1.6)
    Expected: exit 1, nothing uploaded, and the message offers both real fixes
              (regenerate for v1.1.6, or deploy v1.0.0 to the env first)

    This is the case the directory name cannot catch, because the directory name
    is what's wrong. It is also the one that reaches Gen3.
    """
    _, synth = dirs
    _seed_batch(synth, "v1.1.6", generated_from="v1.0.0")

    result = runner.invoke(app, ["synth", "upload", "--env", "test"])
    assert result.exit_code == 1
    assert "v1.0.0" in result.output
    assert "--allow-version-mismatch" in result.output
    mock_run.assert_not_called()


@patch("g3dt.cli.synth.env_of", side_effect=lambda e: _env_cfg(e))
@patch("g3dt.cli._internal.runner.run")
def test_upload_proceeds_when_the_batch_matches(mock_run, _env, dirs):
    """
    Inputs:  a v1.1.6 batch whose provenance says v1.1.6; upload --env test
    Expected: exit 0 and the uploader runs

    The check must not stand in the way of the normal path -- a batch generated
    for the environment it is being uploaded to.
    """
    _, synth = dirs
    _seed_batch(synth, "v1.1.6", generated_from="v1.1.6")

    result = runner.invoke(app, ["synth", "upload", "--env", "test"])
    assert result.exit_code == 0, result.output
    mock_run.assert_called_once()


@patch("g3dt.cli.synth.env_of", side_effect=lambda e: _env_cfg(e))
@patch("g3dt.cli._internal.runner.run")
def test_upload_mismatch_can_be_forced(mock_run, _env, dirs):
    """
    Inputs:  a mismatched batch plus --allow-version-mismatch
    Expected: exit 0, the uploader runs, and the mismatch is still announced

    An operator who knows the environment really runs the other version needs a
    way through. It stays loud so it can never pass unnoticed.
    """
    _, synth = dirs
    _seed_batch(synth, "v1.1.6", generated_from="v1.0.0")

    result = runner.invoke(
        app, ["synth", "upload", "--env", "test", "--allow-version-mismatch"]
    )
    assert result.exit_code == 0, result.output
    assert "mismatch allowed" in result.output
    mock_run.assert_called_once()


@patch("g3dt.cli.synth.env_of", side_effect=lambda e: _env_cfg(e))
@patch("g3dt.cli._internal.runner.run")
def test_upload_warns_but_proceeds_without_provenance(mock_run, _env, dirs):
    """
    Inputs:  a v1.1.6 batch with no provenance file; upload --env test
    Expected: exit 0, the uploader runs, and a warning explains why it could not
              be verified

    Batches generated before this change carry no provenance and cannot be
    attributed after the fact. Blocking them would break existing local state
    for no safety gain, so this degrades to a warning rather than an error.
    """
    _, synth = dirs
    _seed_batch(synth, "v1.1.6")

    result = runner.invoke(app, ["synth", "upload", "--env", "test"])
    assert result.exit_code == 0, result.output
    assert "No provenance" in result.output
    mock_run.assert_called_once()


@patch("g3dt.cli.synth.env_of", side_effect=lambda e: _env_cfg(e))
@patch("g3dt.cli._internal.runner.run")
def test_upload_version_flag_selects_a_promoted_batch(mock_run, _env, dirs):
    """
    Inputs:  a v1.0.0 batch (provenance v1.0.0); the env still declares v1.1.6;
             g3dt synth upload --env test --version v1.0.0
    Expected: exit 0, the v1.0.0 batch dir is uploaded, and the override is
              announced because SSM still says v1.1.6

    After `dict deploy --env test --version v1.0.0`, the environment really runs
    v1.0.0 while SSM has not caught up. Passing --version here is how the
    operator states that, and it must line the batch up with reality rather than
    with the stale declared value.
    """
    _, synth = dirs
    _seed_batch(synth, "v1.0.0", generated_from="v1.0.0")

    result = runner.invoke(
        app, ["synth", "upload", "--env", "test", "--version", "v1.0.0"]
    )
    assert result.exit_code == 0, result.output
    assert "SSM says v1.1.6" in result.output
    argv = list(mock_run.call_args.args[0])
    base_dir = argv[argv.index("--base-dir") + 1]
    assert base_dir.endswith("/v1.0.0/")
