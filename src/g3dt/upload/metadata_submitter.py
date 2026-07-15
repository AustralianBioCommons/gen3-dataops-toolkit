import os
import sys
import time
import json
import boto3
from botocore.exceptions import BotoCoreError, ClientError
from gen3.auth import Gen3Auth
from gen3.submission import Gen3Submission
import logging
from datetime import datetime
import jwt
import requests
from typing import Any, Dict, List, Optional
import re
import pandas as pd
import uuid
from g3dt.utils.athena_utils import write_iceberg_to_db
from tenacity import retry, stop_after_attempt, wait_exponential

# redefine to use local cache in /tmp
os.environ['XDG_CACHE_HOME'] = '/tmp/.cache'

logger = logging.getLogger(__name__)

def create_boto3_session(
    aws_profile: Optional[str] = None,
    aws_region: Optional[str] = None,
):
    """
    Create and return a boto3 Session object using an optional AWS profile.

    Args:
        aws_profile (str, optional): The AWS CLI named profile to use.
            If None, uses default credentials.
        aws_region (str, optional): The AWS region to use.
            If None, uses the default region from the environment.

    Returns:
        boto3.Session: The created session instance.
    """
    logger.debug(
        "Creating boto3 session with aws_profile=%s, aws_region=%s",
        aws_profile, aws_region,
    )
    kwargs = {}
    if aws_profile:
        kwargs['profile_name'] = aws_profile
    if aws_region:
        kwargs['region_name'] = aws_region
    return boto3.Session(**kwargs)

def is_s3_uri(s3_uri: str) -> bool:
    """
    Check if the provided URI is a valid S3 URI.

    Args:
        s3_uri (str): The string to check.

    Returns:
        bool: True if the string starts with 's3://', False otherwise.
    """
    logger.debug("Checking if %s is an S3 URI.", s3_uri)
    return s3_uri.startswith("s3://")

def get_filename(file_path: str) -> str:
    """
    Extract the filename from a file path.

    Args:
        file_path (str): The full path to a file.

    Returns:
        str: The filename (with extension).
    """
    filename = file_path.split("/")[-1]
    logger.debug(
        "Extracted filename '%s' from file_path '%s'.",
        filename,
        file_path,
    )
    return filename

def get_node_from_file_path(file_path: str) -> str:
    """
    Extract the node name from a file path, assuming file is named as 'node.json'.

    Args:
        file_path (str): The file path.

    Returns:
        str: The base node name before the extension.
    """
    filename = get_filename(file_path)
    node = filename.split(".")[0]
    logger.debug("Extracted node '%s' from filename '%s'.", node, filename)
    return node

def list_metadata_jsons(metadata_dir: str) -> list:
    """
    List all .json files in a given directory.

    Args:
        metadata_dir (str): Directory containing metadata JSON files.

    Returns:
        list: List of absolute paths to all .json files in the directory.

    Raises:
        Exception: If there is an error reading the directory.
    """
    try:
        logger.info(
            "Listing .json files in metadata directory: %s",
            metadata_dir,
        )
        files = os.listdir(metadata_dir)
        return [
            os.path.abspath(os.path.join(metadata_dir, file_name))
            for file_name in files
            if file_name.endswith(".json")
        ]
    except OSError as e:
        logger.error("Error listing metadata JSONs in %s: %s", metadata_dir, e)
        raise

def find_data_import_order_file(metadata_dir: str) -> str:
    """
    Find the DataImportOrder.txt file within a directory.

    Args:
        metadata_dir (str): Directory to search in.

    Returns:
        str: Full path to the DataImportOrder.txt file.

    Raises:
        FileNotFoundError: If no such file is found.
    """
    try:
        logger.info("Searching for DataImportOrder.txt in %s", metadata_dir)
        files = [os.path.join(metadata_dir, f) for f in os.listdir(metadata_dir)]
        order_files = [f for f in files if "DataImportOrder.txt" in f]
        if not order_files:
            logger.error("No DataImportOrder.txt file found in the given directory.")
            raise FileNotFoundError(
                "No DataImportOrder.txt file found in the given directory."
            )
        logger.debug("Found DataImportOrder.txt file: %s", order_files[0])
        return order_files[0]
    except OSError as e:
        logger.error(
            "Error finding DataImportOrder.txt in %s: %s",
            metadata_dir,
            e,
        )
        raise

