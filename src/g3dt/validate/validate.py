import gen3_validator
import os
import tempfile
import json
import boto3
import pandas as pd
import logging
import awswrangler as wr
import io
from botocore.exceptions import ClientError
from g3dt.utils.athena_utils import write_iceberg_to_db

logger = logging.getLogger(__name__)

def parse_s3_uri(s3_uri: str):
    """
    Parse an Amazon S3 URI into a bucket and key.

    :param s3_uri: The S3 URI to parse. Example: 's3://my-bucket/my-folder/mykey.json'
    :type s3_uri: str

    :returns: Tuple containing the S3 bucket name and key.
    :rtype: tuple[str, str]

    :raises ValueError: If the URI does not start with 's3://' or is malformed.

    .. note::
        The returned key will include all path elements after the bucket, including folders.

    **Example**

        >>> parse_s3_uri('s3://my-bucket/data/file.csv')
        ('my-bucket', 'data/file.csv')
    """
    if not s3_uri.startswith("s3://"):
        logger.error("s3_uri must start with 's3://'")
        raise ValueError("s3_uri must start with 's3://'")
    _, _, bucket_and_key = s3_uri.partition("s3://")
    bucket, _, key = bucket_and_key.partition("/")
    if not bucket or not key:
        logger.error("Malformed s3_uri: must be of the form 's3://bucket/key'")
        raise ValueError("Malformed s3_uri: must be of the form 's3://bucket/key'")
    return bucket, key

def get_s3_client():
    """
    Create and return a boto3 client for interacting with Amazon S3.

    :returns: boto3 S3 client object for making S3 API requests.
    :rtype: boto3.client

    :raises Exception: If the boto3 client could not be created.

    .. note::
        In larger codebases, you might want to cache and reuse S3 clients
        to maximize performance and conserve resources.
    """
    try:
        return boto3.client('s3')
    except Exception as e:
        logger.error(f"Failed to create boto3 S3 client: {e}")
        raise

def read_json_from_s3(s3_uri: str):
    """
    Download and parse a JSON file from S3.

    :param s3_uri: Full S3 URI to the JSON file. Example: 's3://bucket/file.json'
    :type s3_uri: str

    :returns: Dictionary representation of the parsed JSON file.
    :rtype: dict

    :raises ClientError: If AWS S3 reports an error reading the object.
    :raises Exception: If JSON parsing fails or any other exception occurs.

    **Example**

        >>> data = read_json_from_s3('s3://acdc-datasets/study.json')
        >>> print(data['title'])
    """
    try:
        bucket, key = parse_s3_uri(s3_uri)
        s3 = get_s3_client()
        obj = s3.get_object(Bucket=bucket, Key=key)
        return json.load(obj['Body'])
    except ClientError as ce:
        logger.error(f"ClientError reading JSON from {s3_uri}: {ce}")
        raise
    except Exception as e:
        logger.error(f"Failed to read JSON from {s3_uri}: {e}")
        raise

def write_bytes_to_s3(bytes_data, s3_uri: str):
    """
    Write bytes (or a UTF-8 string) to a file in S3.

    :param bytes_data: The data to write. Can be a str (encoded as UTF-8) or bytes.
    :type bytes_data: Union[str, bytes]
    :param s3_uri: S3 URI to write the data to, e.g. 's3://bucket/path/file.txt'
    :type s3_uri: str

    :raises Exception: If writing fails.

    **Example**

        >>> write_bytes_to_s3('example content', 's3://bucket/out.txt')
    """
    try:
        bucket, key = parse_s3_uri(s3_uri)
        s3 = get_s3_client()
        s3.put_object(Bucket=bucket, Key=key, Body=bytes_data)
        logger.info(f"Successfully wrote to {s3_uri}")
    except Exception as e:
        logger.error(f"Failed to write bytes to {s3_uri}: {e}")
        raise

def download_s3_file(s3_uri: str, local_path: str):
    """
    Download a file from S3 and save it to the local filesystem.

    :param s3_uri: Full S3 URI to file to download.
    :type s3_uri: str
    :param local_path: Absolute or relative path where file will be saved locally.
    :type local_path: str

    :raises ClientError: If the file cannot be retrieved from S3.
    :raises Exception: For filesystem write errors and other issues.

    **Example**

        >>> download_s3_file('s3://bucket/data.csv', '/tmp/my_local_copy.csv')
    """
    bucket, key = parse_s3_uri(s3_uri)
    s3 = get_s3_client()
    try:
        logger.info(f"Downloading {s3_uri} to {local_path}")
        s3.download_file(bucket, key, local_path)
        logger.debug(f"Downloaded: {local_path}")
    except ClientError as ce:
        logger.error(f"ClientError while downloading {s3_uri} to {local_path}: {ce}")
        raise
    except Exception as e:
        logger.error(f"Failed to download {s3_uri} to {local_path}: {e}")
        raise

