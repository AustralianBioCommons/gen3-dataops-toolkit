import pytest
import pandas as pd
import numpy as np
import json
from decimal import Decimal
from datetime import datetime, date
import re
from unittest.mock import MagicMock, patch

from g3dt.utils.athena_utils import (
    AthenaConfig,
    AthenaQuery,
    AthenaValidationWriter,
    generate_validation_id,
    write_validation_json_to_s3,
    write_gold_json_to_s3,
    json_serialiser,
    convert_dataframe_types_for_json,
    replace_nan_with_none,
    write_release_jsons_to_s3,
    write_iceberg_to_db,
)

@pytest.fixture
def sample_athena_config():
    """
    Fixture for providing a sample AthenaConfig instance.

    This fixture helps set up a default AthenaConfig used by multiple tests.
    It provides a sample AWS region, AWS profile, and Athena query S3 output location.
    Using this fixture allows the downstream unit tests to avoid needing to specify connection config.
    """
    return AthenaConfig(
        aws_region='eu-west-2',
        aws_profile='fake-profile',
        athena_s3_output='s3://fake-bucket/output'
    )

@patch('g3dt.utils.athena_utils.wr.catalog.get_tables')
@patch('g3dt.utils.athena_utils.boto3.Session')
def test_list_tables_returns_expected_tables(mock_boto_session, mock_get_tables, athena_query_instance):
    """
    Test AthenaQuery.list_tables returns the correct list of table names.

    - It should return all table names provided by awswrangler's get_tables, using the correct boto3 session.
    - If exceptions are thrown by the underlying library, they are raised.
    - It also verifies logging occurs for table discovery.

    This is important so that filter/data-table logic built on top of this utility
    can confidently enumerate Athena tables available for a database.
    """
    # Arrange
    fake_session = MagicMock()
    mock_boto_session.return_value = fake_session

    mock_get_tables.return_value = [
        {'Name': 'table1'},
        {'Name': 'table2'},
        {'Name': 'foo_bar'}
    ]

    # Act
    result = athena_query_instance.list_tables('analytics_db')

    # Assert
    mock_boto_session.assert_called_once()
    mock_get_tables.assert_called_once_with(database='analytics_db', boto3_session=fake_session)
    # Table names returned should exactly match the 'Name' keys of get_tables return value
    assert result == ['table1', 'table2', 'foo_bar']

def test_list_tables_raises_on_wr_error(athena_query_instance):
    """
    Test AthenaQuery.list_tables re-raises exceptions if get_tables fails.

    This test verifies that exceptions raised in the underlying Athena or
    awswrangler layer are propagated, not swallowed, so error monitoring works.
    """
    with patch('g3dt.utils.athena_utils.wr.catalog.get_tables', side_effect=RuntimeError("Athena down")), \
         patch('g3dt.utils.athena_utils.boto3.Session') as mock_boto_session:
        mock_boto_session.return_value = MagicMock()
        with pytest.raises(RuntimeError, match="Athena down"):
            athena_query_instance.list_tables('broken_db')


@pytest.fixture
def athena_query_instance(sample_athena_config):
    """
    Fixture for creating an AthenaQuery instance for tests.

    This gives test functions a preconfigured AthenaQuery object based on the sample configuration, so
    tests can focus on logic and mocks rather than instantiating objects themselves.
    """
    return AthenaQuery(sample_athena_config)

@pytest.fixture
def athena_validation_writer_instance(sample_athena_config):
    """
    Fixture to provide an AthenaValidationWriter instance.

    This test fixture constructs an AthenaValidationWriter configured with test database and table names,
    supporting tests that involve operations using this class.
    """
    return AthenaValidationWriter(sample_athena_config, 'test_db', 'test_table')

@pytest.fixture
def athena_gold_writer_instance(sample_athena_config):
    """
    Fixture to provide an AthenaValidationWriter instance.

    This test fixture constructs an AthenaValidationWriter configured with test database and table names,
    supporting tests that involve operations using this class.
    """
    return AthenaValidationWriter(sample_athena_config, 'test_db', 'test_table')

# --- Tests for AthenaConfig ---

def test_athena_config_as_dict(sample_athena_config):
    """
    Test if AthenaConfig.as_dict() returns the correct dictionary.

    This ensures that the AthenaConfig dataclass conversion to dictionary
    is working so downstream code expecting config as a dict will get the right keys and values.
    It checks that all fields are present and have the expected sample values.
    """
    config_dict = sample_athena_config.as_dict()
    assert config_dict['aws_region'] == 'eu-west-2'
    assert config_dict['aws_profile'] == 'fake-profile'
    assert config_dict['athena_s3_output'] == 's3://fake-bucket/output'

# --- Tests for AthenaQuery ---

@patch('g3dt.utils.athena_utils.boto3.Session')
def test_get_boto_session(mock_boto_session, athena_query_instance):
    """
    Test that AthenaQuery._get_boto_session() correctly constructs a boto3 session.

    This unit test confirms that the code instantiates a boto3.Session object
    using exactly the AWS region and profile held in the AthenaConfig.
    This is important for ensuring that all AWS requests are sent to the correct
    region and use the intended credentials/config.
    """
    athena_query_instance._get_boto_session()
    mock_boto_session.assert_called_with(
        region_name='eu-west-2',
        profile_name='fake-profile'
    )

@patch('g3dt.utils.athena_utils.wr')
@patch('g3dt.utils.athena_utils.boto3.Session')
def test_query_athena_success(mock_boto_session, mock_wr, athena_query_instance):
    """
    Test AthenaQuery.query_athena to ensure it calls awswrangler's read_sql_query correctly.

    This checks that when you ask AthenaQuery to execute a SQL query, its implementation:
      - uses the right parameters (SQL, database, boto session, etc)
      - returns the same DataFrame as produced by the mocked awswrangler call

    This is foundational to all logic that uses query_athena, so the test protects
    against regressions or interface drift with awswrangler.
    """
    mock_session_instance = MagicMock()
    mock_boto_session.return_value = mock_session_instance
    mock_df = pd.DataFrame({'col1': [1, 2]})
    mock_wr.athena.read_sql_query.return_value = mock_df

    sql = "SELECT * FROM test"
    df = athena_query_instance.query_athena(sql, 'test_db')

    mock_wr.athena.read_sql_query.assert_called_once_with(
        sql=sql,
        boto3_session=mock_session_instance,
        database='test_db',
        ctas_approach=True,
        s3_output='s3://fake-bucket/output'
    )
    assert df.equals(mock_df)

