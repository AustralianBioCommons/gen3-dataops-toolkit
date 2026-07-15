import logging
import boto3
import awswrangler as wr
import pandas as pd
import json
import re
import ast
from typing import Optional, Dict, Any, Union
from dataclasses import dataclass
from datetime import datetime, date
import uuid 
import pytz
import base64
import numpy as np
from decimal import Decimal
from gen3_validator.dict import DataDictionary

logger = logging.getLogger(__name__)

@dataclass
class AthenaConfig:
    """
    Configuration class for dbt release info writing.

    Args:
        aws_region (str): AWS region for Athena and S3 operations.
        aws_profile (str): AWS profile to use for authentication.
        athena_s3_output (str): S3 location for Athena query output.
    """
    aws_region: str
    aws_profile: str
    athena_s3_output: str

    def as_dict(self) -> Dict[str, Any]:
        return {
            "aws_region": self.aws_region,
            "aws_profile": self.aws_profile,
            "athena_s3_output": self.athena_s3_output,
        }

# ----------------- Athena Helpers -----------------

class AthenaQuery:
    def __init__(self, athena_config):
        self.config = athena_config

    def _get_boto_session(self):
        """Creates a boto3 session with the configured region and optional profile."""
        region = self.config.aws_region
        profile = getattr(self.config, "aws_profile", None)
        logger.debug(
            f"Creating boto3 session with region: {region}, profile: {profile}"
        )
        if not region:
            logger.error("config region must be set for boto3 session.")
            raise RuntimeError("config region must be set")
        # Only require region; profile is optional
        if profile:
            session = boto3.Session(region_name=region, profile_name=profile)
        else:
            session = boto3.Session(region_name=region)
        logger.debug("boto3 session successfully created.")
        return session
    
    def list_tables(self, database: str) -> list:
        """
        List all table names in the specified Athena database using awswrangler.

        Args:
            database (str): The Athena database to list tables from.

        Returns:
            list: A list of table names (str).
        """
        boto3_session = self._get_boto_session()
        try:
            tables = wr.catalog.get_tables(database=database, boto3_session=boto3_session)
            table_names = [tbl['Name'] for tbl in tables]
            logger.info(f"Found {len(table_names)} tables in database '{database}'.")
            return table_names
        except Exception as e:
            logger.error(f"Error listing tables in Athena database '{database}': {e}", exc_info=True)
            raise

    def query_athena(self, sql: str, athena_database: str, ctas_approach: bool = True) -> pd.DataFrame:
        """Runs an Athena query and returns the results as a pandas DataFrame."""
        logger.info(f"Running Athena query: {sql}")
        boto3_session = self._get_boto_session()
        try:
            df = wr.athena.read_sql_query(
                sql=sql,
                boto3_session=boto3_session,
                database=athena_database,
                ctas_approach=ctas_approach,
                s3_output=self.config.athena_s3_output
            )
            logger.info(
                f"Athena query completed successfully. Returned {len(df)} rows."
            )
        except Exception as e:
            logger.error(f"Error running Athena query: {e}", exc_info=True)
            raise
        return df
    
    def create_release_table(
        self, release_db: str, release_table: str, release_s3_location: str
    ) -> None:
        """
        Create the release tracking table in the given database if it does not already exist.

        The table is created as an Iceberg table with Parquet format and Snappy compression.
        The schema includes release_tag, model_name, db_name, snapshot_id, committed_at, inserted_at, and github_sha.

        The names are parameters (resolved by the caller, e.g. from SSM
        release/db + release/table + buckets/metadata) so this module stays a
        pure, name-free helper library.

        Raises:
            Exception: If table creation fails.
        """
        create_sql = f"""
            CREATE TABLE IF NOT EXISTS {release_db}.{release_table} (
                release_tag        STRING,
                model_name         STRING,
                db_name            STRING,
                snapshot_id        BIGINT,
                committed_at       TIMESTAMP,
                inserted_at        TIMESTAMP,
                github_sha         STRING
            )
            PARTITIONED BY (release_tag)
            LOCATION '{release_s3_location}'
            TBLPROPERTIES (
                'table_type'='ICEBERG',
                'format'='parquet',
                'write_compression'='snappy'
            )
        """
        logger.info(f"Ensuring release table exists: {release_db}.{release_table}")
        try:
            self.query_athena(create_sql, release_db, False)
            logger.info(f"Release table {release_db}.{release_table} created or already exists.")
        except Exception as e:
            logger.error(f"Failed to create release table {release_db}.{release_table}: {e}", exc_info=True)
            raise

    def insert_to_iceberg_table(self, df, table_name, athena_database):
        """Inserts row into table."""
        logger.info(
            f"Inserting DataFrame with {len(df)} rows into iceberg table '{table_name}' "
            f"in database '{athena_database}'."
        )
        boto3_session = self._get_boto_session()
        try:
            logger.info(f"Inserting to icerberg table with s3 output: {self.config.athena_s3_output}")
            temp_s3_path = f"{self.config.athena_s3_output}/temp/{uuid.uuid4()}/"
            wr.athena.to_iceberg(
                df=df,
                database=athena_database,
                table=table_name,
                boto3_session=boto3_session,
                workgroup='primary',
                temp_path=temp_s3_path
            )
            logger.info(
                f"Insert to iceberg table '{table_name}' successful."
            )
        except Exception as e:
            logger.error(
                f"Error inserting to iceberg table '{table_name}': {e}",
                exc_info=True
            )
            raise

    def find_db_for_model(self, model_name: str) -> Optional[str]:
        """
        Search all Athena databases for a table with the given name and return the database name if found.

        This function iterates through all available Athena databases and checks if a table with the specified
        model_name exists in any of them. If found, it returns the name of the database; otherwise, it returns None.

        Args:
            model_name (str): The name of the table/model to search for.
        Returns:
            Optional[str]: The name of the database containing the table, or None if not found.

        Raises:
            Exception: If there is an error fetching databases or tables from Athena.

        Example:
            >>> find_db_for_model("my_table", config)
            'my_database'
            >>> find_db_for_model("nonexistent_table", config)
            None

        Notes:
            - Requires AWS credentials and permissions to list Athena databases and tables.
            - Uses the awswrangler (wr) library.
            - The AWS region is determined from config.aws_region.

        """
        try:
            # Create a session and pass it to wrangler
            boto3_session = self._get_boto_session()
            databases = wr.catalog.databases(boto3_session=boto3_session)
            db_list = databases.get('Database', [])
            for db in db_list:
                try:
                    # Pass the session to other wrangler calls too
                    tables = wr.catalog.get_tables(database=db, boto3_session=boto3_session)
                    table_names = [t.get('Name') for t in tables]
                    if model_name in table_names:
                        return db
                except Exception as table_exc:
                    logger.warning(f"Could not fetch tables for database '{db}': {table_exc}")
                    continue
            return None
        except Exception as e:
            logger.error(f"Error searching for model '{model_name}' in Athena databases: {e}")
            return None