def list_s3_objects(s3_uri: str):
    """
    List all objects/files under a given S3 prefix.

    :param s3_uri: S3 URI for either a "directory" or a prefix to search.
                   Must start with 's3://'.
                   Example: 's3://my-bucket/folder/subfolder/'
    :type s3_uri: str

    :yields: Tuples of (bucket, key) for each object/file found.
    :rtype: Iterator[tuple[str, str]]

    :raises ClientError: If listing S3 objects fails.

    **Example**

        >>> for bucket, key in list_s3_objects('s3://bucket/validation/'):
        ...     print(key)
    """
    try:
        bucket, prefix = parse_s3_uri(s3_uri)
        if prefix and not prefix.endswith("/"):
            prefix += "/"
        s3 = get_s3_client()
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=bucket, Prefix=prefix):
            contents = page.get("Contents", [])
            for obj in contents:
                yield bucket, obj["Key"]
    except ClientError as ce:
        logger.error(f"ClientError listing objects for {s3_uri}: {ce}")
        raise
    except Exception as e:
        logger.error(f"Error listing objects for {s3_uri}: {e}")
        raise

def load_schema_from_s3_uri(s3_uri: str) -> dict:
    """
    Load a Gen3 JSON schema from S3 as a Python dictionary.

    :param s3_uri: The full S3 URI to the schema JSON file.
    :type s3_uri: str

    :returns: Dictionary representation of the schema.
    :rtype: dict

    :raises Exception: If there are errors reading or parsing the file.

    **Example**

        >>> schema = load_schema_from_s3_uri('s3://bucket/schemas/project_schema.json')
    """
    logger.info(f"Loading schema from S3 URI: {s3_uri}")
    try:
        schema = read_json_from_s3(s3_uri)
        logger.info(f"Schema loaded successfully from {s3_uri}")
        return schema
    except Exception as e:
        logger.error(f"Could not load schema from {s3_uri}: {e}")
        raise

def write_schema_to_temp_file(schema: dict, filename: str = "schema.json") -> str:
    """
    Write a schema dictionary to a temp file (as formatted JSON).

    :param schema: The schema data, typically loaded from S3.
    :type schema: dict
    :param filename: The filename to use within the temporary directory. Default: "schema.json".
    :type filename: str, optional

    :returns: The full absolute file path to the written file.
    :rtype: str

    :raises Exception: If directory creation or file writing fails.

    **Example**

        >>> path = write_schema_to_temp_file(schema_dict)
        >>> print(open(path).read())
    """
    try:
        temp_dir = os.path.join("/tmp", next(tempfile._get_candidate_names()))
        os.makedirs(temp_dir, exist_ok=True)
        schema_path = os.path.join(temp_dir, filename)
        logger.info(f"Writing schema to temp file: {schema_path}")
        with open(schema_path, "w", encoding="utf-8") as f:
            json.dump(schema, f, indent=2)
        logger.info(f"Schema written to: {schema_path}")
        return os.path.abspath(schema_path)
    except Exception as e:
        logger.error(f"Failed to write schema to temp file: {e}")
        raise