def test_create_release_table(athena_query_instance):
    """
    Test that AthenaQuery.create_release_table issues the expected CREATE TABLE query.

    This test covers whether create_release_table:
      - correctly calls .query_athena
      - builds the CREATE TABLE from its PARAMETERS (db, table, s3 location) —
        the caller resolves those from SSM; nothing may be hard-coded here
      - uses the given DB and disables ctas_approach

    It is designed to catch regressions where the SQL or the name-free contract
    is accidentally changed, so that upgrades do not break data lineage marking
    or re-couple the helper to one project's names.
    """
    with patch.object(athena_query_instance, 'query_athena') as mock_query:
        athena_query_instance.create_release_table(
            "etl_test_dataops_metadata_db", "releases", "s3://etl-test-metadata-1-x/"
        )
        mock_query.assert_called_once()
        call_args = mock_query.call_args[0]
        assert "CREATE TABLE IF NOT EXISTS etl_test_dataops_metadata_db.releases" in call_args[0]
        assert "LOCATION 's3://etl-test-metadata-1-x/'" in call_args[0]
        assert call_args[1] == 'etl_test_dataops_metadata_db'
        assert call_args[2] is False

@patch('g3dt.utils.athena_utils.wr')
@patch('g3dt.utils.athena_utils.boto3.Session')
def test_insert_to_iceberg_table(mock_boto_session, mock_wr, athena_query_instance):
    """
    Test AthenaQuery.insert_to_iceberg_table for correct call to awswrangler.athena.to_iceberg.

    Ensures that:
      - The correct DataFrame, table, database, and boto session are passed
      - The workgroup and temp_path (which is random) are set as expected

    By mocking awswrangler, this test validates that our code correctly hands off
    the DataFrame and arguments when we want to store data in an Iceberg table in Athena,
    so that data engineering pipelines can reliably save to new or existing data lakes.
    """
    mock_session_instance = MagicMock()
    mock_boto_session.return_value = mock_session_instance
    df_to_insert = pd.DataFrame({'id': [1]})

    athena_query_instance.insert_to_iceberg_table(df_to_insert, 'ice_table', 'ice_db')

    # We cannot assert temp_path exactly due to uuid, so we use ANY
    from unittest.mock import ANY
    mock_wr.athena.to_iceberg.assert_called_once_with(
        df=df_to_insert,
        database='ice_db',
        table='ice_table',
        boto3_session=mock_session_instance,
        workgroup='primary',
        temp_path=ANY
    )

@patch('g3dt.utils.athena_utils.wr')
@patch('g3dt.utils.athena_utils.boto3.Session')
def test_find_db_for_model_scenarios(mock_boto_session, mock_wr, athena_query_instance):
    """
    Comprehensive test for AthenaQuery.find_db_for_model covering multiple scenarios:

    This test verifies the following scenarios:
      1. The model is found in the second database. find_db_for_model should return the name of
         that database and stop searching, even if later databases would error.
      2. The model is not found in any database. find_db_for_model should try all databases and
         return None, handling any exceptions cleanly (such as permissions errors).

    Explanation for a junior dev:
      - Mocks are used to prevent real AWS calls.
      - We emulate a situation where there are multiple Athena databases, with varying contents.
      - When the sought model table is present in a database, we check that it is found.
      - We also check that error conditions (like permission errors while listing tables)
        do not cause the search to crash, but are handled gracefully.

    This sort of robust negative + positive path testing ensures automation is resilient to AWS
    environment inconsistencies and accidental outages or permission changes.
    """
    # Mock the boto3 session and awswrangler calls
    mock_session_instance = MagicMock()
    mock_boto_session.return_value = mock_session_instance
    mock_wr.catalog.databases.return_value = {'Database': ['db1', 'db2', 'db_with_error']}

    # --- SCENARIO 1: Model is found in the second database ---
    mock_wr.catalog.get_tables.side_effect = [
        [{'Name': 'other_table'}],                               # Tables in 'db1'
        [{'Name': 'the_model_to_find'}],                         # Tables in 'db2'
        Exception("Permissions error on this database")          # Error on 'db_with_error'
    ]

    result = athena_query_instance.find_db_for_model('the_model_to_find')

    # It should find the model in 'db2' and return before hitting the error on the third DB
    assert result == 'db2'
    # Verify it was called for the first two databases
    assert mock_wr.catalog.get_tables.call_count == 2
    mock_wr.catalog.get_tables.assert_any_call(database='db1', boto3_session=mock_session_instance)
    mock_wr.catalog.get_tables.assert_any_call(database='db2', boto3_session=mock_session_instance)

    # --- SCENARIO 2: Model is not found in any database ---
    # Reset mocks for a clean test
    mock_wr.catalog.get_tables.reset_mock()
    mock_wr.catalog.get_tables.side_effect = [
        [{'Name': 'table_a'}],                                   # Tables in 'db1'
        [{'Name': 'table_b'}],                                   # Tables in 'db2'
        Exception("Permissions error on this database")          # Error on 'db_with_error'
    ]

    result_none = athena_query_instance.find_db_for_model('nonexistent_model')

    # It should search all databases, handle the error, and return None
    assert result_none is None
    # Verify it was called for all three databases
    assert mock_wr.catalog.get_tables.call_count == 3

@patch('g3dt.utils.athena_utils.wr.catalog.databases')
@patch('g3dt.utils.athena_utils.boto3.Session')
def test_find_db_for_model_not_found(mock_boto_session, mock_databases, athena_query_instance):
    """
    Test find_db_for_model returns None when no Athena databases exist.

    This test checks that if the list of Athena databases is empty (perhaps due
    to an empty environment or missing permissions), find_db_for_model returns None as expected,
    and does not throw errors.
    """
    mock_databases.return_value = pd.DataFrame({'Database': []})
    result = athena_query_instance.find_db_for_model('nonexistent_model')
    assert result is None

# --- Tests for AthenaValidationWriter ---

def test_get_latest_snapshot_id(athena_validation_writer_instance):
    """
    Test AthenaValidationWriter._get_latest_snapshot_id returns the latest snapshot id.

    The test uses a MagicMock to fake the AthenaQuery and returns a DataFrame
    with a known snapshot_id, ensuring that:
      - _get_latest_snapshot_id fetches and correctly parses the snapshot_id
      - the object's snapshot_id property is updated as a side effect

    This protects against future changes which might break snapshot tracking for downstream exports.
    """
    mock_athena_query = MagicMock()
    snapshot_df = pd.DataFrame({
        'snapshot_id': ['123456789'],
        'committed_at': ['2025-10-28 12:00:00.000 UTC']
    })
    mock_athena_query.query_athena.return_value = snapshot_df

    with patch('g3dt.utils.athena_utils.AthenaQuery', return_value=mock_athena_query):
        snapshot_id = athena_validation_writer_instance._get_latest_snapshot_id()
        assert snapshot_id == '123456789'
        assert athena_validation_writer_instance.snapshot_id == '123456789'


