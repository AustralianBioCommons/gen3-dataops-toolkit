import pytest
from unittest.mock import MagicMock, patch, mock_open, call
import pandas as pd
import json
import io
from botocore.exceptions import ClientError

# Module path
MODULE_PATH = "g3dt.validate.validate"

# Import functions to test
from g3dt.validate.validate import (
    parse_s3_uri,
    get_s3_client,
    read_json_from_s3,
    write_bytes_to_s3,
    download_s3_file,
    list_s3_objects,
    load_schema_from_s3_uri,
    write_schema_to_temp_file,
    create_metadata_table,
    get_latest_validation_for_study,
    download_s3_files_to_temp_dir,
    write_df_to_s3,
    validate_pipeline
)


class TestParseS3Uri:
    @pytest.mark.parametrize("s3_uri, expected", [
        ("s3://my-bucket/my-key", ("my-bucket", "my-key")),
        ("s3://bucket/path/to/file.json", ("bucket", "path/to/file.json")),
        ("s3://test/a/b/c/d.txt", ("test", "a/b/c/d.txt")),
    ])
    def test_parse_valid_s3_uri(self, s3_uri, expected):
        """Test parsing valid S3 URIs."""
        assert parse_s3_uri(s3_uri) == expected

    @pytest.mark.parametrize("invalid_uri", [
        "http://bucket/key",
        "bucket/key",
        "s3://bucket-only",
        "s3://",
    ])
    def test_parse_invalid_s3_uri(self, invalid_uri):
        """Test that invalid S3 URIs raise ValueError."""
        with pytest.raises(ValueError):
            parse_s3_uri(invalid_uri)


class TestGetS3Client:
    @patch(f"{MODULE_PATH}.boto3.client")
    def test_get_s3_client_success(self, mock_boto_client):
        """Test successful S3 client creation."""
        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3
        
        result = get_s3_client()
        
        mock_boto_client.assert_called_with('s3')
        assert result == mock_s3

    @patch(f"{MODULE_PATH}.boto3.client")
    def test_get_s3_client_failure(self, mock_boto_client):
        """Test S3 client creation failure."""
        mock_boto_client.side_effect = Exception("AWS credentials not found")
        
        with pytest.raises(Exception):
            get_s3_client()


class TestReadJsonFromS3:
    @patch(f"{MODULE_PATH}.get_s3_client")
    def test_read_json_success(self, mock_get_client):
        """Test successful JSON reading from S3."""
        mock_s3 = MagicMock()
        mock_get_client.return_value = mock_s3
        
        test_data = {"key": "value", "nested": {"id": 123}}
        mock_s3.get_object.return_value = {"Body": io.BytesIO(json.dumps(test_data).encode())}
        
        result = read_json_from_s3("s3://bucket/file.json")
        
        assert result == test_data
        mock_s3.get_object.assert_called_with(Bucket="bucket", Key="file.json")

    @patch(f"{MODULE_PATH}.get_s3_client")
    def test_read_json_client_error(self, mock_get_client):
        """Test handling of ClientError when reading JSON from S3."""
        mock_s3 = MagicMock()
        mock_get_client.return_value = mock_s3
        mock_s3.get_object.side_effect = ClientError(
            {"Error": {"Code": "NoSuchKey"}}, "GetObject"
        )
        
        with pytest.raises(ClientError):
            read_json_from_s3("s3://bucket/nonexistent.json")


class TestWriteBytesToS3:
    @patch(f"{MODULE_PATH}.get_s3_client")
    def test_write_bytes_success(self, mock_get_client):
        """Test successful writing of bytes to S3."""
        mock_s3 = MagicMock()
        mock_get_client.return_value = mock_s3
        
        test_data = b"test data"
        write_bytes_to_s3(test_data, "s3://bucket/output.txt")
        
        mock_s3.put_object.assert_called_with(
            Bucket="bucket", Key="output.txt", Body=test_data
        )

    @patch(f"{MODULE_PATH}.get_s3_client")
    def test_write_bytes_failure(self, mock_get_client):
        """Test handling of exception during bytes write."""
        mock_s3 = MagicMock()
        mock_get_client.return_value = mock_s3
        mock_s3.put_object.side_effect = Exception("Write failed")
        
        with pytest.raises(Exception):
            write_bytes_to_s3(b"data", "s3://bucket/file.txt")


