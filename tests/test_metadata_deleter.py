import importlib.util
import logging
import pathlib
import sys
from unittest.mock import MagicMock, patch, call
import pytest
import pandas as pd
from gen3.submission import Gen3SubmissionQueryError
from g3dt.upload.metadata_deleter import (
    delete_project_metadata,
    query_metadata_upload_guids,
    delete_records_by_guid,
    delete_node_records_by_property,
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


# ==========================================
# Unit Tests — delete_node_records_by_property (GraphQL, registry-free)
# ==========================================
#
# Synthetic uploads write no Athena receipts, so version-filtered deletion of
# synthetic data queries sheepdog GraphQL for records whose data_version
# property matches, then deletes by id — query -> delete -> re-query until the
# node is empty (the SDK's own delete_nodes pattern).


def _pages(*pages):
    """A query side-effect returning one prepared page per call."""
    it = iter(pages)

    def _side_effect(_query):
        return next(it)

    return _side_effect


def test_delete_node_records_by_property_query_shape_and_delete(mock_gen3_sub):
    """
    Tests the GraphQL filter shape and that every returned id is deleted.

    Background:
        Sheepdog's graphql filters on the COMPOUND project id
        (program-project) and accepts any declared node property as an extra
        argument. Getting either wrong silently matches zero records, which
        would read as "nothing to delete" — so the exact query text is pinned.

    Inputs:
        One page of two subject ids with data_version 0.9.8, then an empty
        page.

    Expected Output:
        - The query names the node, 'project_id: "program1-SynthP"', and
          'data_version: "0.9.8"'.
        - Both ids are deleted via delete_record; the function returns 2.
    """
    mock_gen3_sub.query.side_effect = _pages(
        {"data": {"subject": [{"id": "u1"}, {"id": "u2"}]}},
        {"data": {"subject": []}},
    )

    deleted = delete_node_records_by_property(
        gen3_submission=mock_gen3_sub,
        program_id="program1",
        project_id="SynthP",
        node="subject",
        property_name="data_version",
        property_value="0.9.8",
    )

    assert deleted == 2
    query_text = mock_gen3_sub.query.call_args_list[0].args[0]
    assert "subject (first: 100" in query_text
    assert 'project_id: "program1-SynthP"' in query_text
    assert 'data_version: "0.9.8"' in query_text
    deleted_ids = [
        c.args[2] for c in mock_gen3_sub.delete_record.call_args_list
    ]
    assert deleted_ids == ["u1", "u2"]


def test_delete_node_records_by_property_paginates_until_empty(mock_gen3_sub):
    """
    Tests that deletion keeps re-querying until the node has no matches left.

    Background:
        One query page holds at most page_size records, so a node with more
        matches than one page must be drained across rounds — stopping after
        the first page would leave records behind while reporting success.

    Inputs:
        Two non-empty pages (2 ids, then 1 new id), then an empty page.

    Expected Output:
        - Three queries issued; return value 3 (distinct ids deleted).
    """
    mock_gen3_sub.query.side_effect = _pages(
        {"data": {"subject": [{"id": "u1"}, {"id": "u2"}]}},
        {"data": {"subject": [{"id": "u3"}]}},
        {"data": {"subject": []}},
    )

    deleted = delete_node_records_by_property(
        gen3_submission=mock_gen3_sub,
        program_id="p",
        project_id="proj",
        node="subject",
        property_name="data_version",
        property_value="v1.3.0",
    )

    assert deleted == 3
    assert mock_gen3_sub.query.call_count == 3


def test_delete_node_records_by_property_raises_when_stuck(mock_gen3_sub):
    """
    Tests the no-progress guard.

    Background:
        delete_records_by_guid tolerates per-id failures by design, so if
        EVERY deletion in a round fails (e.g. a permissions problem), the next
        query returns the same page and the loop would spin forever. Seeing
        the same first id twice means no progress — abort loudly.

    Inputs:
        The same page returned on every query.

    Expected Output:
        - RuntimeError naming the node; no infinite loop.
    """
    page = {"data": {"subject": [{"id": "u1"}]}}
    mock_gen3_sub.query.side_effect = _pages(page, page)

    with pytest.raises(RuntimeError, match="subject"):
        delete_node_records_by_property(
            gen3_submission=mock_gen3_sub,
            program_id="p",
            project_id="proj",
            node="subject",
            property_name="data_version",
            property_value="v1.3.0",
        )


def test_delete_node_records_by_property_propagates_query_error(mock_gen3_sub):
    """
    Tests that a GraphQL rejection reaches the caller untouched.

    Background:
        A node whose schema does not declare the filter property makes
        sheepdog return a graphql error (Gen3SubmissionQueryError). Whether
        that is tolerable is a per-node policy decision that belongs to the
        worker loop — the library function must not swallow it.

    Inputs:
        query raises Gen3SubmissionQueryError.

    Expected Output:
        - The same exception type propagates; nothing was deleted.
    """
    mock_gen3_sub.query.side_effect = Gen3SubmissionQueryError("unknown argument")

    with pytest.raises(Gen3SubmissionQueryError):
        delete_node_records_by_property(
            gen3_submission=mock_gen3_sub,
            program_id="p",
            project_id="proj",
            node="subject",
            property_name="data_version",
            property_value="v1.3.0",
        )
    mock_gen3_sub.delete_record.assert_not_called()


# ==========================================
# Worker script — delete_synth_metadata_by_version.py (registry-free)
# ==========================================

_SYNTH_WORKER_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src" / "g3dt" / "services" / "delete"
    / "delete_synth_metadata_by_version.py"
)

