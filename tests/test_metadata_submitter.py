import pytest
import json
import sys
from unittest.mock import MagicMock, patch, call
from requests.exceptions import RequestException
import boto3
from botocore.exceptions import ClientError

# Import the module under test
import g3dt.upload.metadata_submitter as metadata_submitter
from g3dt.upload.metadata_submitter import MetadataSubmitter

# ==========================================
# Fixtures (Reusable Setup)
# ==========================================

@pytest.fixture
def mock_boto_session():
    """
    Creates a mock boto3 Session.

    This ensures that when the code calls `boto3.Session()`, it gets a fake object
    instead of trying to connect to real AWS credentials on your machine.
    
    It returns distinct mock clients for 's3' and 'secretsmanager'.
    """
    session = MagicMock(name="boto_session")
    s3_client = MagicMock(name="s3_client")
    secrets_client = MagicMock(name="secrets_client")

    def client_side_effect(service_name, **kwargs):
        if service_name == 's3':
            return s3_client
        if service_name == 'secretsmanager':
            return secrets_client
        return MagicMock()

    session.client.side_effect = client_side_effect
    return session

@pytest.fixture
def mock_gen3_submission():
    """
    Mocks the Gen3Submission class.

    This intercepts any attempt to instantiate Gen3Submission and returns a mock object.
    This allows us to verify if `submit_record` was called without actually hitting the Gen3 API.
    """
    with patch('g3dt.upload.metadata_submitter.Gen3Submission') as mock_class:
        mock_instance = mock_class.return_value
        yield mock_instance

@pytest.fixture
def submitter_instance(mock_boto_session):
    """
    Returns an instantiated MetadataSubmitter with AWS dependencies mocked.

    This fixture creates a 'ready-to-use' class instance so we don't have to
    write the __init__ boilerplate code in every single test.
    """
    with patch.object(MetadataSubmitter, '_create_boto3_session', return_value=mock_boto_session):
        submitter = MetadataSubmitter(
            metadata_file_list=["s3://bucket/v1.0.0/node.json"],
            api_key={"api_key": "fake_jwt"},
            project_id="TEST_PROJECT",
            data_import_order_path="s3://bucket/DataImportOrder.txt",
            database="test_db",
            table="test_table",
            athena_s3_output="s3://out/athena-output/",
            workgroup="primary",
            max_retries=3
        )
        return submitter

# ==========================================
# Unit Tests: Helper Functions (Standalone)
# ==========================================

def test_is_s3_uri_valid():
    """
    Verifies that `is_s3_uri` correctly identifies S3 paths versus local paths.

    Input: "s3://bucket/file"
    Expected: True

    Input: "/local/file"
    Expected: False
    """
    assert metadata_submitter.is_s3_uri("s3://bucket/file") is True
    assert metadata_submitter.is_s3_uri("/local/file") is False

def test_get_filename_extraction():
    """
    Verifies that `get_filename` splits the path and returns just the file name.

    Input: "s3://my-bucket/folder/data.json"
    Expected: "data.json"
    """
    assert metadata_submitter.get_filename("s3://b/f.json") == "f.json"

def test_get_node_from_file_path_extraction():
    """
    Verifies that `get_node_from_file_path` extracts the node name (filename without extension).

    Input: "/path/to/program.json"
    Expected: "program"
    """
    assert metadata_submitter.get_node_from_file_path("/p/program.json") == "program"

def test_list_metadata_jsons_s3(mock_boto_session):
    """
    Verifies that `list_metadata_jsons_s3` filters the S3 bucket listing
    to return only .json files, ignoring other file types like .txt.

    Mock S3 Contents: ['f/a.json', 'f/b.txt']
    Expected Output: ['s3://b/f/a.json']
    """
    s3_client = mock_boto_session.client('s3')
    s3_client.list_objects.return_value = {
        'Contents': [
            {'Key': 'f/a.json'},
            {'Key': 'f/b.txt'}
        ]
    }
    result = metadata_submitter.list_metadata_jsons_s3("s3://b/f", mock_boto_session)
    assert result == ["s3://b/f/a.json"]

def test_find_data_import_order_file_s3_success(mock_boto_session):
    """
    Verifies that `find_data_import_order_file_s3` locates the correct file
    and returns its full S3 URI.

    Mock S3 Contents: ['path/DataImportOrder.txt']
    Expected Output: "s3://b/path/DataImportOrder.txt"
    """
    s3_client = mock_boto_session.client('s3')
    s3_client.list_objects.return_value = {
        'Contents': [{'Key': 'path/DataImportOrder.txt'}]
    }
    result = metadata_submitter.find_data_import_order_file_s3("s3://b/path", mock_boto_session)
    assert result == "s3://b/path/DataImportOrder.txt"