def list_metadata_jsons_s3(s3_uri: str, session) -> list:
    """
    List all .json files in an S3 "directory" (prefix).

    Args:
        s3_uri (str): S3 URI to the metadata directory
            (e.g. "s3://my-bucket/path/to/dir").
        session (boto3.Session): An active boto3 Session.

    Returns:
        list: List of S3 URIs for all .json files found under the prefix.
    """
    logger.info("Listing .json files in S3 metadata directory: %s", s3_uri)
    s3 = session.client('s3')
    bucket = s3_uri.split("/")[2]
    prefix = "/".join(s3_uri.split("/")[3:])
    if prefix and not prefix.endswith("/"):
        prefix += "/"  # Ensure prefix ends with a slash for directories

    objects = s3.list_objects(Bucket=bucket, Prefix=prefix)
    result = [
        f"s3://{bucket}/{obj['Key']}"
        for obj in objects.get('Contents', [])
        if obj['Key'].endswith(".json")
    ]
    logger.debug("Found %s .json files in S3 at %s", len(result), s3_uri)
    return result

def find_data_import_order_file_s3(s3_uri: str, session) -> str:
    """
    Search for the DataImportOrder.txt file in an S3 directory.

    Args:
        s3_uri (str): S3 URI specifying the directory/prefix to search.
        session (boto3.Session): An active boto3 Session.

    Returns:
        str: Full S3 URI of the found DataImportOrder.txt file.

    Raises:
        FileNotFoundError: If the file does not exist in the specified prefix.
    """
    logger.info(
        "Searching for DataImportOrder.txt in S3 metadata directory: %s",
        s3_uri,
    )
    s3 = session.client('s3')
    bucket = s3_uri.split("/")[2]
    prefix = "/".join(s3_uri.split("/")[3:])
    objects = s3.list_objects(Bucket=bucket, Prefix=prefix)
    order_files = [
        obj['Key']
        for obj in objects.get('Contents', [])
        if obj['Key'].endswith("DataImportOrder.txt")
    ]
    if not order_files:
        logger.error("No DataImportOrder.txt file found in the given S3 directory.")
        raise FileNotFoundError(
            "No DataImportOrder.txt file found in the given directory."
        )
    logger.debug(
        "Found DataImportOrder.txt file in S3: s3://%s/%s",
        bucket,
        order_files[0],
    )
    return f"s3://{bucket}/{order_files[0]}"

def read_metadata_json(file_path: str) -> dict:
    """
    Read and return a JSON file from the local file system.

    Args:
        file_path (str): Path to the .json file.

    Returns:
        dict or list: Parsed contents of the JSON file.
    """
    logger.info("Reading metadata json from local file: %s", file_path)
    with open(file_path, "r", encoding="utf-8") as f:
        data = json.load(f)
    logger.debug(
        "Read %s objects from %s",
        len(data) if isinstance(data, list) else 'object',
        file_path,
    )
    return data

def read_metadata_json_s3(s3_uri: str, session) -> dict:
    """
    Read and return JSON data from an S3 file.

    Args:
        s3_uri (str): Full S3 URI to the .json file.
        session (boto3.Session): Boto3 session.

    Returns:
        dict or list: Parsed JSON object from S3 file.
    """
    logger.info("Reading metadata json from S3 file: %s", s3_uri)
    s3 = session.client('s3')
    obj = s3.get_object(
        Bucket=s3_uri.split("/")[2],
        Key="/".join(s3_uri.split("/")[3:]),
    )
    data = json.loads(obj['Body'].read().decode('utf-8'))
    logger.debug(
        "Read %s objects from %s",
        len(data) if isinstance(data, list) else 'object',
        s3_uri,
    )
    return data