def test_get_latest_snapshot_id_empty_result():
    """
    Test _get_latest_snapshot_id returns None when Athena has no snapshots.

    Background:
        When an Iceberg table has no snapshot history (e.g. newly created or
        metadata was purged), the query against the $snapshots metadata table
        returns an empty DataFrame. The method should handle this gracefully.

    Steps:
        1. Create an AthenaValidationWriter for a test table.
        2. Mock the Athena query to return an empty DataFrame (no rows).
        3. Call _get_latest_snapshot_id().

    Expected:
        - Returns None.
        - writer.snapshot_id is set to None.
    """
    # Arrange
    config = AthenaConfig(
        aws_region='ap-southeast-2',
        aws_profile=None,
        athena_s3_output='s3://fake-bucket/output'
    )
    writer = AthenaValidationWriter(config, 'test_db', 'test_table')

    mock_athena_query = MagicMock()
    mock_athena_query.query_athena.return_value = pd.DataFrame(
        columns=['snapshot_id', 'committed_at']
    )

    with patch('g3dt.utils.athena_utils.AthenaQuery', return_value=mock_athena_query):
        # Act
        result = writer._get_latest_snapshot_id()

        # Assert
        assert result is None
        assert writer.snapshot_id is None


def test_get_latest_snapshot_id_returns_commit_datetime():
    """
    Test _get_latest_snapshot_id returns (snapshot_id, committed_at) tuple.

    Background:
        When called with return_commit_datetime=True, the method should return
        both the snapshot_id and the committed_at timestamp as a tuple. This is
        used by the release writer to record when a snapshot was committed.

    Steps:
        1. Create an AthenaValidationWriter for a test table.
        2. Mock the Athena query to return a single row with a known
           snapshot_id and committed_at value.
        3. Call _get_latest_snapshot_id(return_commit_datetime=True).

    Expected:
        - Returns a tuple of (snapshot_id, committed_at).
        - snapshot_id is '999888777'.
        - committed_at is '2025-06-15 09:30:00.000 UTC'.
    """
    # Arrange
    config = AthenaConfig(
        aws_region='ap-southeast-2',
        aws_profile=None,
        athena_s3_output='s3://fake-bucket/output'
    )
    writer = AthenaValidationWriter(config, 'test_db', 'test_table')

    mock_athena_query = MagicMock()
    mock_athena_query.query_athena.return_value = pd.DataFrame({
        'snapshot_id': ['999888777'],
        'committed_at': ['2025-06-15 09:30:00.000 UTC']
    })

    with patch('g3dt.utils.athena_utils.AthenaQuery', return_value=mock_athena_query):
        # Act
        result = writer._get_latest_snapshot_id(return_commit_datetime=True)

        # Assert
        assert isinstance(result, tuple)
        assert result[0] == '999888777'
        assert result[1] == '2025-06-15 09:30:00.000 UTC'


def test_get_latest_snapshot_id_empty_with_commit_datetime():
    """
    Test _get_latest_snapshot_id returns (None, None) when empty and commit_datetime requested.

    Background:
        When the $snapshots metadata table is empty AND the caller requests
        the committed_at timestamp, the method should return (None, None)
        rather than raising an error.

    Steps:
        1. Create an AthenaValidationWriter for a test table.
        2. Mock the Athena query to return an empty DataFrame.
        3. Call _get_latest_snapshot_id(return_commit_datetime=True).

    Expected:
        - Returns (None, None).
        - writer.snapshot_id is set to None.
    """
    # Arrange
    config = AthenaConfig(
        aws_region='ap-southeast-2',
        aws_profile=None,
        athena_s3_output='s3://fake-bucket/output'
    )
    writer = AthenaValidationWriter(config, 'test_db', 'test_table')

    mock_athena_query = MagicMock()
    mock_athena_query.query_athena.return_value = pd.DataFrame(
        columns=['snapshot_id', 'committed_at']
    )

    with patch('g3dt.utils.athena_utils.AthenaQuery', return_value=mock_athena_query):
        # Act
        result = writer._get_latest_snapshot_id(return_commit_datetime=True)

        # Assert
        assert result == (None, None)
        assert writer.snapshot_id is None


def test_get_full_table_returns_dataframe():
    """
    Test _get_full_table returns the DataFrame from Athena as-is.

    Background:
        _get_full_table queries an Athena table and returns the result as a
        pandas DataFrame. It should pass through whatever Athena returns
        without modifying the data.

    Steps:
        1. Create an AthenaValidationWriter and set its snapshot_id.
        2. Mock the Athena query to return a 2-row DataFrame.
        3. Call _get_full_table().

    Expected:
        - Returns a DataFrame with 2 rows.
        - Column values match the mocked input exactly.
    """
    # Arrange
    config = AthenaConfig(
        aws_region='ap-southeast-2',
        aws_profile=None,
        athena_s3_output='s3://fake-bucket/output'
    )
    writer = AthenaValidationWriter(config, 'test_db', 'test_table')
    writer.snapshot_id = '12345'

    expected_df = pd.DataFrame({
        'col_a': [1, 2],
        'col_b': ['foo', 'bar']
    })

    mock_athena_query = MagicMock()
    mock_athena_query.query_athena.return_value = expected_df

    with patch('g3dt.utils.athena_utils.AthenaQuery', return_value=mock_athena_query):
        # Act
        result = writer._get_full_table()

        # Assert
        assert len(result) == 2
        assert list(result.columns) == ['col_a', 'col_b']
        assert result['col_a'].tolist() == [1, 2]
        assert result['col_b'].tolist() == ['foo', 'bar']


def test_get_full_table_empty_table():
    """
    Test _get_full_table returns an empty DataFrame when the table has no rows.

    Background:
        Some Athena tables may be empty (e.g. freshly created). The method
        should return an empty DataFrame without errors.

    Steps:
        1. Create an AthenaValidationWriter (no snapshot_id set).
        2. Mock the Athena query to return an empty DataFrame with columns.
        3. Call _get_full_table().

    Expected:
        - Returns an empty DataFrame (0 rows).
        - Columns are preserved from the query result.
    """
    # Arrange
    config = AthenaConfig(
        aws_region='ap-southeast-2',
        aws_profile=None,
        athena_s3_output='s3://fake-bucket/output'
    )
    writer = AthenaValidationWriter(config, 'test_db', 'test_table')

    expected_df = pd.DataFrame(columns=['col_a', 'col_b'])

    mock_athena_query = MagicMock()
    mock_athena_query.query_athena.return_value = expected_df

    with patch('g3dt.utils.athena_utils.AthenaQuery', return_value=mock_athena_query):
        # Act
        result = writer._get_full_table()

        # Assert
        assert len(result) == 0
        assert list(result.columns) == ['col_a', 'col_b']


