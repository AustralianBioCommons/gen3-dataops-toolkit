import pytest
from unittest.mock import patch, MagicMock
import pandas as pd
from datetime import datetime
import pytz
from botocore.exceptions import ClientError

# Import the module to be tested
import g3dt.ingest.ingest as ingest_module

# --- Fixtures ---

@pytest.fixture
def sample_dataframe():
    """Provides a basic DataFrame for testing."""
    return pd.DataFrame({'column_one': ['data1', 'data2'], 'column_two': [10, 20]})

@pytest.fixture
def mock_s3_client():
    """Fixture to mock the boto3 S3 client used in the ingest module."""
    with patch('g3dt.ingest.ingest.s3') as mock_client:
        yield mock_client

@pytest.fixture
def mock_wrangler():
    """Fixture to mock the awswrangler library used in the ingest module."""
    with patch('g3dt.ingest.ingest.wr') as mock_wr:
        yield mock_wr

# --- Tests for Helper Functions ---

class TestHelperFunctions:
    """Test suite for standalone helper functions."""

    def test_parse_s3(self):
        """Ensures S3 URIs are correctly parsed into bucket and key."""
        bucket, key = ingest_module.parse_s3("s3://my-test-bucket/path/to/file.csv")
        assert bucket == "my-test-bucket"
        assert key == "path/to/file.csv"

    def test_get_format(self):
        """Tests that the file extension is correctly extracted."""
        assert ingest_module.get_format("s3://bucket/file.csv") == "csv"
        assert ingest_module.get_format("s3://bucket/file.JSON") == "json"
        assert ingest_module.get_format("s3://bucket/archive.tar.gz") == "gz"

    @pytest.mark.parametrize("input_name, expected_output", [
        ("Column Name / 1", "column_name_1"),
        ("  leading_and_trailing_spaces  ", "leading_and_trailing_spaces"),
        ("123_starts_with_digit", "c_123_starts_with_digit"),
        ("invalid__chars!!@", "invalid_chars_"),
        ("", "col"),
        (None, "col")
    ])
    def test_normalise(self, input_name, expected_output):
        """Tests the column name normalisation logic with various inputs."""
        assert ingest_module.normalise(input_name) == expected_output


    def test_compute_row_hash(self):
        """Verifies that the row hash is computed correctly and consistently."""
        row = pd.Series({'a': '1', 'b': '2', 'c': None})
        expected_hash = "4f950e38d0e085c3ef12021f201b378feaa984ef8a2cf6bfefba3d275bb84fa4"
        assert ingest_module.compute_row_hash(row) == expected_hash


    @pytest.mark.parametrize("date_input, expected_output", [
        ("2025-10-30", "2025-10-30"),
        ("2025_10_30", "2025-10-30"),
        ("30-10-2025", "2025-10-30"),
        ("30 10 2025", "2025-10-30")
    ])
    def test_sanitize_submission_date_valid(self, date_input, expected_output):
        """Tests valid date string parsing."""
        assert ingest_module.sanitize_submission_date(date_input) == expected_output

    @pytest.mark.parametrize("invalid_date", ["", "not-a-date", "2025/10/30"])
    def test_sanitize_submission_date_invalid(self, invalid_date):
        """Tests that invalid date strings raise a ValueError."""
        with pytest.raises(ValueError):
            ingest_module.sanitize_submission_date(invalid_date)

# --- Tests for S3 and Wrangler Interactions ---

