import importlib.util
import logging
import pathlib
import sys
from unittest.mock import MagicMock, patch, call
import pytest
import pandas as pd
from g3dt.upload.metadata_deleter import (
    delete_project_metadata,
    query_metadata_upload_guids,
    delete_records_by_guid,
)


# ==========================================
# Fixtures (Reusable Setup)
# ==========================================


@pytest.fixture
def mock_gen3_sub():
    """
    Creates a mock Gen3Submission instance.

    This prevents the tests from trying to actually connect to a Gen3
    commons and allows us to verify if specific methods (like
    delete_nodes) were called.

    Returns:
        MagicMock: A mock Gen3Submission instance.
    """
    return MagicMock()


# ==========================================
# Unit Tests — delete_project_metadata
# ==========================================


def test_delete_metadata_success(mock_gen3_sub):
    """
    Tests a successful metadata deletion workflow.

    The function receives nodes already in deletion order (reverse
    of import order). We pass ['case', 'sample'] and confirm with
    'yes'. delete_nodes should be called for 'case' first, then
    'sample'.

    Inputs:
        nodes: ["case", "sample"] (already reversed)
        prompt_for_confirmation: True, input: "yes"

    Expected Output:
        - delete_nodes called for 'case' then 'sample'.
    """
    with patch("builtins.input", return_value="yes"):
        delete_project_metadata(
            gen3_submission=mock_gen3_sub,
            program_id="program1",
            project_id="TEST-PROJ",
            nodes=["case", "sample"],
            prompt_for_confirmation=True,
        )

    expected_calls = [
        call("program1", "TEST-PROJ", ["case"]),
        call("program1", "TEST-PROJ", ["sample"]),
    ]
    mock_gen3_sub.delete_nodes.assert_has_calls(expected_calls)


def test_delete_metadata_cancelled(mock_gen3_sub):
    """
    Tests that deletion is aborted if the user does not confirm.

    If the user types anything other than 'yes', the delete_nodes
    method should never be called.

    Inputs:
        prompt_for_confirmation: True, input: "no"

    Expected Output:
        - delete_nodes is NOT called.
    """
    with patch("builtins.input", return_value="no"):
        delete_project_metadata(
            gen3_submission=mock_gen3_sub,
            program_id="program1",
            project_id="TEST-PROJ",
            nodes=["sample"],
            prompt_for_confirmation=True,
        )

    mock_gen3_sub.delete_nodes.assert_not_called()


def test_delete_metadata_empty_nodes(mock_gen3_sub, caplog):
    """
    Tests that an empty node list is handled gracefully.

    If no nodes are provided (e.g. all were excluded), the function
    should log an info message and return without calling
    delete_nodes.

    Inputs:
        nodes: []

    Expected Output:
        - delete_nodes is NOT called.
        - "No nodes provided" is logged.
    """
    with caplog.at_level(logging.INFO):
        delete_project_metadata(
            gen3_submission=mock_gen3_sub,
            program_id="program1",
            project_id="TEST-PROJ",
            nodes=[],
        )

    mock_gen3_sub.delete_nodes.assert_not_called()
    assert "No nodes provided" in caplog.text


def test_delete_metadata_api_error(mock_gen3_sub, caplog):
    """
    Tests how the code handles an error from the Gen3 API.

    If the API fails to delete a node, the script should not crash.
    It catches the Exception and logs a [FAILED] message, then
    continues to the next node.

    Inputs:
        Gen3 API: raises an Exception during deletion.

    Expected Output:
        - The error is logged at the ERROR level.
        - The script doesn't crash.
    """
    mock_gen3_sub.delete_nodes.side_effect = Exception(
        "API Timeout"
    )

    with caplog.at_level(logging.ERROR):
        delete_project_metadata(
            gen3_submission=mock_gen3_sub,
            program_id="program1",
            project_id="TEST-PROJ",
            nodes=["sample"],
            prompt_for_confirmation=False,
        )

    assert "[FAILED]" in caplog.text
    assert "API Timeout" in caplog.text


# ==========================================
# Tests for query_metadata_upload_guids
# ==========================================

DELETER_PATH = "g3dt.upload.metadata_deleter"