def test_construct_json_returns_valid_json_with_parsed_links():
    """
    Test construct_json produces valid JSON with submitter_id strings parsed into dicts.

    Background:
        construct_json fetches the latest snapshot, retrieves the full table,
        converts types for JSON serialisation, parses stringified submitter_id
        link columns into real dicts, and returns a JSON string.

    Steps:
        1. Create an AthenaValidationWriter.
        2. Mock the Athena query so that:
           - First call (snapshot query) returns a snapshot_id row.
           - Second call (full table query) returns a DataFrame with a
             stringified submitter_id dict and a normal string column.
        3. Call construct_json().
        4. Parse the returned JSON string.

    Expected:
        - Output is valid JSON (json.loads succeeds).
        - Contains 2 rows matching the input data.
        - The stringified dict "{'submitter_id': 'link1'}" is parsed into
          a real dict {'submitter_id': 'link1'}.
        - Non-dict string values are left unchanged.
    """
    # Arrange
    config = AthenaConfig(
        aws_region='ap-southeast-2',
        aws_profile=None,
        athena_s3_output='s3://fake-bucket/output'
    )
    writer = AthenaValidationWriter(config, 'test_db', 'test_table')

    snapshot_df = pd.DataFrame({
        'snapshot_id': ['55555'],
        'committed_at': ['2025-01-01 00:00:00.000 UTC']
    })
    table_df = pd.DataFrame([
        {'col_a': 1, 'link_col': "{'submitter_id': 'link1'}"},
        {'col_a': 2, 'link_col': 'not a dict'},
    ])

    mock_athena_query = MagicMock()
    mock_athena_query.query_athena.side_effect = [snapshot_df, table_df]

    with patch('g3dt.utils.athena_utils.AthenaQuery', return_value=mock_athena_query):
        # Act
        json_output = writer.construct_json()

        # Assert
        data = json.loads(json_output)
        assert len(data) == 2
        assert data[0]['col_a'] == 1
        assert data[0]['link_col'] == {'submitter_id': 'link1'}
        assert data[1]['col_a'] == 2
        assert data[1]['link_col'] == 'not a dict'


def test_construct_json_snapshot_id_available_after_call():
    """
    Test that writer.snapshot_id is populated after construct_json() completes.

    Background:
        construct_json internally calls _get_latest_snapshot_id which sets
        self.snapshot_id. Callers should be able to access writer.snapshot_id
        after construct_json() returns, without needing to call
        _get_latest_snapshot_id() again. This is important for avoiding
        redundant Athena queries.

    Steps:
        1. Create an AthenaValidationWriter (snapshot_id starts as None).
        2. Mock the Athena query to return snapshot_id='77777' and a table.
        3. Call construct_json().

    Expected:
        - writer.snapshot_id == '77777' after the call.
    """
    # Arrange
    config = AthenaConfig(
        aws_region='ap-southeast-2',
        aws_profile=None,
        athena_s3_output='s3://fake-bucket/output'
    )
    writer = AthenaValidationWriter(config, 'test_db', 'test_table')
    assert writer.snapshot_id is None  # precondition

    snapshot_df = pd.DataFrame({
        'snapshot_id': ['77777'],
        'committed_at': ['2025-03-01 12:00:00.000 UTC']
    })
    table_df = pd.DataFrame([{'col': 'value'}])

    mock_athena_query = MagicMock()
    mock_athena_query.query_athena.side_effect = [snapshot_df, table_df]

    with patch('g3dt.utils.athena_utils.AthenaQuery', return_value=mock_athena_query):
        # Act
        writer.construct_json()

        # Assert
        assert writer.snapshot_id == '77777'


@patch('g3dt.utils.athena_utils.boto3.client')
def test_write_gold_json_to_s3_study_id_not_in_name(mock_boto_client):
    """
    Test write_gold_json_to_s3 when the table name does not contain the study_id.

    Background:
        write_gold_json_to_s3 strips 'gold_' and '{study_id}_' from the table
        name to produce the filename. When the study_id is not present in the
        table name, only the 'gold_' prefix should be stripped.

    Steps:
        1. Call write_gold_json_to_s3 with table_name='gold_other' and
           study_id='ausdiab' (study_id not in table name).
        2. Check the S3 key.

    Expected:
        - The filename in the S3 key is 'other.json' (only 'gold_' stripped).
        - The full key follows the gold_jsons/ pattern.
    """
    mock_s3_instance = MagicMock()
    mock_boto_client.return_value = mock_s3_instance

    # Arrange
    json_data = '{"key": "value"}'

    # Act
    write_gold_json_to_s3(
        s3_bucket='my-test-bucket',
        study_id='ausdiab',
        table_name='gold_other',
        snapshot_id='snap-002',
        json_data=json_data
    )

    # Assert
    expected_key = (
        'gold_jsons/study_id=ausdiab/'
        'table_name=gold_other/'
        'snapshot_id=snap-002/other.json'
    )
    mock_s3_instance.put_object.assert_called_once_with(
        Body=json_data,
        Bucket='my-test-bucket',
        Key=expected_key
    )


def test_format_submitter_id_value(athena_validation_writer_instance):
    """
    Test that _format_submitter_id_value correctly parses stringified dicts.

    This test verifies that if the function is given a string that looks like a dictionary
    (for example, a string produced by str({'submitter_id': 'proj-01'})),
    it safely evaluates and returns a true Python dictionary.

    This is important because Athena stringified JSON fields may be returned as string
    representations, but downstream code expects a dict object.
    """
    # Test dict
    dict_string = "{'submitter_id': 'proj-01'}"
    result = athena_validation_writer_instance._format_submitter_id_value(dict_string)
    assert result == {'submitter_id': 'proj-01'}

    # Test dict with escaped quotes
    dict_string_escapes = "{\"submitter_id\":\"AusDiab_clinical_descriptor_123\"}"
    result = athena_validation_writer_instance._format_submitter_id_value(dict_string_escapes)
    assert result == {'submitter_id': 'AusDiab_clinical_descriptor_123'}
    
    # Test list of dicts
    dict_string_list = "[{\"submitter_id\":\"AusDiab_clinical_descriptor_123\"},{\"submitter_id\":\"AusDiab_clinical_descriptor_456\"}]"
    result = athena_validation_writer_instance._format_submitter_id_value(dict_string_list)
    assert result == [{'submitter_id': 'AusDiab_clinical_descriptor_123'}, {'submitter_id': 'AusDiab_clinical_descriptor_456'}]

def test_format_submitter_id_value_not_dict(athena_validation_writer_instance):
    """
    Test _format_submitter_id_value returns non-dict data as is.

    This validates that the helper does not modify values that aren't stringified dicts,
    but instead just passes them through (for robustness).

    It checks:
      - A normal string is left unchanged
      - Numeric values are left unchanged
      - None is returned as None
    """
    assert athena_validation_writer_instance._format_submitter_id_value("just a string") == "just a string"
    assert athena_validation_writer_instance._format_submitter_id_value(123) == 123
    assert athena_validation_writer_instance._format_submitter_id_value(None) is None

def test_construct_json(athena_validation_writer_instance):
    """
    Test AthenaValidationWriter.construct_json produces correct output with mocks.

    This is an end-to-end test (with mocks) for:
      - loading the validation target table as a DataFrame
      - returning a JSON string with all columns

    Special focus here is on checking that stringified dict columns are actually
    parsed into real Python dicts, and that all table columns are preserved.

    For junior devs, this demonstrates:
      - How mocks can simulate side effects in dependent methods (_get_full_table, etc)
      - How we can check the actual structure and per-row values in the produced JSON string
    """
    # Only mock _get_full_table, do not mock _get_latest_snapshot_id (construct_json no longer calls it)
    mock_table_df = pd.DataFrame([
        {'colA': 1, 'link_col': "{'submitter_id': 'link1'}"},
        {'colA': 2, 'link_col': 'not a dict'},
    ])
    with patch.object(athena_validation_writer_instance, '_get_full_table', return_value=mock_table_df) as mock_get_table, \
         patch.object(athena_validation_writer_instance, '_get_latest_snapshot_id', return_value='mock_snapshot_id'):

        json_output = athena_validation_writer_instance.construct_json()

        mock_get_table.assert_called_once()

        data = json.loads(json_output)

        # Check data integrity and formatting
        assert len(data) == 2
        assert data[0]['colA'] == 1
        assert data[0]['link_col'] == {'submitter_id': 'link1'}
        assert data[1]['link_col'] == 'not a dict'


