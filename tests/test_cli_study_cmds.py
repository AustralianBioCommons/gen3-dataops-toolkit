"""CLI tests for the ``g3dt study`` group (list/show/add/set/remove/repoint).

Background: this group manages the registry that decides what a metadata
upload sends to a live commons, so the tests focus on the three properties
the operator relies on: usage mistakes fail fast with guidance (exit 2),
registry problems fail with the fix named (exit 1), and ``repoint`` proves
every target before writing ANYTHING — one bad target must leave every
parameter untouched. Production gates follow the house rule: typed token,
``--yes`` never bypasses.

Moto provides SSM and S3 (real ParameterAlreadyExists / missing-prefix
behaviour); only the Athena ledger read behind ``--latest`` is patched.
"""
import json
from unittest.mock import patch

import boto3
import pytest
from moto import mock_aws
from typer.testing import CliRunner

from g3dt.cli.main import app

runner = CliRunner()

REGION = "ap-southeast-2"
GOLD = "etl-gold"


@pytest.fixture(autouse=True)
def _region(monkeypatch):
    monkeypatch.setenv("AWS_DEFAULT_REGION", REGION)


def _seed_tree(env="staging"):
    ssm = boto3.client("ssm", region_name=REGION)
    for rel, value in {
        "meta/region": REGION,
        "buckets/metadata": "etl-meta",
    }.items():
        ssm.put_parameter(Name=f"/etl/{env}/{rel}", Value=value, Type="String")


def _seed_study(name, path, env="staging"):
    boto3.client("ssm", region_name=REGION).put_parameter(
        Name=f"/etl/{env}/studies/{name}",
        Value=json.dumps(
            {
                "project_id": name.upper(),
                "program_id": "program1",
                "s3_metadata_path": path,
            }
        ),
        Type="String",
    )


def _param(name, env="staging"):
    return boto3.client("ssm", region_name=REGION).get_parameter(
        Name=f"/etl/{env}/studies/{name}"
    )["Parameter"]["Value"]


def _seed_release_prefix(version, study):
    """A valid upload target: DataImportOrder.txt plus one node JSON."""
    s3 = boto3.client("s3", region_name=REGION)
    try:
        s3.create_bucket(
            Bucket=GOLD,
            CreateBucketConfiguration={"LocationConstraint": REGION},
        )
    except s3.exceptions.BucketAlreadyOwnedByYou:
        pass
    prefix = f"release_jsons/{version}/{study}"
    s3.put_object(Bucket=GOLD, Key=f"{prefix}/DataImportOrder.txt", Body=b"subject\n")
    s3.put_object(Bucket=GOLD, Key=f"{prefix}/subject.json", Body=b"[]")


def _path(version, study):
    return f"s3://{GOLD}/release_jsons/{version}/{study}/"


# --------------------------------------------------------------------------- #
# list / show                                                                  #
# --------------------------------------------------------------------------- #
@mock_aws
def test_list_names_on_stdout_banner_on_stderr():
    """
    Inputs:  two registered studies
    Expected: stdout is EXACTLY the sorted bare names (script-friendly, like
              `config current`); the banner and source line stay on stderr.
    """
    _seed_tree()
    _seed_study("ausdiab", _path("v2.0.0", "ausdiab"))
    _seed_study("cdah", _path("v2.0.0", "cdah"))
    result = runner.invoke(app, ["study", "list", "--env", "staging"])
    assert result.exit_code == 0, result.output
    assert result.stdout.splitlines() == ["ausdiab", "cdah"]
    assert "/etl/staging/studies" in result.stderr


@mock_aws
def test_list_empty_registry_guides_and_exits_zero():
    """
    Inputs:  a deployed env with no studies
    Expected: exit 0 (empty is a state, not a failure) with the add/migrate
              commands on stderr and NOTHING on stdout.
    """
    _seed_tree()
    result = runner.invoke(app, ["study", "list", "--env", "staging"])
    assert result.exit_code == 0, result.output
    assert result.stdout == ""
    assert "g3dt study add" in result.stderr
    assert "g3dt study migrate" in result.stderr