class TestDownloadS3File:
    @patch(f"{MODULE_PATH}.get_s3_client")
    def test_download_success(self, mock_get_client):
        """Test successful file download from S3."""
        mock_s3 = MagicMock()
        mock_get_client.return_value = mock_s3
        
        download_s3_file("s3://bucket/file.json", "/tmp/local.json")
        
        mock_s3.download_file.assert_called_with("bucket", "file.json", "/tmp/local.json")

    @patch(f"{MODULE_PATH}.get_s3_client")
    def test_download_client_error(self, mock_get_client):
        """Test handling of ClientError during download."""
        mock_s3 = MagicMock()
        mock_get_client.return_value = mock_s3
        mock_s3.download_file.side_effect = ClientError(
            {"Error": {"Code": "404"}}, "DownloadFile"
        )
        
        with pytest.raises(ClientError):
            download_s3_file("s3://bucket/missing.json", "/tmp/local.json")


class TestListS3Objects:
    @patch(f"{MODULE_PATH}.get_s3_client")
    def test_list_objects_success(self, mock_get_client):
        """Test listing S3 objects."""
        mock_s3 = MagicMock()
        mock_get_client.return_value = mock_s3
        
        mock_paginator = MagicMock()
        mock_s3.get_paginator.return_value = mock_paginator
        mock_paginator.paginate.return_value = [
            {"Contents": [{"Key": "file1.json"}, {"Key": "file2.json"}]},
            {"Contents": [{"Key": "file3.json"}]}
        ]
        
        results = list(list_s3_objects("s3://bucket/prefix"))
        
        assert len(results) == 3
        assert results[0] == ("bucket", "file1.json")
        assert results[2] == ("bucket", "file3.json")


class TestLoadSchemaFromS3Uri:
    @patch(f"{MODULE_PATH}.read_json_from_s3")
    def test_load_schema_success(self, mock_read_json):
        """Test successful schema loading."""
        test_schema = {"type": "object", "properties": {}}
        mock_read_json.return_value = test_schema
        
        result = load_schema_from_s3_uri("s3://bucket/schema.json")
        
        assert result == test_schema
        mock_read_json.assert_called_with("s3://bucket/schema.json")


class TestWriteSchemaToTempFile:
    @patch("builtins.open", new_callable=mock_open)
    @patch(f"{MODULE_PATH}.os.makedirs")
    @patch(f"{MODULE_PATH}.tempfile._get_candidate_names")
    def test_write_schema_to_temp(self, mock_tempfile, mock_makedirs, mock_file):
        """Test writing schema to temporary file."""
        mock_tempfile.return_value = iter(["temp123"])
        test_schema = {"key": "value"}
        
        result_path = write_schema_to_temp_file(test_schema)
        
        assert "temp123" in result_path
        assert "schema.json" in result_path
        mock_makedirs.assert_called_once()


class TestCreateMetadataTable:
    @patch(f"{MODULE_PATH}.list_s3_objects")
    def test_create_metadata_table_success(self, mock_list_objects):
        """Test creation of metadata table from S3 objects."""
        mock_list_objects.return_value = [
            ("bucket", "study_id=STUDY1/validation_id=V1/table_name=patients/snapshot_id=100/data.json"),
            ("bucket", "study_id=STUDY2/validation_id=V2/table_name=samples/snapshot_id=101/results.json"),
        ]
        
        result = create_metadata_table("s3://bucket/prefix")
        
        assert len(result) == 2
        assert result[0]["study_id"] == "STUDY1"
        assert result[0]["validation_id"] == "V1"
        assert result[1]["table_name"] == "samples"
        assert result[1]["snapshot_id"] == "101"


class TestGetLatestValidationForStudy:
    def test_get_latest_validation_success(self):
        """Test getting latest validation ID for a study."""
        metadata = pd.DataFrame({
            "study_id": ["STUDY1", "STUDY1", "STUDY2"],
            "validation_id": ["V1", "V2", "V1"],
            "table_name": ["patients", "patients", "samples"]
        })
        
        result_df, latest_id = get_latest_validation_for_study(metadata, "STUDY1")
        
        assert latest_id == "V2"
        assert len(result_df) == 1
        assert result_df["validation_id"].iloc[0] == "V2"

    def test_get_latest_validation_no_data(self):
        """Test handling when no data exists for study."""
        metadata = pd.DataFrame({
            "study_id": ["STUDY1"],
            "validation_id": ["V1"],
            "table_name": ["patients"]
        })
        
        result_df, latest_id = get_latest_validation_for_study(metadata, "NONEXISTENT")
        
        assert latest_id is None
        assert len(result_df) == 0


