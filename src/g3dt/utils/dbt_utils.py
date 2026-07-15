import logging
import json
from pathlib import Path
from typing import Optional, Dict, Any, List
import yaml

# Set up logger for this module
logger = logging.getLogger(__name__)

# ----------------- dbt Artifact Helpers -----------------


def load_json(path: Path) -> Optional[Dict[str, Any]]:
    """
    Load a JSON file from the given path and return its contents as a dictionary.

    Args:
        path (Path): The path to the JSON file.

    Returns:
        Optional[Dict[str, Any]]: The loaded JSON data as a dictionary, or None if the file does not exist
                                  or loading fails.

    Logs:
        - Warning if the file does not exist.
        - Error if loading the JSON fails.
    """
    try:
        if not path.exists():
            logger.warning(f"JSON file does not exist: {path}")
            return None
        with path.open() as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Failed to load JSON from {path}: {e}")
        return None


def get_model_names(dbt_schema_path) -> list:
    """
    Extracts and returns a list of model names from a dbt schema YAML file.

    Args:
        dbt_schema_path (str or Path): Path to the dbt schema.yml file.

    Returns:
        list: List of model names found in the schema file. Returns an empty list if none found or on error.

    Raises:
        None. All exceptions are caught and logged.

    Logs:
        - Error if the file cannot be read or parsed, or if the expected structure is missing.
    """
    try:
        with open(dbt_schema_path, mode='r') as f:
            schema = yaml.safe_load(f)
        if not schema or 'models' not in schema:
            logger.error(f"'models' key not found in schema file: {dbt_schema_path}")
            return []
        model_names = [model.get('name') for model in schema['models'] if 'name' in model]
        return model_names
    except Exception as e:
        logger.error(f"Failed to load model names from schema file {dbt_schema_path}: {e}")
        return []

