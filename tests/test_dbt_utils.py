import pytest
import json
import yaml
from pathlib import Path
from unittest.mock import patch, mock_open

# Import the functions from the specified module path
from g3dt.utils.dbt_utils import load_json, get_model_names

# --- Tests for load_json ---

def test_load_json_success(tmp_path):
    """
    Test that a valid JSON file is loaded and parsed correctly by `load_json`.
    
    This test covers the happy path for the `load_json` function:
      - It first creates a dictionary object (`json_content`) with nested structures.
      - It writes this dictionary as JSON to a file located in a temporary directory, which is isolated for the test run.
      - The test then calls `load_json` with the path to the file, expecting it to return a Python object matching the original input.
      - The assertion checks if the returned object is equal to the dictionary that was dumped.
    
    This test ensures that basic file reading and JSON deserialization are handled as expected, and the function works for well-formed, existing JSON files.
    """
    json_content = {"key": "value", "nested": {"id": 1}}
    file_path = tmp_path / "test.json"
    with open(file_path, 'w') as f:
        json.dump(json_content, f)

    result = load_json(file_path)
    assert result == json_content

@patch('g3dt.utils.dbt_utils.logger')
def test_load_json_file_not_exist(mock_logger, tmp_path):
    """
    Test that `load_json` handles a non-existent file gracefully.
    
    This test verifies that when we try to load a JSON file that does not exist:
      - The function returns None instead of raising an exception.
      - A warning is logged using the logger, with a message that includes the missing file's path.
      
    Steps:
      - A file path is constructed in a temporary directory, but no file is created.
      - The function is called with this path.
      - The test asserts that the function returns None.
      - It also checks, using the logger mock, that the correct warning message has been logged.
      
    This ensures robust and predictable handling of missing files, avoiding unhandled exceptions.
    """
    non_existent_path = tmp_path / "non_existent.json"
    result = load_json(non_existent_path)

    assert result is None
    mock_logger.warning.assert_called_with(f"JSON file does not exist: {non_existent_path}")

@patch('g3dt.utils.dbt_utils.logger')
def test_load_json_invalid_json(mock_logger, tmp_path):
    """
    Test that `load_json` returns None and logs an error if the JSON file is malformed.
    
    This test checks the behavior when attempting to parse an invalid JSON file:
      - It creates a file with intentionally invalid JSON content (single quotes instead of double quotes).
      - It calls `load_json` with the file path.
      - The function is expected to return None.
      - The test also checks that an error is logged using the logger mock.
      - It verifies that the error message contains the file path, indicating it is relevant to the error that just occurred.
    
    This covers the error handling pathway for file content that does not conform to JSON syntax.
    """
    invalid_json_path = tmp_path / "invalid.json"
    invalid_json_path.write_text("{'key': 'value'}")  # Invalid JSON with single quotes

    result = load_json(invalid_json_path)

    assert result is None
    mock_logger.error.assert_called_once()
    # Check that the error message contains the expected file path and exception info
    assert str(invalid_json_path) in mock_logger.error.call_args[0][0]

# --- Tests for get_model_names ---

@patch("builtins.open", new_callable=mock_open)
@patch("yaml.safe_load")
def test_get_model_names_success(mock_safe_load, mock_file):
    """
    Test that `get_model_names` extracts model names from a valid DBT schema YAML file.
    
    The test simulates the normal scenario where the schema YAML file:
      - Contains a 'models' key, under which is a list of dictionaries.
      - Each dictionary represents a model and contains a 'name' key.
    Test process:
      - Mocks `yaml.safe_load` to return a schema dictionary containing two models ('model_one', 'model_two').
      - Calls `get_model_names` with a dummy path.
      - Asserts that the returned list contains exactly the names of the models in the right order.
      - Also asserts the file was opened in read mode from the specified path.

    This ensures the function's primary job—reading and parsing DBT model names from schema files—works as designed.
    """
    schema_content = {
        'version': 2,
        'models': [
            {'name': 'model_one', 'description': 'A model.'},
            {'name': 'model_two'}
        ]
    }
    mock_safe_load.return_value = schema_content

    model_names = get_model_names("dummy/path/schema.yml")

    assert model_names == ['model_one', 'model_two']
    mock_file.assert_called_with("dummy/path/schema.yml", mode='r')

@patch("builtins.open", new_callable=mock_open)
@patch("yaml.safe_load")
@patch('g3dt.utils.dbt_utils.logger')
def test_get_model_names_no_models_key(mock_logger, mock_safe_load, mock_file):
    """
    Test that `get_model_names` handles missing 'models' key in a DBT schema YAML file.
    
    In some cases, the schema YAML might not include the 'models' key (it may contain 'sources' or other keys instead).
    This test verifies:
      - When the 'models' key is absent, the function returns an empty list.
      - An error is logged stating that the 'models' key was not found, including the file path.
    The YAML content returned by the mock does not include a 'models' key, simulating this error scenario.
    
    This test is important for ensuring robust error handling and clear developer feedback during schema ingestion.
    """
    schema_content = {'version': 2, 'sources': []}  # No 'models' key
    mock_safe_load.return_value = schema_content
    
    result = get_model_names("dummy/path/schema.yml")
    
    assert result == []
    mock_logger.error.assert_called_with("'models' key not found in schema file: dummy/path/schema.yml")

@patch("builtins.open", new_callable=mock_open)
@patch("yaml.safe_load", side_effect=yaml.YAMLError("Parsing failed"))
@patch('g3dt.utils.dbt_utils.logger')
def test_get_model_names_invalid_yaml(mock_logger, mock_safe_load, mock_file):
    """
    Test that `get_model_names` returns an empty list and logs an error for invalid YAML.
    
    This test covers handling of corrupted or invalid YAML files that cannot be parsed:
      - The test mocks `yaml.safe_load` to raise a `yaml.YAMLError`, simulating a syntax or parsing error in the YAML.
      - The function is called with a (dummy) schema file path.
      - Asserts that it returns an empty list.
      - Checks that an error was logged, and the message contains 'Failed to load model names'.
    
    This test ensures the function is resilient to schema file corruption and gives useful error feedback.
    """
    result = get_model_names("dummy/path/invalid_schema.yml")
    
    assert result == []
    mock_logger.error.assert_called_once()
    assert "Failed to load model names" in mock_logger.error.call_args[0][0]

@patch("builtins.open", side_effect=IOError("Permission denied"))
@patch('g3dt.utils.dbt_utils.logger')
def test_get_model_names_file_read_error(mock_logger, mock_file):
    """
    Test that `get_model_names` returns an empty list and logs an error if the file cannot be opened due to permissions.
    
    This test simulates the case where the system is unable to open the specified schema file, e.g. due to a permissions issue:
      - The built-in `open` is patched to raise an IOError with the message 'Permission denied' every time it is called.
      - The function under test will attempt and fail to open the file.
      - The test checks that the function returns an empty list.
      - It also asserts that an error is logged,
        that the message includes 'Failed to load model names',
        and that the specific error ('Permission denied') is present in the log message.
    
    This protects against accidental pipeline failure or confusing errors due to AWS or file system permissions problems.
    """
    result = get_model_names("dummy/path/protected_schema.yml")
    
    assert result == []
    mock_logger.error.assert_called_once()
    assert "Failed to load model names" in mock_logger.error.call_args[0][0]
    assert "Permission denied" in mock_logger.error.call_args[0][0]