@patch(f"{DELETER_PATH}.AthenaQuery")
@patch(f"{DELETER_PATH}.AthenaConfig")
def test_query_metadata_upload_guids_with_node(
    mock_config_cls, mock_query_cls
):
    """
    Tests that query_metadata_upload_guids correctly builds an SQL
    query with the compound project_id, version, api_endpoint, and
    a specific node filter.

    Background:
        The metadata_upload_iceberg table stores records of every
        metadata submission to Gen3. When deleting by node order,
        the function must filter by node so that only records for
        a specific node type (e.g. 'subject') are returned.

    Inputs:
        database: "test_db"
        table: "test_table"
        project_id: "program1-CDAH"
        api_endpoint: "https://example.com/api/v0"
        version: "0.8.1"
        node: "subject"

    Expected Output:
        - A DataFrame with 2 rows containing gen3_guid values.
        - The SQL query contains the node filter.
    """
    expected_df = pd.DataFrame({
        "gen3_guid": ["uuid-1", "uuid-2"],
        "project_id": ["program1-CDAH", "program1-CDAH"],
        "version": ["0.8.1", "0.8.1"],
    })
    mock_athena = mock_query_cls.return_value
    mock_athena.query_athena.return_value = expected_df

    result = query_metadata_upload_guids(
        database="test_db",
        table="test_table",
        project_id="program1-CDAH",
        api_endpoint="https://example.com/api/v0",
        version="0.8.1",
        athena_s3_output="s3://test-bucket/output/",
        node="subject",
    )

    assert len(result) == 2
    assert list(result["gen3_guid"]) == ["uuid-1", "uuid-2"]

    called_sql = mock_athena.query_athena.call_args[1]["sql"]
    assert "program1-CDAH" in called_sql
    assert "0.8.1" in called_sql
    assert "https://example.com/api/v0" in called_sql
    assert "node = 'subject'" in called_sql


@patch(f"{DELETER_PATH}.AthenaQuery")
@patch(f"{DELETER_PATH}.AthenaConfig")
def test_query_metadata_upload_guids_without_node(
    mock_config_cls, mock_query_cls
):
    """
    Tests that query_metadata_upload_guids does NOT include a node
    filter in the SQL when the node parameter is omitted.

    Background:
        When no specific node is targeted, the query should return
        all records matching project_id, version, and api_endpoint
        regardless of node type. The SQL should not contain any
        'AND node =' clause.

    Inputs:
        node: None (omitted)

    Expected Output:
        - The SQL query does NOT contain 'AND node ='.
        - Results are returned normally.
    """
    expected_df = pd.DataFrame({
        "gen3_guid": ["uuid-1"],
    })
    mock_athena = mock_query_cls.return_value
    mock_athena.query_athena.return_value = expected_df

    result = query_metadata_upload_guids(
        database="test_db",
        table="test_table",
        project_id="program1-CDAH",
        api_endpoint="https://example.com/api/v0",
        version="0.8.1",
        athena_s3_output="s3://test-bucket/output/",
    )

    assert len(result) == 1
    called_sql = mock_athena.query_athena.call_args[1]["sql"]
    assert "AND node =" not in called_sql


@patch(f"{DELETER_PATH}.AthenaQuery")
@patch(f"{DELETER_PATH}.AthenaConfig")
def test_query_metadata_upload_guids_empty(
    mock_config_cls, mock_query_cls
):
    """
    Tests that query_metadata_upload_guids handles an empty result
    from Athena gracefully.

    Background:
        If no records match the given project_id, version, and
        api_endpoint, Athena returns an empty DataFrame. The function
        should return this empty DataFrame without error so the caller
        can decide what to do (e.g. log "nothing to delete").

    Inputs:
        Athena returns an empty DataFrame.

    Expected Output:
        - An empty DataFrame is returned.
    """
    mock_athena = mock_query_cls.return_value
    mock_athena.query_athena.return_value = pd.DataFrame()

    result = query_metadata_upload_guids(
        database="test_db",
        table="test_table",
        project_id="program1-CDAH",
        api_endpoint="https://example.com/api/v0",
        version="0.8.1",
        athena_s3_output="s3://test-bucket/output/",
    )

    assert result.empty


# ==========================================
# Tests for delete_records_by_guid
# ==========================================


def _make_mock_sub(endpoint="https://example.com"):
    """
    Helper to create a mock Gen3Submission with ._endpoint
    and ._auth_provider attributes set.
    """
    mock_sub = MagicMock()
    mock_sub._endpoint = endpoint
    mock_sub._auth_provider = MagicMock()
    return mock_sub