class TestS3Interactions:
    """Test suite for functions that interact with S3 and AWS Wrangler."""

    def test_get_tags_success(self, mock_s3_client):
        """Tests successful retrieval of S3 object tags."""
        mock_s3_client.get_object_tagging.return_value = {
            'TagSet': [{'Key': 'study_id', 'Value': 'study-123'}]
        }
        tags = ingest_module.get_tags("s3://bucket/file")
        assert tags == {'study_id': 'study-123'}
        mock_s3_client.get_object_tagging.assert_called_once_with(Bucket='bucket', Key='file')

    def test_get_tags_no_such_key(self, mock_s3_client):
        """Tests graceful handling of a 'NoSuchKey' error when fetching tags."""
        error_response = {'Error': {'Code': 'NoSuchKey'}}
        mock_s3_client.get_object_tagging.side_effect = ClientError(error_response, 'GetObjectTagging')
        
        tags = ingest_module.get_tags("s3://bucket/nonexistent")
        assert tags == {}

    def test_get_head_meta_success(self, mock_s3_client):
        """Tests successful retrieval of S3 head object metadata."""
        melbourne_tz = pytz.timezone("Australia/Melbourne")
        mock_time = datetime(2025, 10, 30, 14, 0, 0, tzinfo=pytz.utc)
        
        mock_s3_client.head_object.return_value = {
            'ETag': '"mock-etag"',
            'ContentLength': 1024,
            'LastModified': mock_time
        }
        
        meta = ingest_module.get_head_meta("s3://bucket/file")
        
        expected_time_str = mock_time.astimezone(melbourne_tz).strftime("%Y-%m-%dT%H:%M:%S%z")
        
        assert meta['ingest_file_etag'] == "mock-etag"
        assert meta['ingest_file_size_bytes'] == "1024"
        assert meta['ingest_file_last_modified'] == expected_time_str

    def test_read_csv_robust_success(self, mock_wrangler):
        """Tests reading a CSV successfully on the first attempt."""
        mock_wrangler.s3.read_csv.return_value = pd.DataFrame()
        ingest_module.read_csv_robust("s3://bucket/file.csv")
        mock_wrangler.s3.read_csv.assert_called_once_with(
            path="s3://bucket/file.csv",
            sep=None, engine="python", dtype=str,
            keep_default_na=False, encoding='utf-8-sig', quoting=0
        )

    def test_read_csv_robust_fallback_encoding(self, mock_wrangler):
        """Tests that the CSV reader falls back to the next encoding on failure."""
        mock_wrangler.s3.read_csv.side_effect = [
            Exception("UTF-8 decode error"),  # First call fails
            pd.DataFrame({'col': ['data']})   # Second call succeeds
        ]
        df = ingest_module.read_csv_robust("s3://bucket/file.csv")
        assert not df.empty
        assert mock_wrangler.s3.read_csv.call_count == 2
    
    def test_get_ingest_true_files(self, mock_wrangler):
        """Tests filtering of files based on the 'ingest=true' tag."""
        file_list = ["s3://b/f1.csv", "s3://b/f2.csv", "s3://b/f3.csv"]
        mock_wrangler.s3.list_objects.return_value = file_list

        # Mock the get_tags function to control tag values
        def get_tags_side_effect(uri):
            if uri == "s3://b/f1.csv":
                return {'ingest': 'true'}
            if uri == "s3://b/f2.csv":
                return {'ingest': 'false'}
            return {} # f3 has no tags
        
        with patch('g3dt.ingest.ingest.get_tags', side_effect=get_tags_side_effect):
             ingest_files = ingest_module.get_ingest_true_files("s3://b/")
        
        assert ingest_files == ["s3://b/f1.csv"]

# --- Tests for Main Ingest Logic ---