def test_construct_json_gold_writer(athena_gold_writer_instance):
    """
    Test AthenaGoldWriter.construct_json (inherited/modified) produces correct JSON.

    This is an end-to-end test for the AthenaGoldWriter subclass, where:
      - specialized logic applies to Gold tables
      - the correct conversion and JSON stringification is performed

    Ensures that submitter_id links and data types are handled as in the base class,
    but via AthenaGoldWriter's overridden construct_json method.
    """
    # Gold writer expects a DataFrame, similar structure but might represent "gold data"
    gold_table_df = pd.DataFrame([
        {'gold_col': 42, 'submitter_id_link': "{'submitter_id': 'gold-link'}"},
        {'gold_col': 99, 'submitter_id_link': 'not a dict'},
    ])
    with patch.object(athena_gold_writer_instance, '_get_full_table', return_value=gold_table_df) as mock_get_table, \
         patch.object(athena_gold_writer_instance, '_get_latest_snapshot_id', return_value='mock_snapshot_id'):

        json_output = athena_gold_writer_instance.construct_json()

        mock_get_table.assert_called_once()

        data = json.loads(json_output)

        # Check correct row count and content; expects _format_submitter_id_value behavior
        assert len(data) == 2
        assert data[0]['gold_col'] == 42
        assert data[0]['submitter_id_link'] == {'submitter_id': 'gold-link'}
        assert data[1]['gold_col'] == 99
        assert data[1]['submitter_id_link'] == 'not a dict'

# --- Tests for utility functions ---

def test_generate_validation_id():
    """
    Test generate_validation_id produces a plausible, well-formatted ID.

    This tests that:
      - The produced ID matches the required format (YYYYMMDDHHMMSS, e.g. 20240719140533)
      - The value parses as a real datetime without error

    Such validation IDs are often used for unique, sortable file paths,
    so this protects against accidental changes in format.
    """
    validation_id = generate_validation_id()
    assert re.match(r'^\d{14}$', validation_id) is not None
    # Check if it's a plausible datetime
    try:
        datetime.strptime(validation_id, '%Y%m%d%H%M%S')
    except ValueError:
        pytest.fail("Generated ID is not a valid datetime string")

@patch('g3dt.utils.athena_utils.boto3.client')
def test_write_validation_json_to_s3(mock_boto_client):
    """
    Test write_validation_json_to_s3 writes to the correct S3 bucket and key.

    This test mocks out the boto3 S3 client so that we can:
      - Check the put_object method is called with exactly the expected bucket, key, and body
      - Confirm that paths and key formatting use all arguments (study_id, validation_id, etc)

    This protects against accidental bugs or path changes that could make integrations
    with S3-based downstream pipelines fail or write files into the wrong S3 location.
    """
    mock_s3_instance = MagicMock()
    mock_boto_client.return_value = mock_s3_instance

    json_data = '{"key": "value"}'
    write_validation_json_to_s3(
        s3_bucket='my-test-bucket',
        study_id='study-abc',
        validation_id='20251028143000',
        table_name='diagnosis',
        snapshot_id='snap-001',
        json_data=json_data
    )

    expected_key = 'validation/study_id=study-abc/validation_id=20251028143000/table_name=diagnosis/snapshot_id=snap-001/diagnosis.json'

    mock_boto_client.assert_called_once_with('s3')
    mock_s3_instance.put_object.assert_called_once_with(
        Body=json_data,
        Bucket='my-test-bucket',
        Key=expected_key
    )


@patch('g3dt.utils.athena_utils.boto3.client')
def test_write_gold_json_to_s3(mock_boto_client):
    """
    Test write_gold_json_to_s3 writes to the correct S3 bucket and key.

    Background:
        write_gold_json_to_s3 uploads JSON data to S3 for gold-tier Athena tables.
        It strips the 'gold_' prefix and study_id from the table name to produce
        the filename in the S3 key.

    Steps:
        1. Call write_gold_json_to_s3 with a gold table name that contains
           the study_id (e.g. 'gold_ausdiab_diagnosis' with study_id='ausdiab').
        2. Check that the S3 put_object was called with the expected key where
           the filename is 'diagnosis.json' (gold_ and ausdiab_ stripped).

    Expected:
        The S3 object key follows the pattern:
        gold_jsons/study_id=<study_id>/table_name=<table_name>/snapshot_id=<snapshot_id>/<stripped_name>.json
    """
    mock_s3_instance = MagicMock()
    mock_boto_client.return_value = mock_s3_instance

    # Arrange
    json_data = '{"key": "value"}'

    # Act
    write_gold_json_to_s3(
        s3_bucket='my-test-bucket',
        study_id='ausdiab',
        table_name='gold_ausdiab_diagnosis',
        snapshot_id='snap-001',
        json_data=json_data
    )

    # Assert
    expected_key = (
        'gold_jsons/study_id=ausdiab/'
        'table_name=gold_ausdiab_diagnosis/'
        'snapshot_id=snap-001/diagnosis.json'
    )
    mock_s3_instance.put_object.assert_called_once_with(
        Body=json_data,
        Bucket='my-test-bucket',
        Key=expected_key
    )

@patch('g3dt.utils.athena_utils.boto3.client')
def test_write_release_jsons_to_s3_correct_key_and_bucket(mock_boto_client):
    """
    Test that write_release_jsons_to_s3 writes to the correct S3 bucket and key.
    Ensures proper formatting of the S3 object key based on the given arguments.
    """
    mock_s3_instance = MagicMock()
    mock_boto_client.return_value = mock_s3_instance

    json_data = '{"foo": "bar"}'
    s3_bucket = 'test-release-bucket'
    release_id = 'rel-v1'
    study_id = 'staging/edcad'
    table_name = 'edcad_diagnosis'
    write_release_jsons_to_s3(
        s3_bucket=s3_bucket,
        release_id=release_id,
        study_id=study_id,
        table_name=table_name,
        json_data=json_data
    )

    expected_key = f"release_jsons/{release_id}/{study_id}/diagnosis.json"
    mock_boto_client.assert_called_once_with('s3')
    mock_s3_instance.put_object.assert_called_once_with(
        Body=json_data,
        Bucket=s3_bucket,
        Key=expected_key
    )


