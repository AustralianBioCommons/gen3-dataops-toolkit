import pytest
from unittest.mock import MagicMock, patch, call
import os
from urllib import parse

# Assume the module is available at this path
from g3dt.upload.upload_synthdata_s3 import (
    upload_file_with_tags,
    get_synth_files,
    get_node_name,
    get_study_id,
    upload_synth_folder_to_s3,
)

MODULE_PATH = "g3dt.upload.upload_synthdata_s3"

class TestUploadFileWithTags:
    """
    Tests for the upload_file_with_tags function, which uploads files to S3 with 
    specified tags. These tests verify:
      - That a new S3 client is created and used when one isn't provided,
      - That an existing client is used directly,
      - That file-not-found errors are properly caught and logged.

    Each scenario checks that the correct arguments are passed to the AWS SDK
    and proper error handling/logging occurs, which is important to ensure that
    files are uploaded correctly and that error conditions are visible for debugging.
    """
    @patch(f"{MODULE_PATH}.boto3.client")
    def test_upload_success_with_new_client(self, mock_boto_client):
        """
        This test verifies that upload_file_with_tags will create its own boto3 S3 client
        if one isn't passed in and call the S3 'upload_file' method with the right parameters.

        Steps:
          - Mocks boto3.client to return a MagicMock S3 client.
          - Calls upload_file_with_tags with no explicit s3_client argument.
          - Asserts that boto3.client("s3") is called.
          - Asserts that upload_file is invoked with:
              Filename: the local source file
              Bucket: destination bucket
              Key: S3 object key
              ExtraArgs: dict with properly url-encoded tags
        """
        mock_s3 = MagicMock()
        mock_boto_client.return_value = mock_s3
        tags = {"team": "data", "project": "ACDC"}
        
        upload_file_with_tags("local/file.txt", "my-bucket", "s3/key", tags)

        mock_boto_client.assert_called_with("s3")
        mock_s3.upload_file.assert_called_with(
            Filename="local/file.txt",
            Bucket="my-bucket",
            Key="s3/key",
            ExtraArgs={"Tagging": parse.urlencode(tags)},
        )

    def test_upload_success_with_existing_client(self):
        """
        This test ensures upload_file_with_tags will use an explicitly provided s3_client.

        Steps:
          - Provides a MagicMock as s3_client.
          - Calls upload_file_with_tags, passing in tags.
          - Verifies that s3_client.upload_file is called with the correct arguments,
            including proper tag formatting.
          - No boto3.client creation is expected in this scenario.
        """
        mock_s3 = MagicMock()
        tags = {"env": "test"}

        upload_file_with_tags("local/data.json", "my-bucket", "data/key", tags, s3_client=mock_s3)
        
        mock_s3.upload_file.assert_called_with(
            Filename="local/data.json",
            Bucket="my-bucket",
            Key="data/key",
            ExtraArgs={"Tagging": parse.urlencode(tags)},
        )

    @patch(f"{MODULE_PATH}.logger")
    def test_file_not_found(self, mock_logger):
        """
        This test checks error handling for missing source files.

        Scenario:
          - Simulates a FileNotFoundError being raised when upload_file is called.
          - Expects upload_file_with_tags to catch the exception.
          - Verifies that an error is logged (rather than the exception crashing the process).

        This ensures robust handling for cases where a local file might have been deleted
        or moved before upload.
        """
        mock_s3 = MagicMock()
        mock_s3.upload_file.side_effect = FileNotFoundError
        
        upload_file_with_tags("nonexistent.txt", "bucket", "key", {}, s3_client=mock_s3)
        
        mock_logger.error.assert_called_with("The file was not found: nonexistent.txt")

class TestGetSynthFiles:
    """
    Tests for the get_synth_files utility, which is responsible for recursively
    discovering all files beneath a provided folder path.

    - This test confirms that the utility correctly traverses all nested directories,
      collecting files but not folders.
    - Uses mocking for os.walk to control the test directory structure.
    - Verifies output file list against expected OS-agnostic file paths.
    """

    @patch("os.walk")
    def test_recursive_file_collection(self, mock_os_walk):
        """
        This test ensures that get_synth_files will return all files by
        traversing a folder structure recursively.

        Steps:
          - Mocks os.walk to simulate a parent folder with a subfolder and files in each.
          - Calls get_synth_files.
          - Asserts the output matches the union of all files (relative paths),
            using os.path.join so the test is platform-agnostic.
        """
        synth_folder = "/path/to/synth"
        mock_os_walk.return_value = [
            (synth_folder, ["subfolder"], ["file1.json"]),
            (f"{synth_folder}/subfolder", [], ["file2.json", "config.yaml"]),
        ]

        files = get_synth_files(synth_folder)

        # os.path.join used for OS-agnostic comparison
        expected_files = [
            "file1.json",
            os.path.join("/subfolder", "file2.json"),
            os.path.join("/subfolder", "config.yaml"),
        ]

        assert sorted(files) == sorted(expected_files)