class TestDownloadS3FilesToTempDir:
    @patch(f"{MODULE_PATH}.download_s3_file")
    @patch(f"{MODULE_PATH}.tempfile.mkdtemp")
    @patch(f"{MODULE_PATH}.os.makedirs")
    def test_download_files_success(self, mock_makedirs, mock_mkdtemp, mock_download):
        """Test downloading multiple S3 files to temp directory."""
        mock_mkdtemp.return_value = "/tmp/test123"
        
        s3_uris = [
            "s3://bucket/prefix/study_validation_file1.json",
            "s3://bucket/prefix/study_validation_file2.json"
        ]
        
        temp_dir, downloaded = download_s3_files_to_temp_dir(s3_uris)
        
        assert "/tmp/test123" in temp_dir
        assert mock_download.call_count == 2


class TestWriteDfToS3:
    @patch(f"{MODULE_PATH}.write_bytes_to_s3")
    def test_write_df_to_s3(self, mock_write_bytes):
        """Test writing DataFrame to S3 as CSV."""
        df = pd.DataFrame({"col1": [1, 2], "col2": ["a", "b"]})
        
        write_df_to_s3(df, "s3://bucket/output", "test.csv")
        
        mock_write_bytes.assert_called_once()
        call_args = mock_write_bytes.call_args
        assert "s3://bucket/output/test.csv" == call_args[0][1]