class TestIngestPipeline:
    """Test suite for the main data ingestion and processing functions."""

    def test_prepare_ingest_metadata(self, sample_dataframe):
        """Verifies that ingest metadata is correctly added to the DataFrame."""
        uri = "s3://my-bucket/my-file.csv"
        tags = {'submission_date': '2025-10-30', 'custom_tag': 'custom_val'}
        head_meta = {
            'ingest_file_etag': 'etag123',
            'ingest_file_size_bytes': '4096',
            'ingest_file_last_modified': '2025-10-30T14:00:00+1100'
        }
        
        annotated_df = ingest_module.prepare_ingest_metadata(
            df=sample_dataframe, uri=uri, tags=tags, head_meta=head_meta,
            study_id='study-x', ingest_run_id='run-1', ingest_received_at='now',
            ingest_timezone='AEST', ingest_submission_id='sub-1'
        )
        
        # Check that all expected columns were added
        assert 'study_id' in annotated_df.columns
        assert 'submission_date' in annotated_df.columns
        assert 'ingest_run_id' in annotated_df.columns
        assert 'ingest_original_file_path' in annotated_df.columns
        assert 'ingest_row_hash' in annotated_df.columns
        assert 'tag_custom_tag' in annotated_df.columns
        
        # Check values
        assert annotated_df['study_id'].iloc[0] == 'study-x'
        assert annotated_df['ingest_file_etag'].iloc[0] == 'etag123'
        assert annotated_df['tag_custom_tag'].iloc[0] == 'custom_val'

    def test_align_and_combine_frames(self):
        """Ensures DataFrames with different columns are aligned and concatenated."""
        df1 = pd.DataFrame({'a': [1], 'b': [2]})
        df2 = pd.DataFrame({'b': [3], 'c': [4]})
        
        combined = ingest_module.align_and_combine_frames([df1, df2])
        
        # Check shape and columns
        assert combined.shape == (2, 3)
        assert sorted(combined.columns) == ['a', 'b', 'c']
        
        # Check that missing values are filled correctly with an empty string
        assert combined.loc[0, 'c'] == ""
        assert combined.loc[1, 'a'] == ""

    @patch('g3dt.ingest.ingest.write_iceberg_to_db')
    @patch('g3dt.ingest.ingest.prepare_ingest_metadata')
    @patch('g3dt.ingest.ingest.get_head_meta')
    @patch('g3dt.ingest.ingest.read_csv_robust')
    @patch('g3dt.ingest.ingest.get_tags')
    def test_ingest_table_to_parquet_dataset(
        self, mock_get_tags, mock_read_csv, mock_get_head, mock_prepare, mock_write_iceberg
    ):
        """End-to-end test of the single-file ingest pipeline."""
        mock_get_tags.return_value = {'study_id': 's1', 'node': 'diagnosis', 'submission_date': '2025-10-30'}
        mock_read_csv.return_value = pd.DataFrame({'snomed': ['123']})
        mock_get_head.return_value = {}
        mock_prepare.return_value = pd.DataFrame({
            'snomed': ['123'], 'study_id': ['s1'], 'submission_date': ['2025-10-30'], 'ingest_file_name': ['diag.csv']
        })
        
        result = ingest_module.ingest_table_to_parquet_dataset(
            s3_uri="s3://b/diag.csv",
            database="test_db",
            table_prefix="test_prefix",
            athena_s3_output="s3://db-root/output/",
        )

        # Assert that the correct table name was constructed and used
        expected_table_name = "test_prefix_diagnosis"
        mock_write_iceberg.assert_called_once()
        call_args, call_kwargs = mock_write_iceberg.call_args
        assert call_kwargs['table'] == expected_table_name

        # Assert the result dictionary has the expected structure
        assert result['files_processed'] == 1
        assert result['tables_written'] == [expected_table_name]

# --- Tests for XLSX Reading ---


# --- Tests for XLSX Reading ---


class TestXlsxReading:
    """Test suite for the robust XLSX reader function."""


    @patch('g3dt.ingest.ingest.pd')
    def test_read_xlsx_robust_multiple_sheets(self, mock_pd):
        """
        Tests what happens when an Excel file has multiple sheets.
        
        This test simulates an Excel file containing two sheets ('Sheet1' and 'Sheet2').
        It verifies that:
        1. The function calls pandas with the correct arguments (engine='openpyxl', dtype=str).
        2. The function returns a dictionary containing both sheets exactly as they were read.
        """
        # Mock result simulating two sheets
        sheet1 = pd.DataFrame({'col': ['a']})
        sheet2 = pd.DataFrame({'col': ['b']})
        mock_result = {'Sheet1': sheet1, 'Sheet2': sheet2}
        mock_pd.read_excel.return_value = mock_result


        uri = "s3://test-bucket/data.xlsx"
        result = ingest_module.read_xlsx_robust(uri)


        # Verify we got the exact dictionary back
        assert result == mock_result
        assert 'Sheet1' in result
        assert 'Sheet2' in result
        
        # Verify pandas was called with sheet_name=None to read all sheets
        mock_pd.read_excel.assert_called_once_with(
            uri, 
            sheet_name=None, 
            engine="openpyxl", 
            dtype=str, 
            keep_default_na=False
        )


    @patch('g3dt.ingest.ingest.pd')
    def test_read_xlsx_robust_single_sheet_renaming(self, mock_pd):
        """
        Tests the special logic for Excel files with only one sheet.
        
        If an Excel file has only one sheet (e.g., named 'Sheet1'), the function 
        should rename the dictionary key to match the file name instead of the sheet name.
        
        This test:
        1. Simulates reading a file 'my_data.xlsx' that has one sheet named 'Sheet1'.
        2. Verifies that the returned dictionary key is 'my_data', NOT 'Sheet1'.
        """
        # Mock result simulating a single sheet
        sheet1 = pd.DataFrame({'col': ['a']})
        mock_result = {'Sheet1': sheet1}
        mock_pd.read_excel.return_value = mock_result


        uri = "s3://test-bucket/folder/my_data.xlsx"
        result = ingest_module.read_xlsx_robust(uri)


        # The key should be the filename 'my_data', not 'Sheet1'
        assert 'my_data' in result
        assert 'Sheet1' not in result
        pd.testing.assert_frame_equal(result['my_data'], sheet1)


    @pytest.mark.parametrize("invalid_uri", [
        "http://bucket/file.xlsx",
        "/local/path/file.xlsx", 
        ""
    ])
    def test_read_xlsx_robust_invalid_uri(self, invalid_uri):
        """
        Tests validation for bad S3 links.
        
        This checks that if a user passes a link that doesn't start with 's3://',
        the function immediately stops and complains (raises a ValueError) rather
        than trying to process it.
        """
        with pytest.raises(ValueError) as excinfo:
            ingest_module.read_xlsx_robust(invalid_uri)
        
        assert f"Invalid S3 URI: {invalid_uri}" in str(excinfo.value)


    @patch('g3dt.ingest.ingest.pd')
    def test_read_xlsx_robust_failure(self, mock_pd):
        """
        Tests how the function handles corrupted files or read errors.
        
        This simulates a scenario where pandas crashes (e.g., corrupt file).
        It verifies that the function catches that crash and raises a standard 
        RuntimeError with a helpful message.
        """
        mock_pd.read_excel.side_effect = Exception("Corrupt file header")


        with pytest.raises(RuntimeError) as excinfo:
            ingest_module.read_xlsx_robust("s3://bucket/bad.xlsx")
        
        assert "Failed to read XLSX" in str(excinfo.value)