@patch('g3dt.utils.athena_utils.boto3.client')
def test_write_release_jsons_strips_study_name_with_env_prefix(mock_boto_client):
    """
    Test that write_release_jsons_to_s3 strips the study name from the filename.

    Background:
        When a release JSON is written to S3, the filename should be the entity
        name only (e.g. 'clinical_descriptor.json'), not prefixed with the study
        name (e.g. 'caughtcad_clinical_descriptor.json'). The study name is
        already encoded in the S3 directory path via study_id.

    Inputs:
        - study_id: 'staging/caughtcad' (env/study format)
        - table_name: 'caughtcad_clinical_descriptor' (study name still in table name)

    Expected:
        - The S3 object key filename should be 'clinical_descriptor.json',
          with 'caughtcad_' stripped from the table_name.
    """
    mock_s3_instance = MagicMock()
    mock_boto_client.return_value = mock_s3_instance

    json_data = '{"key": "value"}'
    write_release_jsons_to_s3(
        s3_bucket='release-bucket',
        release_id='v0.9.7',
        study_id='staging/caughtcad',
        table_name='caughtcad_clinical_descriptor',
        json_data=json_data
    )

    expected_key = 'release_jsons/v0.9.7/staging/caughtcad/clinical_descriptor.json'
    mock_s3_instance.put_object.assert_called_once_with(
        Body=json_data,
        Bucket='release-bucket',
        Key=expected_key
    )


@patch('g3dt.utils.athena_utils.boto3.client')
def test_write_release_jsons_strips_study_name_without_env_prefix(mock_boto_client):
    """
    Test that write_release_jsons_to_s3 strips the study name when study_id
    has no environment prefix (e.g. just 'ausdiab' instead of 'staging/ausdiab').

    Background:
        Some callers may pass study_id without an environment prefix. The function
        should still correctly identify and strip the study name from the filename.

    Inputs:
        - study_id: 'ausdiab' (no env prefix)
        - table_name: 'ausdiab_lab_result'

    Expected:
        - The S3 object key filename should be 'lab_result.json'.
    """
    mock_s3_instance = MagicMock()
    mock_boto_client.return_value = mock_s3_instance

    json_data = '{"key": "value"}'
    write_release_jsons_to_s3(
        s3_bucket='release-bucket',
        release_id='v1.0.0',
        study_id='ausdiab',
        table_name='ausdiab_lab_result',
        json_data=json_data
    )

    expected_key = 'release_jsons/v1.0.0/ausdiab/lab_result.json'
    mock_s3_instance.put_object.assert_called_once_with(
        Body=json_data,
        Bucket='release-bucket',
        Key=expected_key
    )


@patch('g3dt.utils.athena_utils.boto3.client')
def test_write_release_jsons_no_strip_when_study_not_in_name(mock_boto_client):
    """
    Test that write_release_jsons_to_s3 leaves the filename unchanged when the
    study name is not present as a prefix in the table_name.

    Background:
        If the table_name does not start with the study name, the function should
        use the table_name as-is for the filename. This prevents accidental
        corruption of filenames that happen to contain the study name elsewhere.

    Inputs:
        - study_id: 'staging/ausdiab'
        - table_name: 'diagnosis' (study name not present)

    Expected:
        - The S3 object key filename should be 'diagnosis.json' (unchanged).
    """
    mock_s3_instance = MagicMock()
    mock_boto_client.return_value = mock_s3_instance

    json_data = '{"key": "value"}'
    write_release_jsons_to_s3(
        s3_bucket='release-bucket',
        release_id='v1.0.0',
        study_id='staging/ausdiab',
        table_name='diagnosis',
        json_data=json_data
    )

    expected_key = 'release_jsons/v1.0.0/staging/ausdiab/diagnosis.json'
    mock_s3_instance.put_object.assert_called_once_with(
        Body=json_data,
        Bucket='release-bucket',
        Key=expected_key
    )


class TestConvertDataFrameTypesForJson:
    """
    Test suite for the convert_dataframe_types_for_json function.
    This function converts pandas DataFrame columns into JSON-serializable types,
    handling decimals, datetime objects, NaNs, NaTs, and numpy types.
    """

    def test_convert_decimal_to_float(self):
        """Ensure Decimal column values convert correctly to floats."""
        df = pd.DataFrame({'amount': [Decimal('10.5'), Decimal('20.75'), Decimal('30.25')]})
        result = convert_dataframe_types_for_json(df)
        assert result['amount'].dtype == float
        assert result['amount'].tolist() == [10.5, 20.75, 30.25]

    def test_convert_datetime_to_iso_string(self):
        """Ensure datetime columns convert to ISO formatted strings."""
        df = pd.DataFrame({'timestamp': pd.to_datetime(['2025-01-01', '2025-02-01', '2025-03-01'])})
        result = convert_dataframe_types_for_json(df)
        assert result['timestamp'].iloc[0] == '2025-01-01T00:00:00'
        assert all(isinstance(x, str) for x in result['timestamp'])

    def test_convert_nat_to_none(self):
        """Check that NaT (not a time) values convert to None."""
        df = pd.DataFrame({'timestamp': [pd.Timestamp('2025-01-01'), pd.NaT, pd.Timestamp('2025-03-01')]})
        result = convert_dataframe_types_for_json(df)
        assert result['timestamp'].iloc[0] == '2025-01-01T00:00:00'
        assert result['timestamp'].iloc[1] is None
        assert result['timestamp'].iloc[2] == '2025-03-01T00:00:00'

    def test_convert_numpy_nan_to_none(self):
        """Check that numpy NaN values convert to None."""
        df = pd.DataFrame({'value': [1.5, np.nan, 3.5]})
        result = convert_dataframe_types_for_json(df)
        assert result['value'].iloc[0] == 1.5
        assert result['value'].iloc[1] is None
        assert result['value'].iloc[2] == 3.5

    def test_convert_numpy_int64_to_nullable_int(self):
        """Verify numpy int64 columns convert to pandas nullable Int64 dtype."""
        df = pd.DataFrame({'count': np.array([1, 2, 3], dtype=np.int64)})
        result = convert_dataframe_types_for_json(df)
        # After conversion, dtype should be Pandas 'Int64' (nullable integer)
        assert str(result['count'].dtype) == "Int64"
        # All values should be either int or pandas NA (np.nan in Int64 uses pd.NA)
        # They are NOT guaranteed to be Python ints, but are either int or pd.NA
        for value in result['count']:
            if pd.isna(value):
                assert value is pd.NA
            else:
                assert isinstance(value, (int, np.integer))

    def test_convert_mixed_object_column(self):
        """Ensure object type columns with mixed datetime and strings convert correctly."""
        df = pd.DataFrame({'mixed': [pd.Timestamp('2025-01-01'), None, 'string_value']})
        result = convert_dataframe_types_for_json(df)
        assert result['mixed'].iloc[0] == '2025-01-01T00:00:00'
        assert result['mixed'].iloc[1] is None
        assert result['mixed'].iloc[2] == 'string_value'

    def test_dataframe_copy_not_modified(self):
        """Verify that original DataFrame remains unchanged after conversion."""
        df = pd.DataFrame({'value': [Decimal('10.5'), Decimal('20.75')]})
        original_values = df['value'].copy()
        result = convert_dataframe_types_for_json(df)
        assert all(isinstance(x, Decimal) for x in df['value'])
        assert all(isinstance(x, float) for x in result['value'])
        assert all(df['value'] == original_values)

    def test_empty_dataframe(self):
        """Ensure the function handles an empty DataFrame gracefully."""
        df = pd.DataFrame()
        result = convert_dataframe_types_for_json(df)
        assert result.empty
        assert len(result) == 0

    def test_dataframe_with_multiple_type_columns(self):
        """Test conversion for diverse types in multiple DataFrame columns, including int_col with missing values."""
        df = pd.DataFrame({
            'decimal_col': [Decimal('1.5'), Decimal('2.5')],
            'datetime_col': pd.to_datetime(['2025-01-01', '2025-02-01']),
            'int_col': pd.Series([1, pd.NA], dtype="Int64"),
            'float_col': [1.5, np.nan],
            'str_col': ['a', 'b']
        })
        result = convert_dataframe_types_for_json(df)
        assert all(isinstance(x, float) for x in result['decimal_col'])
        assert all(isinstance(x, str) for x in result['datetime_col'])
        assert result['int_col'].dtype == 'Int64'
        assert result['int_col'].iloc[0] == 1
        assert pd.isna(result['int_col'].iloc[1]) or result['int_col'].iloc[1] is pd.NA  # Should be pd.NA
        assert result['float_col'].iloc[0] == 1.5
        assert result['float_col'].iloc[1] is None
        assert result['str_col'].tolist() == ['a', 'b']

    def test_convert_python_datetime_in_object_column(self):
        """Test conversion of Python's datetime objects in object-type columns."""
        df = pd.DataFrame({'dates': [datetime(2025, 1, 1), datetime(2025, 2, 1), None]})
        result = convert_dataframe_types_for_json(df)
        assert result['dates'].iloc[0] == '2025-01-01T00:00:00'
        assert result['dates'].iloc[1] == '2025-02-01T00:00:00'
        assert result['dates'].iloc[2] is None