def read_data_import_order_txt_s3(s3_uri: str, session, exclude_nodes: list = None) -> list:
    """
    Read a DataImportOrder.txt file from S3 and return node order as a list, optionally excluding some nodes.

    Args:
        s3_uri (str): S3 URI to the DataImportOrder.txt file.
        session (boto3.Session): Boto3 session.
        exclude_nodes (list, optional): Node names to exclude from result.

    Returns:
        list: Node names (order as listed in file), optionally excluding nodes in exclude_nodes.

    Raises:
        ValueError: If the provided S3 URI does not point to DataImportOrder.txt.
    """
    filename = s3_uri.split("/")[-1]
    if 'DataImportOrder.txt' not in filename:
        logger.error("File %s is not a DataImportOrder.txt file", filename)
        raise ValueError(
            f"File {filename} is not a DataImportOrder.txt file"
        )
    logger.info(
        "Reading DataImportOrder.txt from S3 file: %s",
        s3_uri,
    )
    s3 = session.client('s3')
    obj = s3.get_object(
        Bucket=s3_uri.split("/")[2],
        Key="/".join(s3_uri.split("/")[3:]),
    )
    content = obj['Body'].read().decode('utf-8')
    import_order = [
        line.rstrip()
        for line in content.splitlines()
        if line.strip()
    ]
    logger.debug("Raw import order from S3 file: %s", import_order)
    if exclude_nodes is not None:
        import_order = [node for node in import_order if node not in exclude_nodes]
        logger.debug(
            "Import order after excluding nodes %s: %s",
            exclude_nodes,
            import_order,
        )
    logger.debug(
        "Final import order from S3 file %s: %s",
        s3_uri,
        import_order,
    )
    return import_order


def read_data_import_order_txt(file_path: str, exclude_nodes: list) -> list:
    """
    Read DataImportOrder.txt from local file, optionally excluding some nodes.

    Args:
        file_path (str): Path to DataImportOrder.txt.
        exclude_nodes (list): Node names to exclude from result.

    Returns:
        list: Node names, excludes specified nodes, keeps listed order.

    Raises:
        FileNotFoundError: If the file is not found.
    """
    try:
        logger.info(
            "Reading DataImportOrder.txt from local file: %s",
            file_path,
        )
        with open(file_path, "r", encoding="utf-8") as f:
            import_order = [line.rstrip() for line in f if line.strip()]
            logger.debug("Raw import order from file: %s", import_order)
            if exclude_nodes is not None:
                import_order = [
                    node for node in import_order if node not in exclude_nodes
                ]
                logger.debug(
                    "Import order after excluding nodes %s: %s",
                    exclude_nodes,
                    import_order,
                )
        logger.debug("Final import order from %s: %s", file_path, import_order)
        return import_order
    except FileNotFoundError:
        logger.error("Error: DataImportOrder.txt not found in %s", file_path)
        return []

def split_json_objects(json_list, max_size_kb=50, print_results=False) -> list:
    """
    Split a list of JSON-serializable objects into size-limited chunks.

    Each chunk/list, when JSON-serialized, will not exceed max_size_kb kilobytes.

    Args:
        json_list (list): List of JSON serializable objects.
        max_size_kb (int, optional): Max chunk size in KB. Default: 50.
        print_results (bool, optional): If True, info log the size/count per chunk. Default: False.

    Returns:
        list: List of lists. Each sublist size (JSON-serialized) <= max_size_kb.
    """
    logger.info(
        "Splitting JSON objects into max %s KB chunks. Total items: %s",
        max_size_kb,
        len(json_list),
    )

    def get_size_in_kb(obj):
        """
        Get the size in kilobytes of the JSON-serialized object.

        Args:
            obj: JSON-serializable object.

        Returns:
            float: Size of the object in kilobytes.
        """
        size_kb = sys.getsizeof(json.dumps(obj)) / 1024
        logger.debug("Calculated size: %.2f KB", size_kb)
        return size_kb

    def split_list(items):
        """
        Recursively split the list so each chunk fits within max_size_kb.

        Args:
            json_list (list): List to split.

        Returns:
            list: List of sublists.
        """
        if get_size_in_kb(items) <= max_size_kb:
            logger.debug(
                "Split length %s is within max size %s KB.",
                len(items),
                max_size_kb,
            )
            return [items]
        mid = len(items) // 2
        left_list = items[:mid]
        right_list = items[mid:]
        logger.debug(
            "Splitting list at index %s: left %s, right %s",
            mid,
            len(left_list),
            len(right_list),
        )
        return split_list(left_list) + split_list(right_list)

    split_lists = split_list(json_list)
    if print_results:
        for i, lst in enumerate(split_lists):
            logger.info(
                "List %s size: %.2f KB, contains %s objects",
                i + 1,
                get_size_in_kb(lst),
                len(lst),
            )
    logger.debug("Total splits: %s", len(split_lists))
    return split_lists