def create_metadata_table(s3_uri: str) -> list[dict]:
    """
    Scan an S3 location for JSON files matching a specific directory structure
    and assemble metadata for each found file.

    :param s3_uri: S3 prefix to search for validation result files.
                   Should point to root folder for validation result hierarchy.
    :type s3_uri: str

    :returns: A list of metadata dicts, one per .json file found under the prefix.
              Each dict contains: study_id, validation_id, table_name, snapshot_id,
              file_name, and s3_uri.
    :rtype: list[dict]

    :raises Exception: If S3 listing/parsing fails.

    .. note::
        Only .json files matching expected S3 path pattern will be included.
        Ignores unrelated files.

    **Example**

        >>> table = create_metadata_table('s3://bucket/validation/')
        >>> print(table[0]['s3_uri'])
    """
    import re
    logger.info(f"Creating metadata table from S3 URI: {s3_uri}")
    result = []
    regex = re.compile(
        r"study_id=([^/]+)/validation_id=([^/]+)/table_name=([^/]+)/snapshot_id=([^/]+)/([^/]+\.json)$"
    )
    try:
        for bucket, key in list_s3_objects(s3_uri):
            if not key.endswith(".json"):
                continue
            match = regex.search(key)
            if match:
                study_id, validation_id, table_name, snapshot_id, file_name = match.groups()
                entry = {
                    "study_id": study_id,
                    "validation_id": validation_id,
                    "table_name": table_name,
                    "snapshot_id": snapshot_id,
                    "file_name": file_name,
                    "s3_uri": f"s3://{bucket}/{key}"
                }
                logger.debug(f"Matched file entry: {entry}")
                result.append(entry)
        logger.info(f"Finished building metadata table. {len(result)} records found.")
        return result
    except Exception as e:
        logger.error(f"Error occurred while creating metadata table from {s3_uri}: {e}")
        raise

def get_latest_validation_for_study(metadata_table: pd.DataFrame, study_id: str):
    """
    Find the most recent validation (by validation_id) for a given study within a metadata table.

    :param metadata_table: DataFrame containing at least columns 'study_id' and 'validation_id'.
    :type metadata_table: pandas.DataFrame
    :param study_id: The study identifier to filter for.
    :type study_id: str

    :returns: Tuple of (DataFrame of latest validation's rows, validation_id as string).
              Both empty DataFrame and None if study is not present.
    :rtype: tuple[pandas.DataFrame, Optional[str]]

    :raises Exception: If filtering or max computation fails.

    **Example**

        >>> df, valid_id = get_latest_validation_for_study(table, "ausdiab")
        >>> print(valid_id)
    """
    logger.info(f"Selecting latest validation for study_id: {study_id}")
    try:
        if 'study_id' not in metadata_table.columns:
            logger.warning("No 'study_id' column in metadata_table.")
            return pd.DataFrame(), None
        filtered_table = metadata_table[metadata_table['study_id'] == study_id]
        if not filtered_table.empty:
            latest_validation_id = filtered_table['validation_id'].max()
            logger.info(f"Latest validation_id for study {study_id}: {latest_validation_id}")
            result_table = filtered_table[filtered_table['validation_id'] == latest_validation_id]
        else:
            logger.warning(f"No entries found for study_id {study_id}")
            result_table = pd.DataFrame()
            latest_validation_id = None
        logger.debug(f"{len(result_table)} records returned for latest validation_id.")
        return result_table, latest_validation_id
    except Exception as e:
        logger.error(f"Error attempting to get latest validation for study {study_id}: {e}")
        raise

def download_s3_files_to_temp_dir(s3_uris: list[str], temp_dir_name: str = "to_validate"):
    """
    Download a set of S3 files to a new temporary directory.

    :param s3_uris: List of S3 URIs to download.
    :type s3_uris: list[str]
    :param temp_dir_name: Name of the subdirectory to use inside the temp dir.
    :type temp_dir_name: str, optional (default: "to_validate")

    :returns: (target_temp_dir, downloaded_files)
              - target_temp_dir: Path to directory containing files
              - downloaded_files: List of absolute paths of successfully downloaded files.
    :rtype: tuple[str, list[str]]

    :raises Exception: If temporary folder creation or downloads fail.

    .. note::
        Each file is downloaded under a sanitized file name (see logic in code).
        Skips files if download fails.

    **Example**

        >>> d, files = download_s3_files_to_temp_dir(['s3://bucket/a.json'], 'data')
        >>> print(files)
    """
    logger.info(f"Downloading {len(s3_uris)} S3 files to temp dir named '{temp_dir_name}'")
    try:
        base_tempdir = tempfile.mkdtemp()
        target_temp_dir = os.path.join(base_tempdir, temp_dir_name)
        os.makedirs(target_temp_dir, exist_ok=True)
        logger.debug(f"Target temp dir for downloads: {target_temp_dir}")
        downloaded_files = []
        for s3_uri in s3_uris:
            if not s3_uri.startswith("s3://"):
                logger.warning(f"Skipping invalid s3_uri (not an s3 path): {s3_uri}")
                continue
            _, _, path = s3_uri.partition("s3://")
            file_name = os.path.basename(path.split("/", 1)[1] if "/" in path else path)
            parts = file_name.split("_")
            if len(parts) > 2:
                final_file_name = "_".join(parts[2:])
            else:
                final_file_name = file_name
            dest_path = os.path.join(target_temp_dir, final_file_name)
            try:
                download_s3_file(s3_uri, dest_path)
                downloaded_files.append(dest_path)
            except Exception as e:
                logger.warning(f"Skipping failed download for {s3_uri}: {e}")
                continue
        logger.info(f"Total files downloaded: {len(downloaded_files)}")
        return target_temp_dir, downloaded_files
    except Exception as e:
        logger.error(f"Failed to download S3 files to temp dir: {e}")
        raise