_ALL_WORKER_PATH = (
    pathlib.Path(__file__).resolve().parents[1]
    / "src" / "g3dt" / "services" / "delete"
    / "delete_all_metadata_for_project.py"
)


def _load_script(path, name):
    """Import a packaged worker script as a fresh module object."""
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _stub_synth_worker_env(mod, monkeypatch):
    """Stub the synth worker's config + auth chain (no AWS, no Gen3).

    resolve_study is patched to RAISE: the whole point of this worker is that
    synthetic projects are not registered studies, so any registry lookup is
    a regression.
    """
    from g3dt.config import EnvConfig

    env_cfg = EnvConfig(
        name="test", is_ec2=False, region="ap-southeast-2",
        dictionary_version="v1", aws_profile=None, aws_secret_name="sec",
        schema_s3_uri="u", domain="d", app_name="a", namespace="n",
        cluster_name="c", schema_repo="Org/schema-repo",
    )
    monkeypatch.setattr(mod.g3dt_config, "resolve_env", lambda *a, **k: env_cfg)
    monkeypatch.setattr(
        mod.g3dt_config, "resolve_study",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("registry lookup in registry-free worker")
        ),
        raising=False,
    )
    monkeypatch.setattr(mod, "create_boto3_session", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        mod, "get_gen3_api_key_aws_secret", lambda *a, **k: {"api_key": "jwt"}
    )
    monkeypatch.setattr(
        mod, "create_gen3_submission_class", lambda *a, **k: MagicMock()
    )


def test_synth_version_worker_never_touches_study_registry(monkeypatch):
    """
    Tests that the version worker is genuinely registry-free.

    Background:
        The original failure this feature fixes was 'Study not found — no
        studies are registered' for synthetic projects. The worker takes
        --study as the Gen3 project code itself; if anyone reintroduces a
        resolve_study call, this test's poisoned stub trips immediately.

    Inputs:
        --study synthetic_dataset_1 --version v1.3.0 --node subject, with
        resolve_study patched to raise and the deleter reporting 2 records.

    Expected Output:
        - main() completes normally; the deleter got the raw project id and
          verbatim version.
    """
    mod = _load_script(_SYNTH_WORKER_PATH, "synth_worker_registry_free")
    _stub_synth_worker_env(mod, monkeypatch)
    calls = []

    def fake_delete(**kwargs):
        calls.append(kwargs)
        return 2

    monkeypatch.setattr(mod, "delete_node_records_by_property", fake_delete)
    monkeypatch.setattr(sys, "argv", [
        "delete_synth_metadata_by_version.py", "--study", "synthetic_dataset_1",
        "--env", "test", "--version", "v1.3.0", "--node", "subject",
    ])

    mod.main()  # returns; no SystemExit

    assert len(calls) == 1
    assert calls[0]["project_id"] == "synthetic_dataset_1"
    assert calls[0]["program_id"] == "program1"
    assert calls[0]["property_name"] == "data_version"
    assert calls[0]["property_value"] == "v1.3.0"