def get_gen3_api_key_aws_secret(secret_name: str, region_name: str, session) -> dict:
    """
    Retrieve a Gen3 API key stored as a secret in AWS Secrets Manager and parse it as a dict.

    Args:
        secret_name (str): Name of the AWS secret.
        region_name (str): AWS region where the secret is located.
        session (boto3.Session): Boto3 session.

    Returns:
        dict: Parsed Gen3 API key.

    Raises:
        Exception: On failure to retrieve or parse the secret.
    """
    logger.info(
        "Retrieving Gen3 API key from AWS Secrets Manager: "
        "secret_name=%s, region=%s",
        secret_name,
        region_name,
    )
    client = session.client(
        service_name='secretsmanager',
        region_name=region_name,
    )
    try:
        get_secret_value_response = client.get_secret_value(
            SecretId=secret_name,
        )
    except (BotoCoreError, ClientError) as e:
        logger.error("Error getting secret value from AWS Secrets Manager: %s", e)
        raise

    secret = get_secret_value_response['SecretString']

    try:
        secret = json.loads(secret)
        api_key = secret
        logger.debug("Retrieved Gen3 API key from secret %s", secret_name)
        return api_key
    except (json.JSONDecodeError, TypeError) as e:
        logger.error("Error parsing Gen3 API key from AWS Secrets Manager: %s", e)
        raise


def infer_api_endpoint_from_jwt(
    jwt_token: str,
    api_version: str = 'v0',
) -> str:
    """
    Extract the API endpoint URL from a JSON Web Token (JWT) credential.

    Args:
        jwt_token (str): The JSON Web Token (JWT) credential.
        api_version (str, optional): API version to append to the URL
            (default: "v0").

    Returns:
        str: The extracted API endpoint URL.
    """
    logger.info("Decoding JWT to extract API URL.")
    url = jwt.decode(
        jwt_token,
        options={"verify_signature": False},
    ).get('iss', '')
    if '/user' in url:
        url = url.split('/user')[0]
    url = f"{url}/api/{api_version}"
    logger.info("Extracted API URL from JWT: %s", url)
    return url


def create_gen3_submission_class(api_key: dict):
    """
    Create and authenticate a Gen3Submission client using an
    API key dictionary.

    The API endpoint is inferred from the JWT token embedded
    in the API key.

    Args:
        api_key (dict): The Gen3 API key as a Python dict
            (must contain 'api_key' field).

    Returns:
        Gen3Submission: An authenticated Gen3Submission object.
    """
    logger.debug("Extracting JWT token from API key dict.")
    jwt_token = api_key['api_key']
    logger.info("Inferring API endpoint from JWT token.")
    api_endpoint = infer_api_endpoint_from_jwt(jwt_token)
    logger.debug("Inferred API endpoint: %s", api_endpoint)
    logger.info(
        "Creating Gen3Submission class for endpoint: %s",
        api_endpoint,
    )
    auth = Gen3Auth(refresh_token=api_key)
    submit = Gen3Submission(endpoint=api_endpoint, auth_provider=auth)
    return submit