def test_delete_records_by_guid_success(caplog):
    """
    Tests that delete_records_by_guid calls delete_record once
    per UUID and logs the correct batch progress.

    Background:
        The function iterates over UUIDs one at a time, calling
        gen3_submission.delete_record() for each. UUIDs are
        grouped into batches only for rate-limiting pauses.

    Inputs:
        uuids: ["uuid-1", "uuid-2"]
        delete_record returns successfully for both.

    Expected Output:
        - delete_record is called twice (once per UUID).
        - The summary log shows 2 successful, 0 failed.
    """
    mock_sub = _make_mock_sub()
    mock_sub.delete_record.return_value = {"success": True}

    with caplog.at_level(logging.INFO):
        delete_records_by_guid(
            gen3_submission=mock_sub,
            program_id="program1",
            project_id="CDAH",
            uuids=["uuid-1", "uuid-2"],
        )

    assert mock_sub.delete_record.call_count == 2
    assert "Successful: 2, Failed: 0" in caplog.text


def test_delete_records_by_guid_failure_continues(caplog):
    """
    Tests that when delete_record raises an exception for one
    UUID, the function logs a warning and continues deleting
    the remaining UUIDs.

    Background:
        The Gen3 SDK raises an exception (e.g. HTTPError) when
        a record cannot be deleted. The function should catch
        the error, log the failed UUID, and proceed with the
        next UUID so that one failure does not block the rest.

    Inputs:
        uuids: ["uuid-1", "uuid-2"]
        delete_record raises Exception for "uuid-1",
        succeeds for "uuid-2".

    Expected Output:
        - delete_record is called twice.
        - A warning is logged containing "uuid-1".
        - The summary shows 1 successful, 1 failed.
    """
    mock_sub = _make_mock_sub()
    mock_resp = MagicMock()
    mock_resp.json.return_value = {
        "code": 400,
        "message": "Deletion transaction failed.",
        "entities": [{
            "errors": [{
                "id": "uuid-1",
                "message": "Entity not found.",
            }],
        }],
    }
    error = Exception("400 Client Error")
    error.response = mock_resp
    mock_sub.delete_record.side_effect = [
        error,
        {"success": True},
    ]

    with caplog.at_level(logging.INFO):
        delete_records_by_guid(
            gen3_submission=mock_sub,
            program_id="program1",
            project_id="CDAH",
            uuids=["uuid-1", "uuid-2"],
        )

    assert mock_sub.delete_record.call_count == 2
    assert "uuid-1" in caplog.text
    assert "400" in caplog.text
    assert "Entity not found." in caplog.text
    assert "Successful: 1" in caplog.text
    assert "Failed: 1" in caplog.text


def test_delete_records_by_guid_empty_list(caplog):
    """
    Tests that passing an empty UUID list results in an early
    return with no delete_record calls made.

    Background:
        If the Athena query returns no matching records, the
        UUID list will be empty. The function should detect
        this and skip all deletion logic.

    Inputs:
        uuids: [] (empty list)

    Expected Output:
        - delete_record is NOT called.
        - An info log about skipping is emitted.
    """
    mock_sub = _make_mock_sub()

    with caplog.at_level(logging.INFO):
        delete_records_by_guid(
            gen3_submission=mock_sub,
            program_id="program1",
            project_id="CDAH",
            uuids=[],
        )

    mock_sub.delete_record.assert_not_called()
    assert "No UUIDs provided" in caplog.text


# ==========================================
# Worker script — version-not-found / --skip-if-empty behaviour
# ==========================================
#
# These exercise the packaged per-study worker
# (src/g3dt/services/delete/delete_metadata_by_guid.py) the bulk
# `g3dt delete metadata` loop calls. The script is data, not an importable
# module, so we load it from its packaged path and run main() with every
# AWS/Gen3/Athena dependency mocked and Athena returning no rows.

_WORKER_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src" / "g3dt" / "services" / "delete" / "delete_metadata_by_guid.py"
)