def test_find_data_import_order_file_s3_missing(mock_boto_session):
    """
    Verifies that `find_data_import_order_file_s3` raises FileNotFoundError
    if the file does not exist in the bucket.

    Mock S3 Contents: ['other.txt']
    Expected Outcome: raise FileNotFoundError
    """
    s3_client = mock_boto_session.client('s3')
    s3_client.list_objects.return_value = {'Contents': [{'Key': 'other.txt'}]}
    with pytest.raises(FileNotFoundError):
        metadata_submitter.find_data_import_order_file_s3("s3://b/p", mock_boto_session)

def test_read_data_import_order_txt_s3(mock_boto_session):
    """
    Verifies that `read_data_import_order_txt_s3` reads the file content,
    splits it by newlines, and returns a clean list of nodes.

    Mock File Content: b"program\nproject"
    Expected Output: ["program", "project"]
    """
    s3_client = mock_boto_session.client('s3')
    mock_body = MagicMock()
    mock_body.read.return_value = b"program\nproject"
    s3_client.get_object.return_value = {'Body': mock_body}
    
    result = metadata_submitter.read_data_import_order_txt_s3(
        "s3://bucket/DataImportOrder.txt", 
        mock_boto_session
    )
    assert result == ["program", "project"]

def test_split_json_objects_logic():
    """
    Verifies the logic for splitting a large list into smaller chunks.
    
    We force a split by setting `max_size_kb` to be smaller than the size of our items.
    
    Input: A list of 3 large strings (each > 1KB).
    Configuration: max_size_kb = 1.5 KB.
    Expected Outcome: The list is split into multiple chunks (at least 2), because 
                      3 items (~3KB total) cannot fit into a single 1.5KB chunk.
    """
    # Create strings ~1KB each
    large_string = "x" * 1024
    data = [{"a": large_string}, {"b": large_string}, {"c": large_string}]
    
    chunks = metadata_submitter.split_json_objects(data, max_size_kb=1.5)
    
    assert len(chunks) > 1
    
    # Verify we didn't lose any data during the split
    flattened = [item for chunk in chunks for item in chunk]
    assert len(flattened) == 3

# ==========================================
# Unit Tests: Class Methods
# ==========================================

def test_flatten_submission_results_logic(submitter_instance):
    """
    Verifies `_flatten_submission_results`. It should take a nested API response
    and flatten it into a simple list of dictionaries, renaming key fields for clarity.

    Input: A Gen3 API response with nested 'entities' and 'unique_keys'.
    Expected Output: A flat dict where 'id' -> 'gen3_guid' and 'unique_keys' are unpacked.
    """
    complex_response = [{
        "code": 200, "transaction_id": 1, 
        "entities": [{"id": "g1", "type": "case", "unique_keys": [{"project_id": "P"}]}]
    }]
    
    flat = submitter_instance._flatten_submission_results(complex_response)
    
    assert len(flat) == 1
    assert flat[0]['gen3_guid'] == "g1"
    assert flat[0]['project_id'] == "P"

def test_find_version_from_path(submitter_instance):
    """
    Verifies that `_find_version_from_path` extracts semantic versions from file paths.
    It expects a standard 3-part version (Major.Minor.Patch).

    Input: "s3://b/v1.0.0/f.json" -> Expected: "1.0.0"
    Input: "s3://b/1.2.3/f.json"  -> Expected: "1.2.3"
    """
    assert submitter_instance._find_version_from_path("s3://b/v1.0.0/f.json") == "1.0.0"
    assert submitter_instance._find_version_from_path("s3://b/1.2.3/f.json") == "1.2.3"

def test_collect_versions_mixed_error(submitter_instance):
    """
    Verifies that `_collect_versions_from_metadata_file_list` raises an error if 
    the provided files belong to different versions. This prevents accidental mixing of data releases.

    Input Files: ["v1.0.0/a.json", "v2.0.0/b.json"]
    Expected Outcome: raise ValueError
    """
    submitter_instance.metadata_file_list = ["v1.0.0/a.json", "v2.0.0/b.json"]
    with pytest.raises(ValueError):
        submitter_instance._collect_versions_from_metadata_file_list()

def test_upload_submission_results_success(submitter_instance):
    """
    Verifies that `_upload_submission_results` successfully prepares the data
    and calls the parquet writer.

    We mock `write_iceberg_to_db` to verify it gets called once.
    """
    with patch('g3dt.upload.metadata_submitter.write_iceberg_to_db') as mock_write, \
         patch('g3dt.upload.metadata_submitter.infer_api_endpoint_from_jwt'), \
         patch.object(submitter_instance, '_collect_versions_from_metadata_file_list', return_value="1.0"):
        
        submitter_instance._upload_submission_results([{"p": 1}])
        mock_write.assert_called_once()

def test_upload_submission_results_max_retries_exceeded(submitter_instance):
    """
    Verifies the retry logic in `_upload_submission_results`.
    If the upload fails consistently, it should retry up to `max_retries` and then fail.

    Setup: Mock writer to always raise Exception. Set max_retries = 2.
    Expected Outcome: The function calls the writer multiple times (>= 2) and finally raises Exception.
    """
    with patch('g3dt.upload.metadata_submitter.write_iceberg_to_db') as mock_write, \
         patch('g3dt.upload.metadata_submitter.infer_api_endpoint_from_jwt'), \
         patch.object(submitter_instance, '_collect_versions_from_metadata_file_list', return_value="1.0"):

        mock_write.side_effect = Exception("Fail")
        submitter_instance.max_retries = 2

        with pytest.raises(Exception):
            submitter_instance._upload_submission_results([])
        
        # Verify retries happened (Initial attempt + Retries)
        assert mock_write.call_count >= 2