class TestPathHelpers:
    """
    Tests for helper functions get_node_name and get_study_id, which extract
    the node (file's stem) and the study id (first folder) from file paths.

    get_node_name:
      - Should extract the filename without extension, regardless of directory depth.
    get_study_id:
      - Should return the first part of a path (before first '/'), or
        the filename itself if no folder exists.

    These utilities are essential for tagging and organizing synthdata files based on their path.
    """
    @pytest.mark.parametrize("path, expected", [
        ("study_A/data/node_name.json", "node_name"),
        ("study_B/node_v2.json", "node_v2"),
        ("node_only.csv", "node_only"),
    ])
    def test_get_node_name(self, path, expected):
        """
        For various file paths, ensures get_node_name extracts only the base filename without extension.
        Example: "study_X/data/my_node.json" -> "my_node"
        """
        assert get_node_name(path) == expected

    @pytest.mark.parametrize("path, expected", [
        ("STUDY_ID_1/data/file.json", "STUDY_ID_1"),
        ("study-123/sub/file.json", "study-123"),
        ("root_file.json", "root_file.json"),  # If no '/', returns the full string
    ])
    def test_get_study_id(self, path, expected):
        """
        For various file paths, ensures get_study_id returns the folder before the first '/',
        or the file name itself if there is no directory.
        Example: "STUDY_ID_1/data/file.json" -> "STUDY_ID_1"
        """
        assert get_study_id(path) == expected

class TestUploadSynthFolderToS3:
    """
    Integration-style tests for upload_synth_folder_to_s3, which uploads a folder
    of synthetic data files to S3 and applies a set of tags to each.

    What this test validates:
      - The function filters out non-JSON files (like README.md) and warns about them.
      - The function uses get_study_id and get_node_name to generate the right tags.
      - The function formats S3 object keys using the correct submission date and paths.
      - Calls to the actual S3 upload logic happen as expected for each valid file.
    """

    @patch(f"{MODULE_PATH}.upload_file_with_tags")
    @patch(f"{MODULE_PATH}.get_synth_files")
    @patch(f"{MODULE_PATH}.logger")
    def test_e2e_flow(self, mock_logger, mock_get_synth_files, mock_upload_file):
        """
        This test simulates an end-to-end upload of a synth data folder.

        Steps:
          - get_synth_files is mocked to supply a list of mock "files", including both valid
            JSONs and a non-JSON which must be skipped.
          - Calls upload_synth_folder_to_s3 with fixed arguments for local folder, bucket, etc.
          - Checks that:
               - The warning for skipping README.md (a non-JSON) appears.
               - upload_file_with_tags is called for each valid .json file with the correct arguments:
                   * Full local file path
                   * Bucket name
                   * S3 key in the standard YYYY-MM-DD_synthetic_metadata/.. format (formatted date)
                   * Tags dict with ingest flag, submission_date, study_id, node name, and data_release_version.
          - Uses assert_has_calls to validate both calls happened with correct args, in any order.

        This comprehensive test ensures the production upload logic is robust even in the
        face of non-uploadable files and the tag/S3 path logic is correct.
        """
        local_folder = "/synth/data"
        bucket = "synth-bucket"
        study = "PROJ-1"
        date = "2025_10_28"
        release_ver = "v1.1"

        mock_get_synth_files.return_value = [
            os.path.join("STUDY_A", "node1.json"),
            "README.md",
            os.path.join("STUDY_B", "data", "node2.json"),
        ]
        
        upload_synth_folder_to_s3(
            local_folder_path=local_folder,
            bucket_name=bucket,
            study_id=study, # Note: this arg is overridden by get_study_id
            submission_date=date,
            data_release_version=release_ver,
        )

        # Check that README is skipped
        mock_logger.warning.assert_called_with("Skipping non-JSON file: README.md")

        # Check calls to upload_file_with_tags
        expected_calls = [
            call(
                local_file_path=os.path.join(local_folder, "STUDY_A", "node1.json"),
                bucket_name=bucket,
                s3_object_key=f"2025-10-28_synthetic_metadata/{os.path.join('STUDY_A', 'node1.json')}",
                tags={
                    "ingest": "true",
                    "submission_date": "2025-10-28",
                    "study_id": "STUDY_A",
                    "node": "node1",
                    "data_release_version": release_ver,
                },
            ),
            call(
                local_file_path=os.path.join(local_folder, "STUDY_B", "data", "node2.json"),
                bucket_name=bucket,
                s3_object_key=f"2025-10-28_synthetic_metadata/{os.path.join('STUDY_B', 'data', 'node2.json')}",
                tags={
                    "ingest": "true",
                    "submission_date": "2025-10-28",
                    "study_id": "STUDY_B",
                    "node": "node2",
                    "data_release_version": release_ver,
                },
            ),
        ]
        mock_upload_file.assert_has_calls(expected_calls, any_order=True)