@mock_aws
def test_show_prints_record_and_liveness_pass():
    """
    Inputs:  a study whose path holds DataImportOrder.txt + a node JSON
    Expected: the three fields plus 'liveness : PASS' with the JSON count —
              the same checks upload will make, run ahead of time.
    """
    _seed_tree()
    _seed_release_prefix("v2.0.0", "cdah")
    _seed_study("cdah", _path("v2.0.0", "cdah"))
    result = runner.invoke(app, ["study", "show", "cdah", "--env", "staging"])
    assert result.exit_code == 0, result.output
    assert "project_id       : CDAH" in result.stdout
    assert "PASS" in result.stdout
    assert "1 node JSONs" in result.stdout


@mock_aws
def test_show_uppercase_resolves_with_note():
    """
    Inputs:  `study show CDAH` (the live 4.0.1 confusion: project_id case)
    Expected: resolves the canonical 'cdah' record and says so on stderr.
    """
    _seed_tree()
    _seed_release_prefix("v2.0.0", "cdah")
    _seed_study("cdah", _path("v2.0.0", "cdah"))
    result = runner.invoke(app, ["study", "show", "CDAH", "--env", "staging"])
    assert result.exit_code == 0, result.output
    assert "name             : cdah" in result.stdout
    assert "resolved 'CDAH' -> 'cdah'" in result.stderr


@mock_aws
def test_show_unknown_lists_registered_names():
    """
    Inputs:  a typo'd study name
    Expected: exit 1, the real registry contents, and a did-you-mean.
    """
    _seed_tree()
    _seed_study("cdah", _path("v2.0.0", "cdah"))
    result = runner.invoke(app, ["study", "show", "cdha", "--env", "staging"])
    assert result.exit_code == 1
    assert "Registered: cdah" in result.stderr
    assert "Did you mean 'cdah'" in result.stderr


# --------------------------------------------------------------------------- #
# add / set / remove                                                           #
# --------------------------------------------------------------------------- #
@mock_aws
def test_add_happy_path_writes_parameter():
    _seed_tree()
    result = runner.invoke(
        app,
        [
            "study", "add", "cdah", "--env", "staging",
            "--project-id", "CDAH", "--program-id", "program1",
            "--path", _path("v2.0.0", "cdah"),
        ],
    )
    assert result.exit_code == 0, result.output
    assert json.loads(_param("cdah"))["project_id"] == "CDAH"


@mock_aws
def test_add_uppercase_name_is_usage_error():
    """
    Inputs:  `study add CDAH ...`
    Expected: exit 2 with the lowercase hint; nothing written.
    """
    _seed_tree()
    result = runner.invoke(
        app,
        [
            "study", "add", "CDAH", "--env", "staging",
            "--project-id", "CDAH", "--program-id", "program1",
            "--path", "s3://b/v1/cdah/",
        ],
    )
    assert result.exit_code == 2
    assert "'cdah'" in result.stderr


@mock_aws
def test_add_non_s3_path_is_usage_error():
    _seed_tree()
    result = runner.invoke(
        app,
        [
            "study", "add", "cdah", "--env", "staging",
            "--project-id", "CDAH", "--program-id", "program1",
            "--path", "/local/dir",
        ],
    )
    assert result.exit_code == 2
    assert "s3://" in result.stderr


@mock_aws
def test_add_duplicate_points_at_set():
    _seed_tree()
    _seed_study("cdah", _path("v2.0.0", "cdah"))
    result = runner.invoke(
        app,
        [
            "study", "add", "cdah", "--env", "staging",
            "--project-id", "CDAH", "--program-id", "program1",
            "--path", _path("v2.0.0", "cdah"),
        ],
    )
    assert result.exit_code == 1
    assert "g3dt study set" in result.stderr


@mock_aws
def test_set_no_fields_is_usage_error():
    _seed_tree()
    _seed_study("cdah", _path("v2.0.0", "cdah"))
    result = runner.invoke(app, ["study", "set", "cdah", "--env", "staging"])
    assert result.exit_code == 2
    assert "Nothing to set" in result.stderr