def test_submit_data_chunks_hard_failure(submitter_instance, mock_gen3_submission):
    """
    Verifies `_submit_data_chunks` behavior on catastrophic API failure.
    Even if the API fails, we expect the code to try and log/upload whatever result it has.

    Setup: Mock API to always raise RequestException.
    Expected Outcome: Raises RuntimeError (stops processing), but `_upload_submission_results` is called to log the attempt.
    """
    chunks = [[{"data": 1}]]
    mock_gen3_submission.submit_record.side_effect = RequestException("Fail")
    submitter_instance.max_retries = 1
    
    with patch.object(submitter_instance, '_upload_submission_results') as mock_upload:
        with pytest.raises(RuntimeError):
            submitter_instance._submit_data_chunks(chunks, "node", mock_gen3_submission, "f")
        
        # Verify that we attempted to upload the failure logs
        mock_upload.assert_called()

def test_submit_metadata_orchestration(submitter_instance):
    """
    Verifies the full orchestration workflow: `submit_metadata`.
    It ensures the method reads the order, finds files, and calls the submitter loop.

    Setup:
      - Order: ['program']
      - File Map: {'program': '...'}
      - Exclude Nodes: [] (Nothing skipped)

    Expected Outcome: `_submit_data_chunks` is called exactly once for the 'program' node.
    """
    # Important: Clear default exclusions so our test node 'program' isn't skipped
    submitter_instance.exclude_nodes = []
    
    mock_order = ['program']
    mock_file_map = {'program': 's3://b/program.json'}
    mock_chunks = [[{'d': 1}]]

    with patch.object(submitter_instance, '_create_gen3_submission_class'), \
         patch.object(submitter_instance, '_read_data_import_order', return_value=mock_order) as mock_read, \
         patch.object(submitter_instance, '_create_file_map', return_value=mock_file_map) as mock_map, \
         patch.object(submitter_instance, '_prepare_json_chunks', return_value=mock_chunks), \
         patch.object(submitter_instance, '_submit_data_chunks') as mock_submit:

        submitter_instance.submit_metadata()

        mock_read.assert_called_once()
        mock_map.assert_called_once()
        
        # Verify the submission loop ran exactly once for 'program'
        assert mock_submit.call_count == 1
        assert mock_submit.call_args.kwargs['node'] == 'program'


def test_submit_metadata_specific_node(submitter_instance):
    """
    Verifies that `submit_metadata` correctly processes only the specified node
    when `specific_node` parameter is provided.

    Setup:
      - Order: ['program', 'project', 'case']
      - File Map: {'program': '...', 'project': '...', 'case': '...'}
      - specific_node: 'project'

    Expected Outcome: `_submit_data_chunks` is called exactly once for the 'project' node only.
    """
    submitter_instance.exclude_nodes = []
    
    mock_order = ['program', 'project', 'case']
    mock_file_map = {
        'program': 's3://b/program.json',
        'project': 's3://b/project.json',
        'case': 's3://b/case.json'
    }
    mock_chunks = [[{'d': 1}]]

    with patch.object(submitter_instance, '_create_gen3_submission_class'), \
         patch.object(submitter_instance, '_read_data_import_order', return_value=mock_order), \
         patch.object(submitter_instance, '_create_file_map', return_value=mock_file_map), \
         patch.object(submitter_instance, '_prepare_json_chunks', return_value=mock_chunks), \
         patch.object(submitter_instance, '_submit_data_chunks') as mock_submit:

        submitter_instance.submit_metadata(specific_node='project')

        # Verify the submission loop ran exactly once for 'project' only
        assert mock_submit.call_count == 1
        assert mock_submit.call_args.kwargs['node'] == 'project'


def test_submit_metadata_specific_node_not_found(submitter_instance):
    """
    Verifies that `submit_metadata` raises ValueError when the specified node
    is not found in the data import order.

    Setup:
      - Order: ['program', 'project']
      - specific_node: 'nonexistent_node'

    Expected Outcome: Raises ValueError with appropriate message.
    """
    submitter_instance.exclude_nodes = []
    
    mock_order = ['program', 'project']
    mock_file_map = {'program': 's3://b/program.json', 'project': 's3://b/project.json'}

    with patch.object(submitter_instance, '_create_gen3_submission_class'), \
         patch.object(submitter_instance, '_read_data_import_order', return_value=mock_order), \
         patch.object(submitter_instance, '_create_file_map', return_value=mock_file_map):

        with pytest.raises(ValueError, match="Node 'nonexistent_node' not found in data import order"):
            submitter_instance.submit_metadata(specific_node='nonexistent_node')