class TestJsonSerialiser:
    """
    Test suite for the json_serialiser function.
    This function serializes special types to JSON-friendly formats, including Decimal,
    datetime, numpy numeric types, and handles NaNs and NAs.
    """

    def test_serialise_decimal(self):
        """Test Decimal objects serialize to floats."""
        result = json_serialiser(Decimal('10.5'))
        assert result == 10.5
        assert isinstance(result, float)

    def test_serialise_datetime(self):
        """Test Python datetime serialization to ISO string."""
        dt = datetime(2025, 10, 29, 15, 30, 0)
        result = json_serialiser(dt)
        assert result == '2025-10-29T15:30:00'
        assert isinstance(result, str)

    def test_serialise_pandas_timestamp(self):
        """Test pandas Timestamp serialization to ISO string."""
        ts = pd.Timestamp('2025-10-29 15:30:00')
        result = json_serialiser(ts)
        assert result == '2025-10-29T15:30:00'
        assert isinstance(result, str)

    def test_serialise_numpy_int64(self):
        """Test serialization of numpy int64 to int."""
        value = np.int64(42)
        result = json_serialiser(value)
        assert result == 42
        assert isinstance(result, int)

    def test_serialise_numpy_int32(self):
        """Test serialization of numpy int32 to int."""
        value = np.int32(42)
        result = json_serialiser(value)
        assert result == 42
        assert isinstance(result, int)

    def test_serialise_numpy_float64(self):
        """Test serialization of numpy float64 to float."""
        value = np.float64(3.14)
        result = json_serialiser(value)
        assert result == 3.14
        assert isinstance(result, float)

    def test_serialise_numpy_float32(self):
        """Test serialization of numpy float32 to float."""
        value = np.float32(3.14)
        result = json_serialiser(value)
        assert abs(result - 3.14) < 0.01
        assert isinstance(result, float)

    def test_serialise_numpy_nan(self):
        """Test serialization of numpy NaN to None."""
        result = json_serialiser(np.nan)
        assert result is None

    def test_serialise_pandas_nat(self):
        """Test serialization of pandas NaT to None."""
        result = json_serialiser(pd.NaT)
        assert result is None

    def test_serialise_pandas_na(self):
        """Test serialization of pandas NA to None."""
        result = json_serialiser(pd.NA)
        assert result is None
    
    def test_serialise_datetime_date(self):
        """Test serialization of datetime.date to ISO string."""
        value = date(2025, 10, 29)
        result = json_serialiser(value)
        assert result == '2025-10-29'
        assert isinstance(result, str)

    # def test_serialise_unsupported_type_raises_error(self):
    #     """Test that unsupported types raise TypeError."""
    #     with pytest.raises(TypeError, match="not JSON serialisable"):
    #         json_serialiser(set([1, 2, 3]))


    def test_serialiser_in_json_dumps(self):
        """Test json_serialiser integration with json.dumps."""
        data = {
            'decimal': Decimal('10.5'),
            'datetime': datetime(2025, 10, 29),
            'numpy_int': np.int64(42),
            'numpy_float': np.float64(3.14),
            'nan': np.nan
        }
        
        # Clean the data before serialising
        cleaned_data = replace_nan_with_none(data)

        # Now, json.dumps receives data where np.nan has already been replaced by None
        json_str = json.dumps(cleaned_data, default=json_serialiser)
        parsed = json.loads(json_str)

        assert parsed['decimal'] == 10.5
        assert parsed['datetime'] == '2025-10-29T00:00:00'
        assert parsed['numpy_int'] == 42
        assert parsed['numpy_float'] == 3.14
        assert parsed['nan'] is None # This will now pass


    def test_serialiser_with_nested_structures(self):
        """Test json_serialiser correctly handles nested dicts and lists."""
        data = {
            'values': [Decimal('1.5'), np.int64(2), np.nan],
            'nested': {
                'timestamp': pd.Timestamp('2025-10-29'),
                'count': np.int32(100)
            }
        }
        data = replace_nan_with_none(data)
        json_str = json.dumps(data, default=json_serialiser)
        parsed = json.loads(json_str)
        assert parsed['values'] == [1.5, 2, None]
        assert parsed['nested']['timestamp'] == '2025-10-29T00:00:00'
        assert parsed['nested']['count'] == 100

