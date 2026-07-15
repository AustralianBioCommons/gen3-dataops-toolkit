import pytest
from unittest.mock import MagicMock, patch, call
import pandas as pd
import argparse

MODULE_PATH = "g3dt.utils.release_writer"

from g3dt.utils.release_writer import (
    safe_sql_string,
    insert_release_row,
    main,
    parse_args
)

class TestSafeSqlString:
    """
    Tests for safe_sql_string function, which is designed to take in various Python values and
    return a value that is safe to include as a string inside an SQL query, ensuring escaping of
    single quotes and proper handling of None and empty strings.

    This class parameterizes test cases with multiple types of input, such as:
      - None and empty strings, which should be converted to SQL NULL,
      - typical strings, which should be quoted,
      - strings containing single quotes, which should have those quotes escaped for SQL safety,
      - and non-string (e.g. integer) values, which should be converted to string and quoted.
    Each test asserts that the output matches the expected, canonical SQL-safe form.
    """
    @pytest.mark.parametrize("input_val, expected", [
        (None, "NULL"),
        ("", "NULL"),
        ("a simple string", "'a simple string'"),
        ("string with 'single quotes'", "'string with ''single quotes'''"),
        (12345, "'12345'"),
    ])
    def test_sql_string_escaping(self, input_val, expected):
        """
        Given various input values (including None, empty, regular, special, and numeric),
        ensures that safe_sql_string produces the correct SQL-escaped string literal.
        """
        assert safe_sql_string(input_val) == expected

class TestInsertReleaseRow:
    """
    Tests for the insert_release_row function, used to insert information about model releases
    into an Athena-backed release table, while avoiding duplicate entries.

    Two key scenarios are tested:
    1. If a release row that matches the input parameters already exists (based on a COUNT query),
       insert_release_row should *not* attempt an INSERT and perform only the COUNT query.
    2. If no such row exists (the COUNT is zero), insert_release_row should perform both the SELECT
       and INSERT queries to add the new row.

    The AthenaQuery class, responsible for making Athena queries, is mocked, as is its query_athena
    method. The tests verify call counts and SQL string content to ensure correct business logic is enforced.
    """
    @patch(f"{MODULE_PATH}.AthenaQuery")
    def test_skips_if_row_exists(self, mock_athena_query_class):
        """
        Test that when the database already contains a row for the provided
        (model_name, db_name, snapshot_id, etc), insert_release_row does *not*
        try to insert a duplicate. This is detected by mocking query_athena to
        return a DataFrame with a nonzero (here 1) count.
        """
        mock_athena_query_instance = MagicMock()
        mock_athena_query_instance.query_athena.return_value = pd.DataFrame({'cnt': [1]})
        mock_athena_query_class.return_value = mock_athena_query_instance
        
        insert_release_row(
            athena_config=MagicMock(),
            model_name="test_model",
            db_name="test_db",
            snapshot_id=1,
            committed_at="2025-10-28 15:00:00",
            release_db="release_db",
            release_table="releases",
            release_tag="v1.0",
            github_sha="abc"
        )
        
        # The function should call only one query (the count) and should not attempt an insert
        mock_athena_query_instance.query_athena.assert_called_once()
        assert "SELECT COUNT(*)" in mock_athena_query_instance.query_athena.call_args[0][0]

    @patch(f"{MODULE_PATH}.AthenaQuery")
    def test_inserts_if_row_not_exists(self, mock_athena_query_class):
        """
        Test that when the database does not contain a matching row (the count is zero),
        insert_release_row issues two queries: one SELECT COUNT(*) and one INSERT INTO ...
        The first query_athena call returns 0, so the second call is triggered to actually insert.
        Both the number of calls and the structure of the second query (it must be an INSERT) are asserted.
        """
        mock_athena_query_instance = MagicMock()
        mock_athena_query_instance.query_athena.side_effect = [
            pd.DataFrame({'cnt': [0]}), # Select count query returns 0
            None                        # Insert query returns nothing
        ]
        mock_athena_query_class.return_value = mock_athena_query_instance
        
        insert_release_row(
            athena_config=MagicMock(),
            model_name="test_model",
            db_name="test_db",
            snapshot_id=1,
            committed_at="2025-10-28 15:00:00",
            release_db="release_db",
            release_table="releases",
            release_tag="v1.0",
            github_sha="abc"
        )
        
        # The function should issue both the SELECT and INSERT queries
        assert mock_athena_query_instance.query_athena.call_count == 2
        insert_call = mock_athena_query_instance.query_athena.call_args_list[1]
        assert "INSERT INTO" in insert_call[0][0]