class MetadataSubmitter:
    def __init__(
        self,
        metadata_file_list: list,
        api_key: dict,
        project_id: str,
        data_import_order_path: str,
        database: str,
        table: str,
        athena_s3_output: str,
        workgroup: str = "primary",
        table_location: str = None,
        program_id: str = "program1",
        max_size_kb: int = 100,
        exclude_nodes: Optional[List[str]] = None,
        max_retries: int = 3,
        aws_profile: str = None,
        aws_region: str = None,
        partition_cols: Optional[List[str]] = None,
        upload_to_database: bool = True
    ):
        """
        Initialise a MetadataSubmitter for submitting a set of metadata JSON
        files to a Gen3 data commons endpoint, in order.

        **Workflow Overview:**
        1.  **Node Traversal:** The submitter iterates through
            each node defined in the `data_import_order` list.
        2.  **File Resolution:** For each node name, it locates the corresponding JSON file
            (e.g., `node.json`) from the provided file list.
        3.  **Chunking:** The JSON file is read and split into manageable chunks based on size.
        4.  **Submission:** Each chunk is submitted to the Gen3 Sheepdog API via `gen3.submission`.
        5.  **Response Handling:** The API response, which includes the `submission_id` for
            the records, is captured.
        6.  **Persistence:** The response data is flattened, converted into a DataFrame, and
            written to Parquet files in S3. These records are also registered in a specific
            upload table within the configured database for audit and tracking.

        Args:
            metadata_file_list (list): List of local file paths or S3 URIs to
                metadata .json files, one per node type.
            api_key (dict): Gen3 API key as a parsed dictionary.
            project_id (str): Gen3 project ID to submit data to (e.g., "internal-project").
            data_import_order_path (str): Path or S3 URI to DataImportOrder.txt
                specifying node submission order.
            database (str): Database name for storing the metadata upload.
                Example: "etl_test_dataops_metadata_db"
            table (str): Table name for storing the metadata upload.
                Example: "metadata_upload"
            athena_s3_output (str): S3 path for Athena query
                results output.
            workgroup (str, optional): Athena workgroup to use
                (default: "primary").
            table_location (str, optional): S3 location for the Iceberg table.
                If None, uses the default location for the database/table.
            program_id (str, optional): Gen3 program ID (default: "program1").
            max_size_kb (int, optional): Maximum size per submission chunk,
                in KB (default: 100).
            exclude_nodes (list, optional): List of node names to skip during
                submission. Defaults to ["project", "program", "acknowledgement", "publication"].
            max_retries (int, optional): Maximum number of retry attempts per
                node chunk (default: 3).
            aws_profile (str, optional): AWS CLI named profile to use for boto3
                session (default: None).
            aws_region (str, optional): AWS region to use for boto3
                session (default: None).
            partition_cols (list, optional): List of column names to partition the parquet table by.
                Defaults to ["upload_datetime"].
            upload_to_database (bool, optional): Whether to upload
                the metadata to a database. Defaults to True.
                Configured via database, table, and
                athena_s3_output.
        """
        self.metadata_file_list = metadata_file_list
        self.api_key = api_key
        self.project_id = project_id
        self.data_import_order_path = data_import_order_path
        self.database = database
        self.table = table
        self.athena_s3_output = athena_s3_output
        self.workgroup = workgroup
        self.table_location = table_location
        self.program_id = program_id
        self.max_size_kb = max_size_kb
        self.exclude_nodes = exclude_nodes or [
            "project",
            "program",
            "acknowledgement",
            "publication",
        ]
        self.max_retries = max_retries
        self.submission_results = []
        self.aws_profile = aws_profile
        self.aws_region = aws_region
        self.partition_cols = partition_cols or ["upload_datetime"]
        self.upload_to_database = upload_to_database
        self.boto3_session = None
        if self.upload_to_database:
            self.boto3_session = self._create_boto3_session()
        logger.info("MetadataSubmitter initialised.")

    def _create_gen3_submission_class(self):
        """Helper to instantiate the Gen3Submission class using the provided API key."""
        return create_gen3_submission_class(self.api_key)

    def _create_boto3_session(self):
        """Helper to create a boto3 session using the provided AWS profile."""
        return create_boto3_session(
            self.aws_profile, self.aws_region
        )

    def _flatten_submission_results(self, submission_results: List[Dict]) -> List[Dict]:
        """
        Flattens a list of Gen3 submission result dictionaries into a single
        list of entity dictionaries.

        For each submission result, this function processes its entities (if any),
        extracting the 'project_id' and 'submitter_id' from the 'unique_keys'
        field (if present) into the top-level entity dictionary for easy access.

        Any submission result that does not have a code of 200 or lacks entities
        is skipped, and a warning is logged.

        Args:
            submission_results (List[Dict]):
                A list of Gen3 submission result dictionaries, each containing at
                least a "code" and "entities" entry.

        Returns:
            List[Dict]:
                A flat list, where each element is an entity dictionary
                (with keys 'project_id' and 'submitter_id' added if available).
        """
        flat_list_dict = []
        total = len(submission_results)
        logger.info("Flattening %s submission result(s)...", total)

        for idx, obj in enumerate(submission_results, 1):
            transaction_id = obj.get("transaction_id")
            code = obj.get("code")
            if code != 200:
                logger.warning(
                    "Skipping submission result at index %s (code=%s)",
                    idx - 1,
                    code,
                )
                continue

            entities = obj.get("entities")

            if entities is None:
                logger.warning("No entities found in submission result at index %s", idx - 1)
                continue

            logger.info(
                "Processing submission result %s of %s, %s entities",
                idx,
                total,
                len(entities),
            )

            for entity in entities:
                unique_keys = entity.get("unique_keys", [{}])
                if unique_keys and isinstance(unique_keys, list):
                    keys = unique_keys[0]
                    entity["project_id"] = keys.get("project_id")
                    entity["submitter_id"] = keys.get("submitter_id")
                    entity["transaction_id"] = transaction_id
                    entity["file_path"] = obj.get("file_path", '')
                flat_list_dict.append(entity)

        # renaming cols
        for entity in flat_list_dict:
            entity["gen3_guid"] = entity.pop("id", None)
            entity["node"] = entity.pop("type", None)

        logger.info("Finished flattening. Total entities: %s", len(flat_list_dict))
        return flat_list_dict

    def _find_version_from_path(self, path: str) -> Optional[str]:
        """
        Extracts a semantic version string (e.g., '1.0.0' or 'v1.0.0') from a file path.

        Args:
            path (str): The file path to inspect.

        Returns:
            Optional[str]: The extracted version string if found, otherwise None.
        """
        version_pattern = re.compile(r"^v?(\d+\.\d+\.\d+)$")
        found_versions = []

        for segment in path.split('/'):
            match = version_pattern.match(segment)
            if match:
                found_versions.append(match.group(1))

        if not found_versions:
            return None

        if len(found_versions) > 1:
            logger.warning("more than one match found in path for version string")

        return found_versions[-1]

    def _collect_versions_from_metadata_file_list(self) -> str:
        """
        Extract and validate version information from the internal list of metadata
        file paths (self.metadata_file_list).

        Returns:
            str: The single version found in the file list.

        Raises:
            ValueError: If more than one version is found across the files,
                        or if no version is found at all.
        """
        versions = []
        for file_path in self.metadata_file_list:
            version = self._find_version_from_path(file_path)
            if version:
                versions.append(version)
        versions = list(set(versions))
        if len(versions) > 1:
            logger.error(
                "more than one version found in metadata file list: %s",
                self.metadata_file_list,
            )
            raise ValueError(
                "More than one version found in metadata file list: %s"
                % self.metadata_file_list
            )
        if not versions:
            raise ValueError(
                "No version found in metadata file list: %s" % self.metadata_file_list
            )
        return versions[0]

    def _upload_submission_results(self, submission_results: list):
        """
        Uploads the submission results to S3 and a Parquet table.

        This function performs the final step of the pipeline:
        1.  Flattens the submission response structure.
        2.  Prepares a DataFrame with metadata (upload_id, datetime, version).
        3.  Writes the DataFrame to Parquet files in S3 and registers them in the
            database configured via `self.database` and `self.table`.

        **Retry Mechanism:**
        Uses the `tenacity` library to retry the upload if it fails.
        - Stop: After `self.max_retries` attempts.
        - Wait: Exponential backoff starting at 1s, doubling up to 10s.

        Args:
            submission_results (list): List of submission results to upload.

        Configuration used (from __init__):
            database (str): e.g. "etl_test_dataops_metadata_db"
            table (str): e.g. "metadata_upload"
            athena_s3_output (str): S3 path for Athena query results output.
            workgroup (str): Athena workgroup to use.
            table_location (str): S3 location for the Iceberg table.
        """
        
        @retry(
            stop=stop_after_attempt(self.max_retries),
            wait=wait_exponential(multiplier=1, max=10)
        )
        def inner_upload():
            logger.debug("Collecting version from metadata file list.")
            version = self._collect_versions_from_metadata_file_list()
            logger.debug("Extracted version: %s", version)

            logger.debug("Inferring API endpoint from JWT.")
            api_endpoint = infer_api_endpoint_from_jwt(self.api_key['api_key'])
            logger.debug("Using API endpoint: %s", api_endpoint)

            upload_datetime = datetime.now().isoformat()
            upload_id = str(uuid.uuid4())
            logger.debug("Upload datetime: %s", upload_datetime)
            logger.debug("Generated upload ID: %s", upload_id)

            logger.debug("Flattening submission results for upload.")
            flattened_results = self._flatten_submission_results(submission_results)
            logger.debug(
                "Flattened %s submission result entries.",
                len(flattened_results),
            )

            logger.debug("Converting flattened results to DataFrame.")
            flattened_results_df = pd.DataFrame(flattened_results)
            flattened_results_df['upload_datetime'] = upload_datetime
            flattened_results_df['upload_id'] = upload_id
            flattened_results_df['api_endpoint'] = api_endpoint
            flattened_results_df['version'] = version

            logger.info(
                "Writing DataFrame to Iceberg table: "
                "database=%s, table=%s, athena_s3_output=%s",
                self.database,
                self.table,
                self.athena_s3_output,
            )
            write_iceberg_to_db(
                df=flattened_results_df,
                database=self.database,
                table=self.table,
                athena_s3_output=self.athena_s3_output,
                workgroup=self.workgroup,
                table_location=self.table_location,
                boto3_session=self.boto3_session,
            )
            logger.info(
                "\033[94m[SUCCESS]\033[0m Metadata submission results upload complete. "
                "Uploaded to database=%s, table=%s.",
                self.database,
                self.table,
            )

        # Execute the decorated inner function
        try:
            inner_upload()
        except Exception as e:
            logger.critical("Failed to upload submission results after %s attempts.", self.max_retries)
            raise e

    def _submit_data_chunks(
        self,
        split_json_list: list,
        node: str,
        gen3_submitter,
        file_path: str,
        upload_to_database: bool = True
    ) -> List[Dict]:
        """
        Submit each chunk of data (in split_json_list) for a given node to Gen3,
        using retry logic and logging on failures.

        Upon completion of each chunk (success or failure), the response is uploaded
        to the configured S3 Parquet table using `_upload_submission_results`.

        Args:
            split_json_list (list): List of JSON-serializable chunked data to
                submit.
            node (str): Name of the data node being submitted (e.g., "program").
            gen3_submitter: A Gen3Submission instance for making submissions.
            file_path (str): Path of the file that was submitted.
                Used only for data capture in the result logs.

        Returns:
            List[Dict]: List of response dictionaries for each submitted chunk.

        Raises:
            RuntimeError: If submission fails after all retry attempts for any chunk.
        """
        n_json_data = len(split_json_list)

        for index, jsn in enumerate(split_json_list):
            # Holds results for the current chunk
            current_chunk_response: List[Dict[str, Any]] = []
            progress_str = f"{index + 1}/{n_json_data}"

            submission_success = False
            last_exception: Optional[Exception] = None

            attempt = 0
            while attempt <= self.max_retries:
                try:
                    if attempt == 0:
                        logger.info(
                            "[SUBMIT]  | Project: %-10s | Node: %-12s | "
                            "Split: %-5s",
                            self.project_id,
                            node,
                            progress_str,
                        )
                    else:
                        logger.warning(
                            "[RETRY]   | Project: %-10s | Node: %-12s | "
                            "Split: %-5s | "
                            "Attempt: %s/%s",
                            self.project_id,
                            node,
                            progress_str,
                            attempt,
                            self.max_retries,
                        )

                    res = gen3_submitter.submit_record(self.program_id, self.project_id, jsn)
                    res.update({"file_path": file_path})
                    current_chunk_response.append(res)
                    submission_success = True
                    logger.info(
                        "\033[92m[SUCCESS]\033[0m | Project: %-10s | "
                        "Node: %-12s | Split: %-5s",
                        self.project_id,
                        node,
                        progress_str,
                    )
                    break  # Success

                except (
                    requests.exceptions.RequestException,
                    ValueError,
                    TypeError,
                ) as e:
                    last_exception = e
                    logger.error(
                        "Error submitting chunk %s for node '%s': %s",
                        progress_str,
                        node,
                        e,
                    )

                    if attempt < self.max_retries:
                        time.sleep(0.2)
                    else:
                        logger.critical(
                            "\033[91m[FAILED]\033[0m  | Project: %-10s | "
                            "Node: %-12s | Split: %-5s | Error: %s",
                            self.project_id,
                            node,
                            progress_str,
                            e,
                        )
                attempt += 1
            
            
            if upload_to_database:
                # Also submitting data chunk response info to s3 and parquet table
                logger.info("Submitting data chunk response info to S3 and Parquet table.")
                self._upload_submission_results(submission_results=current_chunk_response)

            if not submission_success:
                # After retries, still failed
                raise RuntimeError(
                    (
                        "Failed to submit chunk %s for node '%s' after %s attempts. "
                        "Last error: %s"
                    )
                    % (progress_str, node, self.max_retries + 1, last_exception)
                ) from last_exception

        logger.info("Finished submitting node '%s'.", node)


    def _read_data_import_order(
        self,
        data_import_order_path: str,
        exclude_nodes: List[str],
        boto3_session=None,
    ):
        """Helper to read the data import order from local disk or S3."""
        if is_s3_uri(data_import_order_path):
            session = boto3_session or self.boto3_session
            return read_data_import_order_txt_s3(
                data_import_order_path,
                session,
                exclude_nodes,
            )
        else:
            return read_data_import_order_txt(data_import_order_path, exclude_nodes)

    def _prepare_json_chunks(self, metadata_file_path: str, max_size_kb: int) -> List[List[Dict]]:
        """
        Read JSON data from a given file path and split it into chunks,
        each with a maximum size of ``max_size_kb`` kilobytes.

        Args:
            metadata_file_path (str): File path (local or S3 URI) to the JSON data.
            max_size_kb (int): Maximum allowed size (in kilobytes) for each chunk.

        Returns:
            list: A list of chunks, where each chunk is a list of dictionaries
                containing JSON data.
        """
        logger.info("Reading metadata json from %s", metadata_file_path)
        if is_s3_uri(metadata_file_path):
            session = self.boto3_session
            data = read_metadata_json_s3(metadata_file_path, session)
        else:
            data = read_metadata_json(metadata_file_path)
        return split_json_objects(data, max_size_kb)

    def _create_file_map(self):
        """
        Generate a mapping from node names to metadata file paths.

        This method infers the node name for each file in `self.metadata_file_list`
        and returns a dictionary where the keys are node names and the values
        are the corresponding file paths.

        Returns:
            dict: Dictionary mapping node names (str) to their associated metadata file paths.
        """
        file_map = {
            get_node_from_file_path(file_path): file_path
            for file_path in self.metadata_file_list
        }
        return file_map

    def submit_metadata(self, specific_node: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Submits metadata for each node defined in the data import order, except those in the exclude list.
        
        Args:
            specific_node (Optional[str]): If provided, only submits metadata for the specified node.
        
        **Detailed Process:**
        1.  **Order Resolution:** The function reads the import order to determine the sequence of nodes.
        2.  **File Mapping:** It finds the matching `node.json` file for each node in the order.
        3.  **Chunk & Submit:** For every file, the JSON content is split into chunks and submitted
            to the Sheepdog API via `gen3.submission`.
        4.  **Audit Logging:** The API response (containing `submission_id`) is flattened and
            converted to a DataFrame. This is then written to Parquet files in S3 and registered
            in the configured upload table.

        Returns:
            List[Dict[str, Any]]: A list of response dictionaries returned from the Gen3 metadata submissions.
                Each dictionary contains the response from submitting a chunk of metadata for a given node.
                The keys in the dictionary are "node_name", "response", and "status_code".
        """
        gen3_submitter = self._create_gen3_submission_class()
        data_import_order = self._read_data_import_order(
            self.data_import_order_path,
            self.exclude_nodes,
            self.boto3_session,
        )
        file_map = self._create_file_map()

        logger.info("Starting metadata submission.")
        
        if specific_node:
            if specific_node not in data_import_order:
                raise ValueError(f"Node '{specific_node}' not found in data import order.")
            data_import_order = [specific_node]

        for node in data_import_order:
            if node in self.exclude_nodes:
                logger.info("Skipping node '%s' (in exclude list).", node)
                continue
            file_path = file_map.get(node)
            if not file_path:
                logger.info("Skipping node '%s' (not present in file list).", node)
                continue

            logger.info("Processing file '%s' for node '%s'.", file_path, node)
            logger.info("Splitting JSON data into chunks.")
            json_chunks = self._prepare_json_chunks(file_path, self.max_size_kb)

            logger.info("Submitting chunks to Gen3.")
            self._submit_data_chunks(
                split_json_list=json_chunks,
                node=node,
                gen3_submitter=gen3_submitter,
                file_path=file_path,
                upload_to_database=self.upload_to_database
            )
            