def write_iceberg_to_db(
    df: pd.DataFrame,
    database: str,
    table: str,
    athena_s3_output: str,
    workgroup: str = "primary",
    table_location: str = None,
    partition_cols: list = None,
    merge_cols: list = None,
    schema_evolution: bool = False,
    boto3_session=None,
) -> None:
    """
    Write a DataFrame to an Iceberg table via Athena.

    Parameters
    ----------
    df : pd.DataFrame
        DataFrame to write.
    database : str
        Glue database name.
    table : str
        Glue/Athena Iceberg table name.
    athena_s3_output : str
        S3 URI for Athena query results and temporary staging
        (e.g., 's3://bucket/athena-output/').
    workgroup : str, optional
        Athena workgroup to use. Defaults to "primary".
    table_location : str, optional
        S3 path where the Iceberg table data files are stored.
        Required when creating a new table. If None, the table
        must already exist.
    partition_cols : list, optional
        Columns to partition the Iceberg table by. If None, no
        partitioning is applied.
    merge_cols : list, optional
        Columns to use for MERGE INTO (upsert) semantics. If None,
        rows are appended.
    schema_evolution : bool, optional
        If True, allow schema evolution for new columns. Defaults
        to False.
    boto3_session : boto3.Session, optional
        A boto3 session. If None, the default session is used.
    """
    if table_location is None:
        logger.warning(
            "table_location is None for %s.%s. "
            "This will fail if the Iceberg table does not "
            "already exist. Set 's3_path' in your config "
            "to provide a table location for new tables.",
            database, table,
        )

    try:
        logger.debug(f"Creating Glue database '{database}' if not exists.")
        wr.catalog.create_database(name=database, exist_ok=True)

        temp_path = f"{athena_s3_output.rstrip('/')}/temp/{uuid.uuid4()}/"
        logger.debug(
            f"Writing DataFrame to Iceberg table {database}.{table} "
            f"(workgroup: {workgroup}, temp_path: {temp_path}, "
            f"table_location: {table_location})"
        )
        wr.athena.to_iceberg(
            df=df.astype("string"),
            database=database,
            table=table,
            temp_path=temp_path,
            table_location=table_location,
            partition_cols=partition_cols,
            merge_cols=merge_cols,
            schema_evolution=schema_evolution,
            workgroup=workgroup,
            boto3_session=boto3_session,
        )
        logger.debug(
            f"Successfully wrote to Iceberg table {database}.{table}"
        )
    except Exception as e:
        if "Must specify table location" in str(e):
            logger.error(
                "Iceberg table %s.%s does not exist and no "
                "table_location was provided. Set 's3_path' "
                "in common.metadata_upload in "
                "the caller to specify where the "
                "table data should be stored.",
                database, table,
            )
        logger.error(
            f"Failed to write to Iceberg table "
            f"{database}.{table}: {e}"
        )
        raise RuntimeError(
            f"Failed to write to Iceberg table "
            f"{database}.{table}: {e}"
        )