@mock_aws
def test_set_merges_single_field_and_prints_diff():
    """
    Inputs:  `study set cdah --program-id program2`
    Expected: only that field changes (read-merge-write is atomic per
              study), and the old -> new line is printed.
    """
    _seed_tree()
    _seed_study("cdah", _path("v2.0.0", "cdah"))
    result = runner.invoke(
        app,
        ["study", "set", "cdah", "--env", "staging", "--program-id", "program2"],
    )
    assert result.exit_code == 0, result.output
    record = json.loads(_param("cdah"))
    assert record["program_id"] == "program2"
    assert record["project_id"] == "CDAH"  # untouched
    assert "program_id: program1 -> program2" in result.stdout


@mock_aws
def test_remove_happy_with_yes():
    _seed_tree()
    _seed_study("cdah", _path("v2.0.0", "cdah"))
    result = runner.invoke(
        app, ["study", "remove", "cdah", "--env", "staging", "--yes"]
    )
    assert result.exit_code == 0, result.output
    ssm = boto3.client("ssm", region_name=REGION)
    with pytest.raises(ssm.exceptions.ParameterNotFound):
        ssm.get_parameter(Name="/etl/staging/studies/cdah")


@mock_aws
def test_mutations_refused_while_legacy_registry_serves():
    """
    Inputs:  `study add` while the registry still resolves via the legacy
             S3 studies.yaml (SSM subtree empty)
    Expected: refused with `g3dt study migrate` named — writing one SSM
              record now would shadow the whole legacy file and silently
              hide its other studies.
    """
    _seed_tree()
    s3 = boto3.client("s3", region_name=REGION)
    s3.create_bucket(
        Bucket="etl-meta",
        CreateBucketConfiguration={"LocationConstraint": REGION},
    )
    s3.put_object(
        Bucket="etl-meta",
        Key="config/studies.yaml",
        Body=(
            b"studies:\n  ausdiab_staging:\n    project_id: AusDiab\n"
            b"    program_id: program1\n"
            b"    s3_metadata_path: s3://b/v1/ausdiab/\n"
        ),
    )
    result = runner.invoke(
        app,
        [
            "study", "add", "cdah", "--env", "staging",
            "--project-id", "CDAH", "--program-id", "program1",
            "--path", "s3://b/v1/cdah/",
        ],
    )
    assert result.exit_code == 1
    assert "g3dt study migrate" in result.stderr
    ssm = boto3.client("ssm", region_name=REGION)
    with pytest.raises(ssm.exceptions.ParameterNotFound):
        ssm.get_parameter(Name="/etl/staging/studies/cdah")


# --------------------------------------------------------------------------- #
# repoint                                                                      #
# --------------------------------------------------------------------------- #
@mock_aws
def test_repoint_release_and_latest_are_mutually_exclusive():
    _seed_tree()
    result = runner.invoke(app, ["study", "repoint", "--env", "staging"])
    assert result.exit_code == 2
    assert "exactly one of --release" in result.stderr


@mock_aws
def test_repoint_normalises_v_prefix_and_writes():
    """
    Inputs:  paths at v2.0.0, `repoint --release v2.1.0` (operator types the
             v), valid v2.1.0 targets in S3
    Expected: both records now point at v2.1.0 — the ledger-vs-path prefix
              asymmetry is handled once, centrally.
    """
    _seed_tree()
    for study in ("ausdiab", "cdah"):
        _seed_study(study, _path("v2.0.0", study))
        _seed_release_prefix("v2.1.0", study)
    result = runner.invoke(
        app, ["study", "repoint", "--release", "v2.1.0", "--env", "staging"]
    )
    assert result.exit_code == 0, result.output
    for study in ("ausdiab", "cdah"):
        assert json.loads(_param(study))["s3_metadata_path"] == _path(
            "v2.1.0", study
        )
    assert "Repointed 2 of 2" in result.stdout


@mock_aws
def test_repoint_bogus_target_writes_nothing():
    """
    Inputs:  two studies; only ONE has a valid v9.9.9 export in S3
    Expected: exit 1 naming the missing target and NEITHER parameter
              changes — validate-all-before-write is the whole point.
    """
    _seed_tree()
    for study in ("ausdiab", "cdah"):
        _seed_study(study, _path("v2.0.0", study))
    _seed_release_prefix("v9.9.9", "ausdiab")  # cdah's target is missing
    before = {s: _param(s) for s in ("ausdiab", "cdah")}
    result = runner.invoke(
        app, ["study", "repoint", "--release", "9.9.9", "--env", "staging"]
    )
    assert result.exit_code == 1
    assert "nothing was written" in result.stderr
    assert "cdah" in result.stderr
    assert {s: _param(s) for s in ("ausdiab", "cdah")} == before