def test_synth_version_worker_tolerates_per_node_query_error(
    monkeypatch, tmp_path, caplog
):
    """
    Tests per-node tolerance of GraphQL rejections.

    Background:
        Dictionaries adopt the data_version property node by node; a node
        whose schema lacks it makes the query error. That must skip THAT node
        with a warning and keep deleting the others — otherwise one lagging
        node blocks the whole cleanup.

    Inputs:
        Two nodes via an import-order file; the first node's delete raises
        Gen3SubmissionQueryError, the second deletes 2 records.

    Expected Output:
        - main() completes (no SystemExit); warning names the failing node.
    """
    mod = _load_script(_SYNTH_WORKER_PATH, "synth_worker_node_tolerance")
    _stub_synth_worker_env(mod, monkeypatch)
    order = tmp_path / "DataImportOrder.txt"
    order.write_text("lab_result\nsubject\n")  # reversed -> subject first

    def fake_delete(**kwargs):
        if kwargs["node"] == "subject":
            raise Gen3SubmissionQueryError("unknown argument data_version")
        return 2

    monkeypatch.setattr(mod, "delete_node_records_by_property", fake_delete)
    monkeypatch.setattr(sys, "argv", [
        "delete_synth_metadata_by_version.py", "--study", "s1",
        "--env", "test", "--version", "v1.3.0",
        "--import-order", str(order),
    ])

    with caplog.at_level(logging.WARNING):
        mod.main()  # returns; the query error did not become a failure

    assert "subject" in caplog.text
    assert "data_version" in caplog.text


def test_synth_version_worker_zero_total_exits_skip_with_hint(
    monkeypatch, caplog
):
    """
    Tests the nothing-matched outcome.

    Background:
        Batches generated before data_version stamping existed carry no
        version property, so a versioned delete finds nothing. The bulk loop
        must count that as a skip (exit 3), and the operator needs the hint
        naming the data_version property — not a silent clean-looking run.

    Inputs:
        --skip-if-empty with the deleter reporting 0 records.

    Expected Output:
        - SystemExit with code 3; the data_version hint is logged.
    """
    mod = _load_script(_SYNTH_WORKER_PATH, "synth_worker_zero_total")
    _stub_synth_worker_env(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "delete_node_records_by_property", lambda **k: 0
    )
    monkeypatch.setattr(sys, "argv", [
        "delete_synth_metadata_by_version.py", "--study", "s1",
        "--env", "test", "--version", "v9.9.9", "--node", "subject",
        "--skip-if-empty",
    ])

    with caplog.at_level(logging.WARNING):
        with pytest.raises(SystemExit) as exc:
            mod.main()

    assert exc.value.code == mod.SKIP_EXIT_CODE
    assert "data_version" in caplog.text


def test_delete_all_worker_synthetic_mode_uses_raw_study_as_project(monkeypatch):
    """
    Tests the whole-project worker's registry-free synthetic mode.

    Background:
        'delete metadata --synthetic' (bare names -> version all) routes here.
        With --synthetic the worker must not consult the study registry —
        --study is the Gen3 project code and --program-id supplies the
        program that synthetic uploads default to.

    Inputs:
        --synthetic --program-id p2 --study synthetic_dataset_1 --node subject,
        resolve_study patched to raise.

    Expected Output:
        - delete_project_metadata called with project_id='synthetic_dataset_1'
          and program_id='p2'.
    """
    mod = _load_script(_ALL_WORKER_PATH, "delete_all_worker_synthetic")
    from g3dt.config import EnvConfig

    env_cfg = EnvConfig(
        name="test", is_ec2=False, region="ap-southeast-2",
        dictionary_version="v1", aws_profile=None, aws_secret_name="sec",
        schema_s3_uri="u", domain="d", app_name="a", namespace="n",
        cluster_name="c", schema_repo="Org/schema-repo",
    )
    monkeypatch.setattr(mod.g3dt_config, "resolve_env", lambda *a, **k: env_cfg)
    monkeypatch.setattr(
        mod.g3dt_config, "resolve_study",
        lambda *a, **k: (_ for _ in ()).throw(
            AssertionError("registry lookup in --synthetic mode")
        ),
    )
    monkeypatch.setattr(mod, "create_boto3_session", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        mod, "get_gen3_api_key_aws_secret", lambda *a, **k: {"api_key": "jwt"}
    )
    monkeypatch.setattr(
        mod, "create_gen3_submission_class", lambda *a, **k: MagicMock()
    )
    captured = {}
    monkeypatch.setattr(
        mod, "delete_project_metadata", lambda **k: captured.update(k)
    )
    monkeypatch.setattr(sys, "argv", [
        "delete_all_metadata_for_project.py", "--study", "synthetic_dataset_1",
        "--env", "test", "--synthetic", "--program-id", "p2",
        "--node", "subject",
    ])

    mod.main()

    assert captured["project_id"] == "synthetic_dataset_1"
    assert captured["program_id"] == "p2"


# ==========================================
# Worker scripts — import-order resolution (no more CWD crash)
# ==========================================
#
# Historically every worker defaulted --import-order to 'DataImportOrder.txt'
# in the CWD and crashed with FileNotFoundError anywhere else. The node order
# now comes from g3dt.import_order.resolve_import_order (explicit flag ->
# release bucket -> cwd -> derive from the dictionary); these tests patch that
# resolver on each loaded worker module and pin the regression plus the
# study_cfg wiring that makes the release-bucket step reachable (or not).