def convert_dataframe_types_for_json(df: pd.DataFrame) -> pd.DataFrame:
    """
    Converts DataFrame column types to JSON-serialisable formats.
    """
    df_copy = df.copy()
    for col in df_copy.columns:
        # Handle Decimal objects first, converting them to float
        if any(isinstance(x, Decimal) for x in df_copy[col].dropna()):
            df_copy[col] = df_copy[col].apply(
                lambda x: float(x) if isinstance(x, Decimal) else x
            )

        # Handle datetime-like types, converting to ISO strings
        if pd.api.types.is_datetime64_any_dtype(df_copy[col].dtype):
            df_copy[col] = df_copy[col].apply(
                lambda x: x.isoformat() if pd.notna(x) else None
            )
            continue 

        # For float columns (either original or from Decimal conversion)
        if pd.api.types.is_float_dtype(df_copy[col].dtype):
            # Only convert to object type if NaN is present
            if df_copy[col].isna().any():
                df_copy[col] = df_copy[col].astype(object).where(df_copy[col].notna(), None)
            else:
                # Ensures columns of Decimals without Nones become float
                df_copy[col] = df_copy[col].astype(float)

        # Convert numpy integers to standard Python integers
        elif pd.api.types.is_integer_dtype(df_copy[col].dtype):
            df_copy[col] = df_copy[col].astype('Int64')

        # Handle object columns that might contain Timestamps or other special types
        elif pd.api.types.is_object_dtype(df_copy[col].dtype):
            df_copy[col] = df_copy[col].apply(
                lambda x: x.isoformat() if isinstance(x, (pd.Timestamp, datetime))
                else None if pd.isna(x)
                else x
            )

    return df_copy