@mock_aws
def test_repoint_dry_run_prints_diff_writes_nothing():
    _seed_tree()
    _seed_study("cdah", _path("v2.0.0", "cdah"))
    _seed_release_prefix("v2.1.0", "cdah")
    before = _param("cdah")
    result = runner.invoke(
        app,
        ["study", "repoint", "--release", "2.1.0", "--env", "staging", "-d"],
    )
    assert result.exit_code == 0, result.output
    assert "v2.0.0/cdah/ -> " in result.stdout
    assert "Dry run" in result.stdout
    assert _param("cdah") == before


@mock_aws
def test_repoint_latest_empty_ledger_guides():
    """
    Inputs:  `repoint --latest` on an env that never cut a release
    Expected: exit 1 telling the operator to cut a release or pass
              --release — not an Athena traceback.
    """
    _seed_tree()
    _seed_study("cdah", _path("v2.0.0", "cdah"))
    with patch("g3dt.studies.latest_release_tag", return_value=None):
        result = runner.invoke(
            app, ["study", "repoint", "--latest", "--env", "staging"]
        )
    assert result.exit_code == 1
    assert "--release" in result.stderr


@mock_aws
def test_repoint_unknown_subset_member_guides_before_any_validation():
    _seed_tree()
    _seed_study("cdah", _path("v2.0.0", "cdah"))
    result = runner.invoke(
        app,
        [
            "study", "repoint", "--release", "2.1.0", "--env", "staging",
            "--studies", "cdah,ghost",
        ],
    )
    assert result.exit_code == 1
    assert "Registered: cdah" in result.stderr


# --------------------------------------------------------------------------- #
# production gates                                                             #
# --------------------------------------------------------------------------- #
@mock_aws
def test_add_prod_gate_typed_token():
    """
    Inputs:  `study add ... --env prod`, empty confirmation
    Expected: exit 1 and NOTHING written — the registry decides what
              uploads to prod, so mutating it earns the typed gate.
    """
    _seed_tree(env="prod")
    result = runner.invoke(
        app,
        [
            "study", "add", "cdah", "--env", "prod",
            "--project-id", "CDAH", "--program-id", "program1",
            "--path", "s3://b/v1/cdah/",
        ],
        input="\n",
    )
    assert result.exit_code == 1
    ssm = boto3.client("ssm", region_name=REGION)
    with pytest.raises(ssm.exceptions.ParameterNotFound):
        ssm.get_parameter(Name="/etl/prod/studies/cdah")


@mock_aws
def test_remove_yes_does_not_bypass_prod_gate():
    """
    Inputs:  `study remove --env prod --yes`, empty confirmation
    Expected: --yes never skips the typed prod gate; the record survives.
    """
    _seed_tree(env="prod")
    _seed_study("cdah", "s3://b/v1/cdah/", env="prod")
    result = runner.invoke(
        app,
        ["study", "remove", "cdah", "--env", "prod", "--yes"],
        input="\n",
    )
    assert result.exit_code == 1
    assert _param("cdah", env="prod")  # still there


@mock_aws
def test_repoint_prod_gate_sits_between_diff_and_write():
    """
    Inputs:  a valid prod repoint, confirmation typed correctly
    Expected: the diff prints BEFORE the prompt (the operator confirms what
              they saw), and the write lands after the token matches.
    """
    _seed_tree(env="prod")
    _seed_study("cdah", _path("v2.0.0", "cdah"), env="prod")
    _seed_release_prefix("v2.1.0", "cdah")
    result = runner.invoke(
        app,
        ["study", "repoint", "--release", "2.1.0", "--env", "prod"],
        input="prod\n",
    )
    assert result.exit_code == 0, result.output
    assert json.loads(_param("cdah", env="prod"))["s3_metadata_path"] == _path(
        "v2.1.0", "cdah"
    )