def _resolver_stub(calls, nodes=("project", "subject", "lab_result")):
    def _resolve(**kwargs):
        calls.append(kwargs)
        return list(nodes), "stub source"
    return _resolve


def test_delete_all_worker_without_cwd_file_resolves_order(monkeypatch, tmp_path):
    """THE regression: no cwd file, no --node -> resolver runs, no crash.

    The original failure: `g3dt delete metadata ... --synthetic` (bare names
    -> version all) crashed with FileNotFoundError('DataImportOrder.txt')
    because the operator's shell was not in a directory holding that file.
    In --synthetic mode the resolver must receive study_cfg=None (registry-
    free), and the resolved submission order must be filtered + reversed.
    """
    mod = _load_script(_ALL_WORKER_PATH, "delete_all_worker_order_regression")
    from g3dt.config import EnvConfig

    env_cfg = EnvConfig(
        name="test", is_ec2=False, region="ap-southeast-2",
        dictionary_version="v1", aws_profile=None, aws_secret_name="sec",
        schema_s3_uri="u", domain="d", app_name="a", namespace="n",
        cluster_name="c", schema_repo="Org/schema-repo",
    )
    monkeypatch.setattr(mod.g3dt_config, "resolve_env", lambda *a, **k: env_cfg)
    monkeypatch.setattr(mod, "create_boto3_session", lambda *a, **k: MagicMock())
    monkeypatch.setattr(
        mod, "get_gen3_api_key_aws_secret", lambda *a, **k: {"api_key": "jwt"}
    )
    monkeypatch.setattr(
        mod, "create_gen3_submission_class", lambda *a, **k: MagicMock()
    )
    captured = {}
    monkeypatch.setattr(
        mod, "delete_project_metadata", lambda **k: captured.update(k)
    )
    calls = []
    monkeypatch.setattr(mod, "resolve_import_order", _resolver_stub(calls))
    monkeypatch.chdir(tmp_path)  # guaranteed: no DataImportOrder.txt here
    monkeypatch.setattr(sys, "argv", [
        "delete_all_metadata_for_project.py", "--study", "synthetic_dataset_1",
        "--env", "test", "--synthetic",
    ])

    mod.main()  # must not raise FileNotFoundError

    assert calls[0]["study_cfg"] is None  # registry-free mode
    # 'project' filtered by EXCLUDE_NODES, remainder reversed.
    assert captured["nodes"] == ["lab_result", "subject"]


def test_guid_worker_passes_study_cfg_to_resolver(monkeypatch, tmp_path):
    """The registered-study worker hands its StudyConfig to the resolver.

    That is what makes the release-bucket step reachable: without it, the
    resolver could never find the DataImportOrder.txt the release ships.
    """
    import pandas as pd

    mod = _load_worker()
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
    monkeypatch.setattr(
        mod, "query_metadata_upload_guids", lambda *a, **k: pd.DataFrame()
    )
    calls = []
    monkeypatch.setattr(mod, "resolve_import_order", _resolver_stub(calls))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "delete_metadata_by_guid.py", "--study", "ausdiab", "--env", "staging",
        "--version", "9.9.9",
    ])

    mod.main()  # empty Athena results -> completes with the not-found warning

    assert calls[0]["study_cfg"] is study_cfg


def test_synth_version_worker_resolves_order_registry_free(monkeypatch, tmp_path):
    """The synthetic version worker resolves with study_cfg=None, no crash."""
    mod = _load_script(_SYNTH_WORKER_PATH, "synth_worker_order_regression")
    _stub_synth_worker_env(mod, monkeypatch)
    monkeypatch.setattr(
        mod, "delete_node_records_by_property", lambda **k: 1
    )
    calls = []
    monkeypatch.setattr(mod, "resolve_import_order", _resolver_stub(calls))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", [
        "delete_synth_metadata_by_version.py", "--study", "s1",
        "--env", "test", "--version", "v1.3.0",
    ])

    mod.main()

    assert calls[0]["study_cfg"] is None


def test_worker_rejects_import_order_with_dict_version(monkeypatch, tmp_path):
    """--import-order and --dict-version together is a usage error (exit 2).

    They name contradictory sources for a destructive run; picking one
    silently would hide an operator mistake.
    """
    mod = _load_script(_ALL_WORKER_PATH, "delete_all_worker_flag_conflict")
    monkeypatch.setattr(sys, "argv", [
        "delete_all_metadata_for_project.py", "--study", "s", "--env", "test",
        "--import-order", str(tmp_path / "o.txt"), "--dict-version", "v1.0.0",
    ])
    with pytest.raises(SystemExit) as exc:
        mod.main()
    assert exc.value.code == 2