def replace_nan_with_none(obj):
    """
    Recursively traverses a dictionary or list and replaces float NaN values with None.
    """
    if isinstance(obj, dict):
        return {k: replace_nan_with_none(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [replace_nan_with_none(i) for i in obj]
    # Check if it's a float and is NaN
    elif isinstance(obj, float) and np.isnan(obj):
        return None
    else:
        return obj


def json_serialiser(obj):
    """
    Custom JSON serialiser for handling types that aren't natively JSON-serialisable.

    This improved version handles:
    - Null/NA values (None, np.nan, pd.NaT), which are converted to None (JSON null).
    - Date and time objects (datetime.date, datetime.datetime, pd.Timestamp),
      which are converted to ISO 8601 strings.
    - Decimal objects, which are converted to floats.
    - NumPy numeric types (integer and floating), which are converted to
      standard Python int and float types.
    - Bytes objects, which are Base64-encoded into a string for safe JSON transport.
    - Set and frozenset objects, which are converted to lists.
    """
    # This check is critical and must be first. It correctly handles
    # None, np.nan, and pd.NaT, converting them to None for JSON null.
    if pd.isna(obj):
        return None

    # Handle specific, non-native JSON types
    if isinstance(obj, (datetime, pd.Timestamp, date)):
        return obj.isoformat()
    if isinstance(obj, Decimal):
        return float(obj)
    if isinstance(obj, np.integer):
        return int(obj)
    if isinstance(obj, np.floating):
        # This handles numpy float types, including np.inf.
        # np.nan is already caught by the pd.isna() check above.
        return float(obj)
    if isinstance(obj, bytes):
        # JSON does not support bytes. A common practice is to encode them
        # as a Base64 string, which is safe for JSON.
        return base64.b64encode(obj).decode('utf-8')
    if isinstance(obj, (set, frozenset)):
        # Sets are not JSON serialisable; convert them to a list.
        return list(obj)

    # For any unhandled types, this will raise an error. This is the correct
    # protocol for a default function used with `json.dumps()`.
    raise TypeError(f"Type {type(obj)} not JSON serialisable")




class AthenaValidationWriter:
    """
    AthenaWriter encapsulates common operations for extracting tabular data from an Amazon Athena table,
    transforming it as needed, and preparing a JSON serialization suitable for downstream processing or archiving to S3.

    Responsibilities:
        - Retrieve the most recent study_id and snapshot_id from the specified Athena table.
        - Extract the entire contents of a table.
        - Clean and format specific columns, such as decoding any stringified dictionary values for submitter_id links.
        - Construct clean, consistent JSON output.

    Args:
        athena_config (AthenaConfig): Configuration for Athena session, including region, workgroup, and output S3 location.
        db_name (str): Name of the Athena database.
        table_name (str): Name of the Athena table.
    """
    def __init__(self, athena_config, db_name, table_name):
        """
        Initializes an AthenaWriter instance.

        Args:
            athena_config (AthenaConfig): AthenaConfig instance with AWS and Athena parameters.
            db_name (str): Database name in Athena.
            table_name (str): Table name in Athena.
        """
        self.athena_config = athena_config
        self.db_name = db_name
        self.table_name = table_name
        self.study_id = None
        self.snapshot_id = None
        logger.info(f"AthenaWriter initialized with db: {db_name}, table: {table_name}")


    def _get_latest_snapshot_id(self, return_commit_datetime: bool = False) -> str:
        """
        Retrieves the latest snapshot_id from the Iceberg/Athena table's $snapshots metadata.

        Returns:
            str: The most recent snapshot ID based on the newest committed_at timestamp.

        Notes:
            The method stores the result as self.snapshot_id for convenient reuse.
            If no results are present, this will raise an IndexError.
        """
        # Query using CAST to avoid bringing in unsupported timestamp(3) with time zone types,
        # instead cast everything to string (safe for Athena <-> pandas & avoids Hive errors)
        query = f"""
            SELECT
                CAST(snapshot_id AS VARCHAR) AS snapshot_id,
                CAST(committed_at AS VARCHAR) AS committed_at
            FROM "{self.db_name}"."{self.table_name}$snapshots"
            ORDER BY committed_at DESC
            LIMIT 1
        """
        athena_query = AthenaQuery(self.athena_config)
        result = athena_query.query_athena(sql=query, athena_database=self.db_name, ctas_approach=False)

        if result.empty:
            logger.warning(
                f"No snapshot rows found for {self.db_name}.{self.table_name}"
            )
            self.snapshot_id = None
            if return_commit_datetime:
                return None, None
            return None

        snapshot_id = result['snapshot_id'].iloc[0]
        self.snapshot_id = snapshot_id
        logger.info(f"Retrieved snapshot_id: {snapshot_id}")

        if return_commit_datetime:
            commit_datetime = result['committed_at'].iloc[0]
            return snapshot_id, commit_datetime if commit_datetime is not None else None
        return snapshot_id

    def _get_full_table(self):
        """
        Retrieves the complete contents of the Athena table, optionally at a specific snapshot.

        If self.snapshot_id is set, queries the table at that snapshot id.
        Otherwise, retrieves the latest/current version.

        Returns:
            list[dict]: Each row as a dictionary where keys are column names.

        Notes:
            This fetches all rows, which may have memory implications for large tables.
        """
        table_ref = f'"{self.db_name}"."{self.table_name}"'
        if self.snapshot_id is not None:
            table_ref = f'{table_ref} FOR VERSION AS OF {self.snapshot_id}'
            logger.info(f"Querying table at snapshot_id: {self.snapshot_id}")
        else:
            logger.info("Querying table at latest/current version (no snapshot_id).")

        query = f"""
            SELECT *
            FROM {table_ref}
        """
        athena_query = AthenaQuery(self.athena_config)
        result = athena_query.query_athena(sql=query, athena_database=self.db_name, ctas_approach=False)
        logger.info(f"Retrieved {len(result)} rows from the table.")
        return result

    def _format_submitter_id_value(self, submitter_id_value: str) -> Union[dict, list, str]:
        """
        Attempts to parse a string as a dictionary or list of dictionaries,
        handling both JSON format and Python dict string representations.
        
        This handles cases where Athena returns:
        - Single dict as JSON: '{"submitter_id": "value"}'
        - Single dict as Python: "{'submitter_id': 'value'}"
        - List of dicts as JSON: '[{"submitter_id": "val1"}, {"submitter_id": "val2"}]'
        
        Args:
            submitter_id_value: The value from the raw Athena row.
        
        Returns:
            dict, list, or original value: Parsed dict/list if successfully parsed,
            else the input value unchanged.
        """
        if not isinstance(submitter_id_value, str):
            return submitter_id_value
        
        # Try JSON parsing first (handles both dicts and lists with escaped quotes)
        try:
            parsed = json.loads(submitter_id_value)
            # Validate that result is dict or list of dicts
            if isinstance(parsed, dict):
                return parsed
            elif isinstance(parsed, list):
                return parsed
        except (json.JSONDecodeError, ValueError):
            pass
        
        # Fallback to ast.literal_eval for Python-style dicts/lists
        dict_or_list_pattern = re.compile(r'^\s*[\[{]\s*["\']?submitter_id["\']?')
        if dict_or_list_pattern.match(submitter_id_value):
            try:
                parsed = ast.literal_eval(submitter_id_value)
                if isinstance(parsed, (dict, list)):
                    return parsed
            except (ValueError, SyntaxError):
                logger.warning(f"Could not parse string-like dictionary/list: {submitter_id_value}")
                pass
        
        return submitter_id_value



    def construct_json(self):
        """
        Constructs a JSON string from the Athena table data, adding and normalizing
        certain metadata fields (including consistent snapshot_id and study_id), and
        parsing stringified dict values for proper downstream JSON compatibility.

        Returns:
            str: Formatted JSON string with all records, ready for saving to file or S3.

        Steps:
            1. Fetch latest study_id and snapshot_id for the table.
            2. Retrieve all table rows.
            3. For each row, update/add 'snapshot_id' and 'study_id', and parse any
               stringified dict fields (e.g., submitter_id link columns).
            4. Serialize rows as a pretty JSON string.
        """
        logger.info("Getting latest snapshot_id")
        self._get_latest_snapshot_id()
        
        logger.info("Constructing JSON data from Athena table...")
        full_table = self._get_full_table()

        if hasattr(full_table, "to_dict"):
            full_table_json = full_table.to_dict(orient="records")
        else:
            logger.error("Expected a pandas DataFrame from _get_full_table(), but got type: %s", type(full_table))
            raise ValueError("Unable to convert table to JSON, not a DataFrame.")
        
        # Convert data types to JSON-serialisable formats
        logger.info("Converting DataFrame types for JSON serialisation...")
        full_table = convert_dataframe_types_for_json(full_table)

        full_table_json = full_table.to_dict(orient="records")

        for obj in full_table_json:
            try:
                # Convert string submitter_id link dicts to valid dict
                for key, value in list(obj.items()):
                    obj[key] = self._format_submitter_id_value(value)
            except Exception as e:
                logger.warning(f"Error processing row for JSON output: {e}")

        try:
            full_table_json = replace_nan_with_none(full_table_json)
            json_data = json.dumps(full_table_json, indent=4, default=json_serialiser)
            logger.info("JSON data construction complete.")
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialise JSON: {e}")
            raise
        
        return json_data


class AthenaGoldWriter(AthenaValidationWriter):
    def __init__(self, athena_config, db_name, table_name):
        super().__init__(athena_config, db_name, table_name)
        self.study_id = None
        self.snapshot_id = None
        self.json_data = None
        logger.info(f"AthenaGoldWriter initialized with db: {db_name}, table: {table_name}")
    
    def construct_json(self) -> str:
        """
        This 'construct_json' is specialized for the AthenaGoldWriter.

        For Gold tables, the expected behavior is:
          - Retrieve the full gold table (via _get_full_table())
          - Attempt to parse stringified dict fields just as in the parent, but may need table-specific logic
          - Serialize as a pretty JSON string
        """
        logger.info("Getting latest snapshot_id")
        self._get_latest_snapshot_id()
        
        logger.info("Constructing JSON data from Athena GOLD table...")
        full_table = self._get_full_table()

        if hasattr(full_table, "to_dict"):
            full_table_json = full_table.to_dict(orient="records")
        else:
            logger.error("Expected a pandas DataFrame from _get_full_table(), but got type: %s", type(full_table))
            raise ValueError("Unable to convert table to JSON, not a DataFrame.")

        # Convert data types to JSON-serialisable formats
        logger.info("Converting DataFrame types for JSON serialisation (Gold)...")
        full_table = convert_dataframe_types_for_json(full_table)
        full_table_json = full_table.to_dict(orient="records")

        for obj in full_table_json:
            try:
                # Convert string submitter_id link dicts to valid dict, if present
                for key, value in list(obj.items()):
                    obj[key] = self._format_submitter_id_value(value)
            except Exception as e:
                logger.warning(f"Error processing GOLD row for JSON output: {e}")

        try:
            full_table_json = replace_nan_with_none(full_table_json)
            json_data = json.dumps(full_table_json, indent=4, default=json_serialiser)
            logger.info("GOLD JSON data construction complete.")
            self.json_data = json_data
        except (TypeError, ValueError) as e:
            logger.error(f"Failed to serialise GOLD JSON: {e}")
            raise

        return json_data


def generate_validation_id():
    """
    Generates a unique validation identifier string, based on the current date and time in Australian Eastern Time.

    Returns:
        str: A string formatted as "YYYYMMDDHHMMSS", e.g., "20240531173010".
    """
    # Get Australian Eastern timezone (automatically handles AEST/AEDT)
    australian_tz = pytz.timezone('Australia/Melbourne')
    
    # Get current time in Australian Eastern timezone
    current_date_time = datetime.now(australian_tz).strftime("%Y%m%d%H%M%S")
    validation_id = f"{current_date_time}"
    logger.info(f"Generated validation_id (Australian Eastern Time): {validation_id}")
    return validation_id

def write_validation_json_to_s3(s3_bucket,
                study_id,
                validation_id,
                table_name,
                snapshot_id,
                json_data):
    """
    Writes the supplied JSON data to an S3 bucket using the specified logical path and metadata.

    Args:
        s3_bucket (str): Name of the S3 bucket.
        study_id (str): Study identifier to include in the S3 key/path.
        validation_id (str): Unique validation run identifier string.
        table_name (str): Source Athena table name.
        snapshot_id (str): Table snapshot ID (for versioning).
        json_data (str): The JSON-serialized string to be uploaded.

    The S3 object key is formatted as:
        validation/study_id=<study_id>/validation_id=<validation_id>/table_name=<table_name>/snapshot_id=<snapshot_id>/<table_name>.json

    Side Effects:
        Uploads (puts) the JSON data to the given S3 bucket.

    Raises:
        Any exception raised by boto3.client('s3').put_object.

    Logging:
        Logs both intent and completion of S3 upload.
    """
    s3 = boto3.client('s3')
    s3_object_key = f"validation/study_id={study_id}/validation_id={validation_id}/table_name={table_name}/snapshot_id={snapshot_id}/{table_name}.json"
    logger.info(f"Writing JSON data to S3 bucket: {s3_bucket}, object key: {s3_object_key}")
    s3.put_object(Body=json_data, Bucket=s3_bucket, Key=s3_object_key)
    logger.info(f"Object created at s3://{s3_bucket}/{s3_object_key}")



def write_gold_json_to_s3(
    s3_bucket,
    study_id,
    table_name,
    snapshot_id,
    json_data,
):
    """
    Write JSON data for a "Gold" Athena table to S3 in a unique and logical path.

    This function uploads the supplied JSON string to the given S3 bucket, placing it under a "validation"
    directory structure specific to "gold" Athena tables. The S3 object key removes the "gold_" prefix from
    the table name for the actual file name and omits any validation ID in the path.

    Args:
        s3_bucket (str): S3 bucket name to upload to.
        study_id (str): The unique identifier for the study; appears in the S3 path.
        table_name (str): Athena gold table name (e.g., "gold_diagnosis"); used in path and for filename.
        snapshot_id (str): Identifier for the data snapshot/version used in the path.
        json_data (str): JSON string (already serialized), to upload.

    S3 Path Pattern:
        validation/study_id=<study_id>/table_name=<table_name>/snapshot_id=<snapshot_id>/<stripped_table_name>.json

        - <stripped_table_name> is the table_name with the "gold_" prefix removed.

    Example:
        gold_table_name = "gold_diagnosis"
        table_name = gold_table_name
        Actual file will be e.g.:
            validation/study_id=foo/table_name=gold_diagnosis/snapshot_id=bar/diagnosis.json

    Side Effects:
        Uploads JSON to S3 using boto3.

    Raises:
        Any exception from boto3's client('s3').put_object.

    Logging:
        Logs before and after the S3 put operation, including path details for traceability.
    """
    s3 = boto3.client('s3')

    filename = table_name
    if filename.startswith("gold_"):
        filename = filename.replace("gold_", "")

    # Remove study_id from filename if present
    if study_id in filename:
        filename = filename.replace(f"{study_id}_", "")
    else:
        logger.warning(f"Filename {filename} does not contain study_id {study_id}, writing filename as {filename}")

    s3_object_key = f"gold_jsons/study_id={study_id}/table_name={table_name}/snapshot_id={snapshot_id}/{filename}.json"
    logger.info(f"Writing JSON data to S3 bucket: {s3_bucket}, object key: {s3_object_key}")
    s3.put_object(Body=json_data, Bucket=s3_bucket, Key=s3_object_key)
    logger.info(f"Object created at s3://{s3_bucket}/{s3_object_key}")


def construct_data_import_order(s3_uri) -> list:
    from g3dt.validate.validate import load_schema_from_s3_uri
    schema_dict = load_schema_from_s3_uri(s3_uri)
    dd = DataDictionary(schema_dict)
    dd.schema = schema_dict
    dd.calculate_node_order()
    return dd.node_order

def write_release_jsons_to_s3(s3_bucket, release_id, study_id, table_name, json_data):
    """
    Write a JSON string to a specific S3 location for a given release and study.

    Args:
        s3_bucket (str): The S3 bucket where the file will be uploaded.
        release_id (str): Release identifier used in the S3 key path.
        study_id (str): Study identifier used in the S3 key path.
        table_name (str): Table name (used for naming the .json file).
        json_data (str): JSON data (as a string) to be uploaded.

    Returns:
        str: The output directory path in S3 where the file was written.

    Raises:
        Exception: Any exception raised by boto3.client('s3').put_object.

    Example:
        >>> output_dir = write_release_jsons_to_s3('my-bucket', 'release123', 'study1', 'gold_foo', '{"x":1}')
    """
    s3 = boto3.client('s3')

    filename = table_name
    study_name = study_id.split("/")[-1]
    if filename.startswith(f"{study_name}_"):
        filename = filename[len(f"{study_name}_"):]
    else:
        logger.warning(f"Filename {filename} does not start with study_name {study_name}, writing filename as {filename}")

    output_dir = f"release_jsons/{release_id}/{study_id}"
    s3_object_key = f"{output_dir}/{filename}.json"
    logger.info(f"Writing JSON data to S3 bucket: {s3_bucket}, object key: {s3_object_key}")
    s3.put_object(Body=json_data, Bucket=s3_bucket, Key=s3_object_key)
    logger.info(f"Object created at s3://{s3_bucket}/{s3_object_key}")
    return output_dir