def write_df_to_s3(df: pd.DataFrame, s3_uri: str, filename: str, sep: str = ",", index: bool = False):
    """
    Write a pandas DataFrame to S3 as a CSV file.

    :param df: DataFrame to serialize.
    :type df: pandas.DataFrame
    :param s3_uri: The S3 prefix/folder to write the file to.
    :type s3_uri: str
    :param filename: The desired file's name within the S3 location.
    :type filename: str
    :param sep: Separator used in CSV. Default is ','.
    :type sep: str, optional
    :param index: Whether to include index in CSV file. Default is False.
    :type index: bool, optional

    :raises Exception: On DataFrame serialization or upload errors.

    **Example**

        >>> write_df_to_s3(df, 's3://bucket/validation/', 'my_results.csv')
    """
    logger.info(f"Writing DataFrame to S3: {s3_uri}, filename: {filename}")
    try:
        if s3_uri.endswith("/"):
            s3_uri = s3_uri[:-1]
        s3_path = f"{s3_uri}/{filename}"
        csv_buffer = io.StringIO()
        df.to_csv(csv_buffer, sep=sep, index=index)
        write_bytes_to_s3(csv_buffer.getvalue(), s3_path)
    except Exception as e:
        logger.error(f"Failed to write DataFrame to S3 ({s3_uri}, {filename}): {e}")
        raise





def count_unlinked_foreign_keys(linkage_results_dict: dict) -> int:
    """
    Count the number of unlinked foreign keys in a linkage results dictionary.

    The input should be the results dictionary produced by
    `gen3_validator.Linkage().validate_links()`.

    :param linkage_results_dict: Dictionary mapping foreign key fields to lists of unlinked key values.
    :type linkage_results_dict: dict

    :returns: Total count of unlinked foreign key values across all fields.
    :rtype: int
    """
    invalid_keys = 0
    for k, v in linkage_results_dict.items():
        if len(v) > 0:
            invalid_keys += len(v)
    return invalid_keys

def truncate_linkage_results(linkage_results_dict: dict) -> dict:
    """
    Truncate linkage results dictionary to only include fields with unlinked foreign keys.

    The input should be the results dictionary produced by
    `gen3_validator.Linkage().validate_links()`.

    :param linkage_results_dict: Dictionary mapping foreign key fields to lists of unlinked key values.
    :type linkage_results_dict: dict

    :returns: Dictionary with only fields with unlinked foreign keys.
    :rtype: dict
    """
    for k, v in linkage_results_dict.items():
        if len(v) > 50:
            v = v[:50]
            v.append("WARN: too many unlinked keys, only showing first 50")
    return linkage_results_dict