class TestValidatePipeline:
    @patch(f"{MODULE_PATH}.write_iceberg_to_db")
    @patch(f"{MODULE_PATH}.write_df_to_s3")
    @patch(f"{MODULE_PATH}.gen3_validator.validate.validate_list_dict")
    @patch(f"{MODULE_PATH}.gen3_validator.ResolveSchema")
    @patch(f"{MODULE_PATH}.download_s3_files_to_temp_dir")
    @patch(f"{MODULE_PATH}.get_latest_validation_for_study")
    @patch(f"{MODULE_PATH}.create_metadata_table")
    @patch(f"{MODULE_PATH}.write_schema_to_temp_file")
    @patch(f"{MODULE_PATH}.load_schema_from_s3_uri")
    @patch(f"{MODULE_PATH}.os.listdir")
    @patch("builtins.open", new_callable=mock_open, read_data='[{"id": "test"}]')
    def test_validate_pipeline_creates_correct_dataframe(
        self,
        mock_file,
        mock_listdir,
        mock_load_schema,
        mock_write_schema,
        mock_create_metadata,
        mock_get_latest,
        mock_download,
        mock_resolver_class,
        mock_validate_list_dict,
        mock_write_df,
        mock_write_iceberg,
    ):
        """
        Test that validate_pipeline creates a DataFrame with the expected columns and values.
        
        This test verifies the end-to-end behavior of the validate_pipeline function by mocking
        all external dependencies (S3 operations, schema resolution, validation) and checking
        that the resulting DataFrame passed to write_df_to_s3 contains the correct structure.
        
        What this test does:
            1. Mocks the schema loading and resolution process
            2. Mocks the metadata table creation from S3 validation files
            3. Mocks the validation results returned by gen3_validator
            4. Calls validate_pipeline with test parameters
            5. Verifies the DataFrame written to S3 has correct columns and values
        
        Expected Inputs (mocked):
            - study_id: Study identifier (e.g. "ausdiab") - used as "test_study" in this test
            - schema_s3_uri: Full S3 URI to the JSON schema file (e.g. "s3://bucket/schema.json")
            - validation_s3_uri: S3 prefix containing validation result files (.json)
                (e.g. "s3://bucket/validation/")
            - write_back_root: S3 prefix to write generated artefacts/reports to
                (e.g. "s3://bucket/results/")
            - glue_database: Glue database to register tables into (e.g. "test_db")
            - athena_s3_output: S3 URI for Athena query results and temporary staging
                (e.g. "s3://bucket/athena-output/")
            - root_node: Root node in the schema graph for link validation (default: "project")
            
        Mocked Dependencies:
            - Schema from S3: A simple JSON schema object {"type": "object"}
            - Metadata table: A list containing study metadata with S3 URIs
            - Latest validation: A DataFrame with study_id, validation_id, and s3_uri
            - Downloaded files: A tuple of (temp_dir, list_of_file_paths)
            - Validation results: A list of dicts with validation outcomes (PASS/FAIL)
        
        Expected Outputs:
            - DataFrame written to S3 should contain these columns:
                - node: The schema node being validated (e.g., "medical_history", "sample")
                - index: Row index in the source data
                - validation_result: "PASS" or "FAIL"
                - invalid_key: The key that failed validation (None if passed)
                - schema_path: Path in schema where validation failed
                - validator: Type of validator that failed
                - validator_value: Expected value from validator
                - validation_error: Human-readable error message
                - validation_id: Unique ID for this validation run
                - study_id: The study being validated
                - schema_version: Version of the schema used
            - DataFrame should have 2 rows (one FAIL, one PASS from mock data)
            - Iceberg table should be written with correct database and table
        """
        mock_load_schema.return_value = {"type": "object"}
        mock_write_schema.return_value = "/tmp/schema.json"
        mock_create_metadata.return_value = [
            {"study_id": "test_study", "s3_uri": "s3://bucket/file.json"}
        ]
        
        mock_latest_df = pd.DataFrame({
            "study_id": ["test_study"],
            "validation_id": ["VAL123"],
            "s3_uri": ["s3://bucket/prefix/file.json"]
        })
        mock_get_latest.return_value = (mock_latest_df, "VAL123")
        mock_download.return_value = ("/tmp/downloaded", ["/tmp/downloaded/file.json"])
        mock_listdir.return_value = ["file.json"]
        
        mock_resolver = MagicMock()
        mock_resolver.schema_resolved = {"resolved": True}
        mock_resolver.get_schema_version.return_value = "1.0.0"
        mock_resolver_class.return_value = mock_resolver
        
        mock_validate_list_dict.return_value = [
            {
                "node": "medical_history",
                "index": 0,
                "validation_result": "FAIL",
                "invalid_key": "root",
                "schema_path": "required",
                "validator": "required",
                "validator_value": ["submitter_id", "type"],
                "validation_error": "'submitter_id' is a required property"
            },
            {
                "node": "sample",
                "index": 1,
                "validation_result": "PASS",
                "invalid_key": None,
                "schema_path": None,
                "validator": None,
                "validator_value": None,
                "validation_error": None
            }
        ]
        
        validate_pipeline(
            study_id="test_study",
            schema_s3_uri="s3://bucket/schema.json",
            validation_s3_uri="s3://bucket/validation/",
            write_back_root="s3://bucket/results/",
            glue_database="test_db",
            athena_s3_output="s3://bucket/athena-output/",
        )
        
        mock_write_df.assert_called_once()
        df_arg = mock_write_df.call_args[0][0]
        
        expected_columns = {
            "node", "index", "validation_result", "invalid_key", "schema_path",
            "validator", "validator_value", "validation_error",
            "validation_id", "study_id", "schema_version"
        }
        assert set(df_arg.columns) == expected_columns

        expected_column_order = [
            "validation_id", "index", "node", "study_id", "validation_result",
            "invalid_key", "schema_path", "validator", "validator_value",
            "validation_error", "schema_version"
        ]
        assert list(df_arg.columns) == expected_column_order
        
        
        assert df_arg["node"].iloc[0] == "medical_history"
        assert df_arg["index"].iloc[0] == 0
        assert df_arg["validation_result"].iloc[0] == "FAIL"
        assert df_arg["invalid_key"].iloc[0] == "root"
        assert df_arg["schema_path"].iloc[0] == "required"
        assert df_arg["validator"].iloc[0] == "required"
        assert df_arg["validator_value"].iloc[0] == ["submitter_id", "type"]
        assert df_arg["validation_error"].iloc[0] == "'submitter_id' is a required property"
        
        assert df_arg["validation_id"].iloc[0] == "VAL123"
        assert df_arg["study_id"].iloc[0] == "test_study"
        assert df_arg["schema_version"].iloc[0] == "1.0.0"
        assert len(df_arg) == 2
        
        mock_write_iceberg.assert_called_once()
        iceberg_call_kwargs = mock_write_iceberg.call_args[1]
        assert iceberg_call_kwargs["database"] == "test_db"
        assert iceberg_call_kwargs["table"] == "full_validation_results"
        assert iceberg_call_kwargs["athena_s3_output"] == "s3://bucket/athena-output/"