# --- Tests for Flattening Logic ---


class TestFlattenXlsx:
    """Test suite for flattening dictionary of DataFrames (Excel sheets)."""


    def test_flatten_xlsx_dict_success(self):
        """
        Tests the normal process of merging multiple Excel sheets.
        
        This test creates two separate tables (representing two sheets).
        It checks that:
        1. They are combined into one long table.
        2. A new column 'sheet_name' is added to tell us which sheet the data came from.
        """
        df_sheet1 = pd.DataFrame({'id': ['A1'], 'value': [10]})
        df_sheet2 = pd.DataFrame({'id': ['B1'], 'value': [20]})
        
        input_dict = {
            'Sheet_One': df_sheet1,
            'Sheet_Two': df_sheet2
        }
        
        result = ingest_module.flatten_xlsx_dict(input_dict)
        
        # Check that we have the new tracking column
        assert 'sheet_name' in result.columns
        assert len(result) == 2
        
        # Check that the data is correctly tagged with its source sheet
        sheet1_rows = result[result['sheet_name'] == 'Sheet_One']
        assert sheet1_rows.iloc[0]['id'] == 'A1'
        
        sheet2_rows = result[result['sheet_name'] == 'Sheet_Two']
        assert sheet2_rows.iloc[0]['id'] == 'B1'


    def test_flatten_xlsx_dict_schema_mismatch(self):
        """
        Tests merging sheets that have different columns.
        
        If Sheet A has column 'col_a' and Sheet B has 'col_b', the combined table
        should have both columns. Rows from Sheet A will have empty values (NaN) 
        for 'col_b', and vice-versa.
        """
        df1 = pd.DataFrame({'col_a': [1]})
        df2 = pd.DataFrame({'col_b': [2]})
        
        result = ingest_module.flatten_xlsx_dict({'s1': df1, 's2': df2})
        
        assert 'col_a' in result.columns
        assert 'col_b' in result.columns
        
        # Verify that missing data is handled safely (pandas uses NaN/float for missing numbers)
        row_s2 = result[result['sheet_name'] == 's2'].iloc[0]
        assert pd.isna(row_s2['col_a'])
        assert row_s2['col_b'] == 2


    def test_flatten_xlsx_dict_empty_input(self):
        """
        Tests validation for empty inputs.
        
        This ensures that if we accidentally pass an empty dictionary (no sheets found),
        the function raises a ValueError instead of crashing silently or returning nothing.
        """
        with pytest.raises(ValueError) as excinfo:
            ingest_module.flatten_xlsx_dict({})
        
        assert "Input dictionary of DataFrames is empty" in str(excinfo.value)