def validate_pipeline(
    study_id: str,
    schema_s3_uri: str,
    validation_s3_uri: str,
    write_back_root: str,
    glue_database: str,
    athena_s3_output: str,
    workgroup: str = "primary",
    root_node: str = "project",
) -> None:
    """
    Orchestrate the validation workflow for a study: load + resolve schema, find the latest
    validation artefacts, validate, and persist results to S3 and Athena/Glue.

    Args:
        study_id: Study identifier (e.g. "ausdiab").
        schema_s3_uri: Full S3 URI to the JSON schema file.
        validation_s3_uri: S3 prefix containing validation result files (.json).
        write_back_root: S3 prefix to write generated artefacts/reports to.
        glue_database: Glue database to register tables into.
        athena_s3_output: S3 URI for Athena query results and temporary staging.
        workgroup: Athena workgroup to use. Defaults to "primary".
        root_node: Root node in the schema graph for link validation.

    Raises:
        RuntimeError: When expected inputs are missing or a pipeline step fails.
    """
    # NOTE: root_node is currently unused in the original implementation.
    # Keeping it in the signature for forward compatibility.

    try:
        schema = load_schema_from_s3_uri(schema_s3_uri)
        schema_path = write_schema_to_temp_file(schema)
        logger.info("Schema loaded and written to temp file: %s", schema_path)
    except Exception as e:
        logger.exception("Schema loading/writing failed.")
        raise RuntimeError("Schema loading/writing failed.") from e  

    try:
        logger.info("Instantiating schema resolver with schema path: %s", schema_path)
        resolver = gen3_validator.ResolveSchema(schema_path=schema_path)
        resolver.resolve_schema()
        logger.info("Schema resolved.")
    except Exception as e:
        logger.exception("Failed to instantiate or resolve schema.")
        raise RuntimeError("Schema resolution failed.") from e

    try:
        metadata_table = pd.DataFrame(create_metadata_table(validation_s3_uri))
        logger.info("Metadata table created from S3 (%s rows).", len(metadata_table))
    except Exception as e:
        logger.exception("Failed to create metadata table.")
        raise RuntimeError("Metadata table creation failed.") from e

    try:
        latest_metadata, latest_validation_id = get_latest_validation_for_study(metadata_table, study_id)
        if latest_metadata is None or latest_metadata.empty:
            raise RuntimeError(f"No latest validation files found for study '{study_id}'.")

        latest_validation_s3_uris = latest_metadata["s3_uri"].tolist()
        logger.info("Latest validation id: %s", latest_validation_id)
        logger.info("Latest validation file count: %s", len(latest_validation_s3_uris))

        downloaded_files_dir, _downloaded_files = download_s3_files_to_temp_dir(latest_validation_s3_uris)
        files_in_dir = os.listdir(downloaded_files_dir)
        logger.info("Downloaded files dir: %s", downloaded_files_dir)
        for name in files_in_dir:
            logger.info(" - %s", name)
    except Exception as e:
        logger.exception("Failed to retrieve/download latest validation files.")
        raise RuntimeError("Retrieval/download of validation files failed.") from e

    try:
        logger.info("Reading JSON validation files from: %s", downloaded_files_dir)

        data_by_file = {}
        for name in files_in_dir:
            if not name.endswith(".json"):
                continue
            full_path = os.path.join(downloaded_files_dir, name)
            with open(full_path, "r") as fh:
                data_by_file[name] = json.load(fh)

        logger.info("Validating %s JSON file(s).", len(data_by_file))

        results: list[dict] = []
        for filename, obj in data_by_file.items():
            logger.info("Validating %s", filename)
            results.extend(gen3_validator.validate.validate_list_dict(obj, resolver.schema_resolved))

        logger.info("Getting Schema Version")
        schema_version = resolver.get_schema_version(schema = schema)
        
        full_validation_results_df = pd.DataFrame(results)
        full_validation_results_df["validation_id"] = latest_validation_id
        full_validation_results_df["study_id"] = study_id
        full_validation_results_df["schema_version"] = schema_version
        
        full_validation_results_df = full_validation_results_df[[
            "validation_id",
            "index",
            "node",
            "study_id",
            "validation_result",
            "invalid_key",
            "schema_path",
            "validator",
            "validator_value",
            "validation_error",
            "schema_version"
        ]]

        logger.info("Validation completed (%s rows).", len(full_validation_results_df))
    except Exception as e:
        logger.exception("Validation failed.")
        raise RuntimeError("Validation failed.") from e

    write_back_s3_uri = (
        f"{write_back_root.rstrip('/')}/study_id={study_id}/validation_id={latest_validation_id}/"
    )

    try:
        logger.info("Writing CSV results to S3: %s", write_back_s3_uri)
        write_df_to_s3(full_validation_results_df, write_back_s3_uri, "full_validation_results.csv")
    except Exception as e:
        logger.exception("Failed to write CSV validation results to S3.")
        raise RuntimeError("Writing CSV results failed.") from e

    try:
        logger.info(
            "Writing Parquet to Glue DB '%s', table '%s'.",
            glue_database,
            "full_validation_results",
        )
        write_iceberg_to_db(
            df=full_validation_results_df,
            database=glue_database,
            table="full_validation_results",
            athena_s3_output=athena_s3_output,
            workgroup=workgroup,
        )
    except Exception as e:
        logger.exception("Failed to write validation results to Parquet/Glue.")
        raise RuntimeError("Writing Parquet/Glue results failed.") from e