def _load_worker():
    """Import the delete-by-guid worker script as a fresh module object."""
    spec = importlib.util.spec_from_file_location(
        "delete_metadata_by_guid_under_test", _WORKER_PATH
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _run_worker_no_data(mod, argv, monkeypatch):
    """Run the worker's main() with Athena returning an empty result.

    All external dependencies (SSM-backed config, boto3, Gen3 auth, Athena) are
    stubbed so the only thing under test is how the worker reacts when no
    records match the requested version. ``--node subject`` is used so it
    processes a single node without reading DataImportOrder from disk.
    """
    from g3dt.config import EnvConfig, StudyConfig

    env_cfg = EnvConfig(
        name="staging", is_ec2=False, region="ap-southeast-2",
        dictionary_version="v1", aws_profile=None, aws_secret_name="sec",
        schema_s3_uri="u", domain="d", app_name="a", namespace="n",
        cluster_name="c", schema_repo="Org/schema-repo",
    )
    study_cfg = StudyConfig(
        key="ausdiab_staging", project_id="AusDiab", program_id="program1",
        s3_metadata_path="s3://b/staging/ausdiab/",
    )
    rc = MagicMock()
    rc.metadata_db = "db"
    rc.athena_output_location = "s3://o/"
    rc.athena_workgroup = "primary"
    monkeypatch.setattr(mod.g3dt_config, "resolve_env", lambda *a, **k: env_cfg)
    monkeypatch.setattr(mod.g3dt_config, "resolve_study", lambda *a, **k: study_cfg)
    monkeypatch.setattr(mod.g3dt_config, "require_project", lambda *a, **k: "etl")
    monkeypatch.setattr(mod.resolver, "resolve", lambda *a, **k: rc)
    monkeypatch.setattr(mod, "create_boto3_session", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        mod, "get_gen3_api_key_aws_secret", lambda *a, **k: {"api_key": "jwt"}
    )
    monkeypatch.setattr(
        mod, "infer_api_endpoint_from_jwt", lambda *a, **k: "https://api"
    )
    monkeypatch.setattr(
        mod, "create_gen3_submission_class", lambda *a, **k: MagicMock()
    )
    # The version's records don't exist -> Athena returns an empty DataFrame.
    monkeypatch.setattr(
        mod, "query_metadata_upload_guids", lambda *a, **k: pd.DataFrame()
    )
    monkeypatch.setattr(sys, "argv", argv)
    mod.main()


def test_worker_skips_with_exit_code_when_no_data_and_flag_set(monkeypatch, caplog):
    """
    Tests that --skip-if-empty turns 'no data at this version' into a skip.

    Background:
        In the bulk delete loop every requested study is processed, even ones
        with nothing at the target version. The worker signals "nothing here,
        skip me" by exiting with SKIP_EXIT_CODE so the loop continues to the
        next study instead of recording a failure.

    Inputs:
        --version 9.9.9 (no records), --skip-if-empty set.

    Expected Output:
        - main() exits with SKIP_EXIT_CODE (3).
        - The actionable "data version not found / data_version property" hint is
          logged so the operator knows why nothing matched.
    """
    mod = _load_worker()
    argv = [
        "delete_metadata_by_guid.py", "--study", "ausdiab", "--env", "staging",
        "--version", "9.9.9", "--node", "subject", "--skip-if-empty",
    ]
    with caplog.at_level(logging.WARNING):
        with pytest.raises(SystemExit) as exc:
            _run_worker_no_data(mod, argv, monkeypatch)

    assert exc.value.code == mod.SKIP_EXIT_CODE
    assert "data version" in caplog.text.lower()
    assert "data_version" in caplog.text


def test_worker_without_flag_completes_but_warns_on_no_data(monkeypatch, caplog):
    """
    Tests that without --skip-if-empty the standalone behaviour is unchanged.

    Background:
        Run directly (not from the bulk loop), a version with no records should
        not raise the skip exit — the script just finishes normally. The operator
        still gets the "data version not found" warning so a typo'd version or
        missing `data_version` property is obvious.

    Inputs:
        --version 9.9.9 (no records), no --skip-if-empty.

    Expected Output:
        - main() returns normally (no SystemExit).
        - The "data version not found / data_version property" warning is logged.
    """
    mod = _load_worker()
    argv = [
        "delete_metadata_by_guid.py", "--study", "ausdiab", "--env", "staging",
        "--version", "9.9.9", "--node", "subject",
    ]
    with caplog.at_level(logging.WARNING):
        _run_worker_no_data(mod, argv, monkeypatch)  # returns, no SystemExit

    assert "data version" in caplog.text.lower()
    assert "data_version" in caplog.text