class TestMainExecutionFlow:
    """
    Tests the main() function, which coordinates reading DBT models and populating release metadata.

    The main flow performs these steps:
      - Collects parsed command-line arguments for configuration.
      - Loads a list of model names from a dbt schema YAML file.
      - For each model, determines its underlying database name via Athena,
        fetches the latest snapshot id and commit time via AthenaValidationWriter,
        and finally attempts to insert a release info row.
      - Logging is also performed at several points in the workflow.

    This test mocks all external dependencies:
      - parse_args for the CLI inputs,
      - get_model_names for the schema extraction,
      - AthenaQuery for Athena lookups,
      - AthenaValidationWriter for fetching latest snapshot data,
      - insert_release_row for actually recording the release,
      - logging to avoid real log output.

    The test ensures that the business logic coordinates all the mocked components:
      - It verifies the correct sequence of calls and propagates input values through the workflow.
      - It checks that insert_release_row is invoked for each discovered DBT model using the snapshot and DB names from the mocks,
        verifying the glue code inside the main() orchestration is correct.
    """
    @patch(f"{MODULE_PATH}.parse_args")
    @patch(f"{MODULE_PATH}.get_model_names")
    @patch(f"{MODULE_PATH}.AthenaQuery")
    @patch(f"{MODULE_PATH}.AthenaValidationWriter")
    @patch(f"{MODULE_PATH}.insert_release_row")
    @patch(f"{MODULE_PATH}.logging")
    def test_main_orchestration(
        self,
        mock_logging,
        mock_insert_release_row,
        mock_snapshot_writer_class,
        mock_athena_query_class,
        mock_get_model_names,
        mock_parse_args,
    ):
        """
        This test fully simulates the main function's orchestration using mocks
        for all side-effectful or external components.
        It asserts that DBT model names are loaded and processed,
        DB lookup and snapshot reading occur for each model,
        and the release insert function is called for each, propagating correct values.

        The test's scenario uses two models and assigns snapshot and db names for each,
        mimicking a real release insert loop.
        """
        mock_args = argparse.Namespace(
            dbt_schema_path="/path/to/schema.yml",
            release_db="prod_release_db",
            release_table="dbt_releases",
            data_release_version="v2.1",
            commit_id="f00ba2",
            aws_region="eu-west-2",
            aws_profile="prod",
            athena_s3_output="s3://prod-athena-logs/",
            release_s3_location="s3://prod-metadata-bucket/",
            verbose=True
        )
        mock_parse_args.return_value = mock_args

        mock_get_model_names.return_value = ["customers", "orders"]

        mock_athena_query_instance = MagicMock()
        mock_athena_query_instance.find_db_for_model.side_effect = ["prod_dwh_customers", "prod_dwh_orders"]
        mock_athena_query_class.return_value = mock_athena_query_instance

        mock_snapshot_writer_instance = MagicMock()
        mock_snapshot_writer_instance._get_latest_snapshot_id.side_effect = [
            (101, "2025-10-27 10:00:00"), # For 'customers'
            (202, "2025-10-27 11:00:00")  # For 'orders'
        ]
        mock_snapshot_writer_class.return_value = mock_snapshot_writer_instance
        
        # Act: call the main orchestration
        main()
        
        # Assert: model list is loaded correctly
        mock_get_model_names.assert_called_with("/path/to/schema.yml")
        
        # Assert: release insert is attempted for both models, each with correct values obtained from mocks
        assert mock_insert_release_row.call_count == 2
        
        mock_insert_release_row.assert_any_call(
            athena_config=mock_athena_query_class.call_args[0][0],
            model_name="customers",
            db_name="prod_dwh_customers",
            snapshot_id=101,
            committed_at="2025-10-27 10:00:00",
            release_db="prod_release_db",
            release_table="dbt_releases",
            release_tag="v2.1",
            github_sha="f00ba2"
        )