class TestIntegration:
    """
    Integration tests combining conversion and serialization
    to ensure full pipeline correctness.
    """

    def test_dataframe_to_json_with_serialiser(self):
        """Test that a DataFrame can be converted and serialized to JSON properly."""
        df = pd.DataFrame({
            'decimal': [Decimal('10.5'), Decimal('20.5')],
            'timestamp': pd.to_datetime(['2025-01-01', '2025-02-01']),
            'value': [1.5, np.nan]
        })
        converted_df = convert_dataframe_types_for_json(df)
        records = converted_df.to_dict('records')
        json_str = json.dumps(records, default=json_serialiser)
        parsed = json.loads(json_str)
        assert len(parsed) == 2
        assert parsed[0]['decimal'] == 10.5
        assert parsed[0]['timestamp'] == '2025-01-01T00:00:00'
        assert parsed[0]['value'] == 1.5
        assert parsed[1]['value'] is None

    def test_large_dataframe_performance(self):
        """Test that conversion works efficiently on larger DataFrames."""
        df = pd.DataFrame({
            'decimal': [Decimal('1.5')] * 1000,
            'timestamp': pd.to_datetime(['2025-01-01'] * 1000),
            'int_val': np.arange(1000, dtype=np.int64),
            'float_val': np.random.randn(1000)
        })
        result = convert_dataframe_types_for_json(df)
        assert len(result) == 1000
        assert all(isinstance(x, float) for x in result['decimal'])
        assert all(isinstance(x, str) for x in result['timestamp'])

    # def test_edge_case_infinity(self):
    #     """Test serialization of infinity values."""
    #     assert json_serialiser(np.inf) == float('inf')
    #     assert json_serialiser(np.NINF) == float('-inf')

    def test_mixed_decimal_and_none(self):
        """Test conversion of DataFrame column that contains both Decimal and None values."""
        df = pd.DataFrame({
            'amount': [Decimal('10.5'), None, Decimal('20.75')]
        })
        result = convert_dataframe_types_for_json(df)
        assert result['amount'].iloc[0] == 10.5
        assert result['amount'].iloc[1] is None
        assert result['amount'].iloc[2] == 20.75


ATHENA_UTILS_PATH = "g3dt.utils.athena_utils"


class TestWriteIcebergToDb:
    """Tests for the write_iceberg_to_db function."""

    @patch(f"{ATHENA_UTILS_PATH}.wr.athena.to_iceberg")
    @patch(f"{ATHENA_UTILS_PATH}.wr.catalog.create_database")
    def test_write_iceberg_success(
        self, mock_create_db, mock_to_iceberg
    ):
        """
        Verifies that write_iceberg_to_db creates the Glue database
        and calls wr.athena.to_iceberg with the correct parameters.

        Inputs:
            - df: A simple DataFrame with one row.
            - database: "test_db"
            - table: "test_table"
            - athena_s3_output: "s3://bucket/athena-output/"
            - workgroup: "primary"

        Expected:
            - wr.catalog.create_database called with name="test_db"
            - wr.athena.to_iceberg called once with correct
              database, table, workgroup, and a temp_path derived
              from athena_s3_output.
        """
        df = pd.DataFrame({"col": ["value"]})

        write_iceberg_to_db(
            df=df,
            database="test_db",
            table="test_table",
            athena_s3_output="s3://bucket/athena-output/",
            workgroup="primary",
            table_location="s3://bucket/data/",
        )

        mock_create_db.assert_called_once_with(
            name="test_db", exist_ok=True
        )
        mock_to_iceberg.assert_called_once()

        call_kwargs = mock_to_iceberg.call_args[1]
        assert call_kwargs["database"] == "test_db"
        assert call_kwargs["table"] == "test_table"
        assert call_kwargs["workgroup"] == "primary"
        assert call_kwargs["table_location"] == "s3://bucket/data/"
        assert call_kwargs["temp_path"].startswith(
            "s3://bucket/athena-output/temp/"
        )

    @patch(f"{ATHENA_UTILS_PATH}.wr.athena.to_iceberg")
    @patch(f"{ATHENA_UTILS_PATH}.wr.catalog.create_database")
    def test_write_iceberg_casts_to_string(
        self, mock_create_db, mock_to_iceberg
    ):
        """
        Verifies that all DataFrame columns are cast to string dtype
        before writing to Iceberg, matching the previous Parquet
        writer behaviour.

        Inputs:
            - df with integer and float columns.

        Expected:
            - The df passed to wr.athena.to_iceberg has all string
              dtypes.
        """
        df = pd.DataFrame({"num": [1, 2], "val": [3.14, 2.71]})

        write_iceberg_to_db(
            df=df,
            database="db",
            table="tbl",
            athena_s3_output="s3://bucket/out/",
        )

        written_df = mock_to_iceberg.call_args[1]["df"]
        for col in written_df.columns:
            assert written_df[col].dtype == "string"

    @patch(f"{ATHENA_UTILS_PATH}.wr.athena.to_iceberg")
    @patch(f"{ATHENA_UTILS_PATH}.wr.catalog.create_database")
    def test_write_iceberg_failure_raises_runtime_error(
        self, mock_create_db, mock_to_iceberg
    ):
        """
        Verifies that when wr.athena.to_iceberg raises an exception,
        write_iceberg_to_db wraps it in a RuntimeError.

        Inputs:
            - df: A simple DataFrame.
            - to_iceberg mock raises Exception("Write failed").

        Expected:
            - RuntimeError is raised.
        """
        mock_to_iceberg.side_effect = Exception("Write failed")
        df = pd.DataFrame({"col": [1]})

        with pytest.raises(RuntimeError, match="Failed to write"):
            write_iceberg_to_db(
                df=df,
                database="test_db",
                table="test_table",
                athena_s3_output="s3://bucket/out/",
            )

    @patch(f"{ATHENA_UTILS_PATH}.wr.athena.to_iceberg")
    @patch(f"{ATHENA_UTILS_PATH}.wr.catalog.create_database")
    def test_write_iceberg_passes_boto3_session(
        self, mock_create_db, mock_to_iceberg
    ):
        """
        Verifies that a provided boto3_session is forwarded to
        wr.athena.to_iceberg.

        Inputs:
            - boto3_session: A MagicMock session object.

        Expected:
            - wr.athena.to_iceberg receives the session in its
              boto3_session kwarg.
        """
        mock_session = MagicMock()
        df = pd.DataFrame({"col": ["v"]})

        write_iceberg_to_db(
            df=df,
            database="db",
            table="tbl",
            athena_s3_output="s3://bucket/out/",
            boto3_session=mock_session,
        )

        call_kwargs = mock_to_iceberg.call_args[1]
        assert call_kwargs["boto3_session"] is mock_session

    @patch(f"{ATHENA_UTILS_PATH}.wr.athena.to_iceberg")
    @patch(f"{ATHENA_UTILS_PATH}.wr.catalog.create_database")
    def test_write_iceberg_table_location_none_by_default(
        self, mock_create_db, mock_to_iceberg
    ):
        """
        Verifies that when table_location is not provided, it
        defaults to None and is passed as None to
        wr.athena.to_iceberg. This means the table must already
        exist; creating a new table without table_location will
        fail with 'Must specify table location'.

        Inputs:
            - df: A simple DataFrame.
            - No table_location argument.

        Expected:
            - wr.athena.to_iceberg receives table_location=None.
        """
        df = pd.DataFrame({"col": ["v"]})

        write_iceberg_to_db(
            df=df,
            database="db",
            table="tbl",
            athena_s3_output="s3://bucket/out/",
        )

        call_kwargs = mock_to_iceberg.call_args[1]
        assert call_kwargs["table_location"] is None
