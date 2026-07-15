import awswrangler as wr
import pandas as pd
import boto3
from g3dt.utils.athena_utils import write_iceberg_to_db
import urllib.parse
import os
import uuid
import hashlib
from datetime import datetime
from botocore.exceptions import ClientError
import logging
import pytz  # Replaced tzlocal with pytz
import s3fs
from typing import Dict


logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)

s3 = boto3.client("s3")

# ---------- helpers ----------
def parse_s3(uri: str):
    p = urllib.parse.urlparse(uri)
    return p.netloc, p.path.lstrip("/")

def get_tags(uri: str) -> dict:
    b, k = parse_s3(uri)
    try:
        ts = s3.get_object_tagging(Bucket=b, Key=k)["TagSet"]
        logger.debug(f"Retrieved tags for {uri}: {ts}")
        return {t["Key"]: t["Value"] for t in ts}
    except ClientError as e:
        if e.response["Error"]["Code"] == "NoSuchKey":
            logger.warning(f"NoSuchKey when getting tags for {uri}")
            return {}
        logger.error(f"ClientError getting tags for {uri}: {e}")
        raise
    except Exception as e:
        logger.error(f"Failed to get tags for S3 object {uri}: {e}")
        raise RuntimeError(f"Failed to get tags for S3 object {uri}: {e}")

def get_head_meta(uri: str) -> dict:
    b, k = parse_s3(uri)
    try:
        h = s3.head_object(Bucket=b, Key=k)
        logger.debug(f"Head metadata for {uri}: {h}")
    except ClientError as e:
        logger.error(f"Failed to get head metadata for S3 object {uri}: {e}")
        raise RuntimeError(f"Failed to get head metadata for S3 object {uri}: {e}")
    except Exception as e:
        logger.error(f"Unexpected error getting head metadata for S3 object {uri}: {e}")
        raise RuntimeError(f"Unexpected error getting head metadata for S3 object {uri}: {e}")
    
    # Consistently use Australia/Melbourne timezone
    aest_tz = pytz.timezone("Australia/Melbourne")
    last_modified_aest = h["LastModified"].astimezone(aest_tz)
    
    return {
        "ingest_file_etag": (h.get("ETag") or "").strip('"'),
        "ingest_file_size_bytes": str(h.get("ContentLength")),
        "ingest_file_last_modified": last_modified_aest.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }

def normalise(name: str) -> str:
    n = (name or "col").strip().lower().replace("/", "_").replace(" ", "_")
    allowed = "abcdefghijklmnopqrstuvwxyz0123456789_"
    n = "".join(ch if ch in allowed else "_" for ch in n)
    while "__" in n: n = n.replace("__", "_")
    if not n: n = "col"
    if n[0].isdigit(): n = f"c_{n}"
    return n

def read_csv_robust(uri: str) -> pd.DataFrame:
    # delimiter sniff + encoding fallback, keep everything as string
    last_err = None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            logger.debug(f"Attempting to read CSV from {uri} with encoding {enc}")
            df = wr.s3.read_csv(
                path=uri,
                sep=None, engine="python",
                dtype=str,
                keep_default_na=False,
                encoding=enc,
                quoting=0,  # QUOTE_MINIMAL
            )
            logger.debug(f"Successfully read CSV from {uri} with encoding {enc}")
            break
        except Exception as e:
            logger.debug(f"Failed to read CSV from {uri} with encoding {enc}: {e}")
            last_err = e
    else:
        logger.error(f"Failed to read CSV from {uri} after trying all encodings: {last_err}")
        raise RuntimeError(f"Failed to read CSV from {uri}: {last_err}")
    # normalise & uniquify headers
    cols = [normalise(c) for c in df.columns]
    seen, out = set(), []
    for c in cols:
        base, i, cand = c, 1, c
        while cand in seen:
            i += 1
            cand = f"{base}_{i}"
        seen.add(cand); out.append(cand)
    df.columns = out
    logger.debug(f"Normalised columns for {uri}: {out}")
    return df.reset_index(drop=True)

def read_json_robust(uri: str) -> pd.DataFrame:
    # encoding fallback, keep everything as string
    last_err = None
    for enc in ("utf-8-sig", "cp1252"):
        try:
            logger.debug(f"Attempting to read JSON from {uri} with encoding {enc}")
            # Remove keep_default_na, which is not a valid argument for wr.s3.read_json
            df = wr.s3.read_json(
                path=uri,
                orient="records",
                dtype=str,
                encoding=enc,
            )
            logger.debug(f"Successfully read JSON from {uri} with encoding {enc}")
            break
        except Exception as e:
            logger.debug(f"Failed to read JSON from {uri} with encoding {enc}: {e}")
            last_err = e
    else:
        logger.error(f"Failed to read JSON from {uri} after trying all encodings: {last_err}")
        raise RuntimeError(f"Failed to read JSON from {uri}: {last_err}")
    return df

def read_xlsx_robust(s3_uri: str) -> dict[str, pd.DataFrame]:
    """
    Reads an XLSX file from the given S3 URI using pandas with the openpyxl engine.
    Reads all values as strings and disables default NA values.
    Returns a dictionary of dataframes, with the sheet name as the key.
    If there is only one sheet, the key is set to the file name (minus extension) from s3_uri.

    Args:
        s3_uri (str): The S3 URI of the XLSX file.

    Returns:
        dict[str, pd.DataFrame]: Dictionary mapping sheet name (or file name) to DataFrame.

    Raises:
        RuntimeError: If reading the XLSX file fails,
        ValueError: If the S3 URI is invalid.
    """
    # Check that s3_uri is a valid S3 URI
    if not isinstance(s3_uri, str) or not s3_uri.startswith("s3://"):
        logger.error(f"Invalid S3 URI: {s3_uri}")
        raise ValueError(f"Invalid S3 URI: {s3_uri}")
    try:
        logger.debug(f"Attempting to read XLSX from {s3_uri}")
        # Read all sheets, always returns dict
        df_dict = pd.read_excel(
            s3_uri,
            sheet_name=None,
            engine="openpyxl",
            dtype=str,
            keep_default_na=False
        )
        logger.debug(f"Successfully read XLSX from {s3_uri}: sheets={list(df_dict.keys())}")

        if len(df_dict) == 1:
            # Only one sheet, rename the key to the file name (no extension)
            import os
            file_name = os.path.splitext(os.path.basename(s3_uri))[0]
            only_df = next(iter(df_dict.values()))
            logger.debug(f"Only one sheet found, renaming key to file name: {file_name}")
            return {file_name: only_df}

        return df_dict
    except Exception as e:
        logger.error(f"Failed to read XLSX from {s3_uri}: {e}")
        raise RuntimeError(f"Failed to read XLSX from {s3_uri}: {e}")


def flatten_xlsx_dict(df_dict: dict[str, pd.DataFrame]) -> pd.DataFrame:
    """
    Flattens a dictionary of DataFrames (representing Excel sheets) into a single DataFrame.

    Adds a "sheet_name" column to each DataFrame, indicating the originating sheet,
    then concatenates all DataFrames into one.

    Args:
        df_dict (dict[str, pd.DataFrame]): Dictionary mapping sheet names to DataFrames.

    Returns:
        pd.DataFrame: Concatenated DataFrame with an added "sheet_name" column.

    Raises:
        ValueError: If the provided dictionary is empty.
        RuntimeError: If an error occurs during DataFrame concatenation.
    """
    if not df_dict:
        logger.error("The df_dict provided to flatten_xlsx_dict is empty.")
        raise ValueError("Input dictionary of DataFrames is empty.")

    dfs_with_sheet = []
    try:
        for sheet_name, df in df_dict.items():
            df_with_sheet = df.copy()
            df_with_sheet["sheet_name"] = sheet_name
            dfs_with_sheet.append(df_with_sheet)
        return pd.concat(dfs_with_sheet, ignore_index=True)
    except Exception as e:
        logger.error(f"Failed to flatten XLSX dictionary: {e}")
        raise RuntimeError(f"Failed to flatten XLSX dictionary: {e}") from e


def get_format(uri: str) -> str:
    """
    Returns the file format (extension) of the given URI, without the leading dot.
    """
    _, ext = os.path.splitext(uri)
    fmt = ext[1:].lower()
    logger.debug(f"File format for {uri} is {fmt}")
    return fmt

def compute_row_hash(row: pd.Series) -> str:
    parts = [f"{k}={row[k] if row[k] is not None else ''}" for k in sorted(row.index)]
    return hashlib.sha256(("||".join(parts)).encode("utf-8")).hexdigest()

def sanitize_submission_date(value: str) -> str:
    """
    Parse a submission_date string and return it in 'YYYY-MM-DD' format.
    Handles various formats including 'YYYY_MM_DD', 'DD MM YYYY', 'DD-MM-YYYY',
    and 'YYYY-MM-DD'. It expects the input to be in one of these formats.
    
    Raises a ValueError if the input is empty or cannot be parsed as a date.
    """
    if not value:
        logger.error("submission_date tag is empty")
        raise ValueError("submission_date tag is empty")

    # Standardize separators by replacing underscores and spaces with hyphens
    cleaned_value = value.replace('_', '-').replace(' ', '-')

    # Attempt to parse the date in 'YYYY-MM-DD' format
    try:
        return datetime.strptime(cleaned_value, "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        # If the first format fails, try the 'DD-MM-YYYY' format
        try:
            return datetime.strptime(cleaned_value, "%d-%m-%Y").strftime("%Y-%m-%d")
        except ValueError:
            logger.error(f"Unable to parse submission_date format to ISO: {value!r}")
            raise ValueError(f"Unable to parse submission_date format to ISO: {value!r}")

def prepare_ingest_metadata(
    df: pd.DataFrame,
    uri: str,
    tags: dict,
    head_meta: dict,
    study_id: str,
    ingest_run_id: str,
    ingest_received_at: str,
    ingest_timezone: str,
    ingest_submission_id: str,
) -> pd.DataFrame:
    """
    Attach ingest and tag metadata columns to a DataFrame for a single file.

    Parameters
    ----------
    df : pd.DataFrame
        The DataFrame to annotate.
    uri : str
        S3 URI of the file.
    tags : dict
        S3 object tags.
    head_meta : dict
        S3 object head metadata.
    study_id : str
        Study identifier.
    ingest_run_id : str
        Unique ID for this ingest run.
    ingest_received_at : str
        ISO timestamp for when ingest was received.
    ingest_timezone : str
        Timezone string for ingest.
    ingest_submission_id : str
        Submission ID for this ingest.

    Returns
    -------
    pd.DataFrame
        Annotated DataFrame.
    """
    file_name = os.path.basename(urllib.parse.urlparse(uri).path)
    df = df.copy()
    try:
        logger.debug(f"Annotating DataFrame for file {uri} with ingest metadata.")
        df["study_id"] = str(study_id)
        df["submission_date"] = sanitize_submission_date(tags["submission_date"])
        df["ingest_run_id"] = ingest_run_id
        df["ingest_received_at"] = ingest_received_at
        df["ingest_timezone"] = ingest_timezone
        df["ingest_original_file_path"] = uri
        df["ingest_file_name"] = file_name
        df["ingest_submission_id"] = str(ingest_submission_id or "")
        df["ingest_file_etag"] = head_meta["ingest_file_etag"]
        df["ingest_file_size_bytes"] = head_meta["ingest_file_size_bytes"]
        df["ingest_file_last_modified"] = head_meta["ingest_file_last_modified"]
    except Exception as e:
        logger.error(f"Failed to prepare ingest metadata for file {uri}: {e}")
        raise RuntimeError(f"Failed to prepare ingest metadata for file {uri}: {e}")

    # Add S3 tags as columns (namespaced)
    for k, v in tags.items():
        try:
            df[f"tag_{normalise(k)}"] = str(v)
        except Exception as e:
            logger.error(f"Failed to add tag '{k}' as column for file {uri}: {e}")
            raise RuntimeError(f"Failed to add tag '{k}' as column for file {uri}: {e}")

    # Compute row-hash over raw columns (exclude ingest_*, tag_*, study_id, submission_date)
    try:
        exclude = {c for c in df.columns if c.startswith("ingest_") or c.startswith("tag_")}
        exclude |= {"study_id", "submission_date"}
        raw_cols = [c for c in df.columns if c not in exclude]
        logger.debug(f"Computing row hash for file {uri} on columns: {raw_cols}")
        df["ingest_row_hash"] = df[raw_cols].apply(compute_row_hash, axis=1)
    except Exception as e:
        logger.error(f"Failed to compute row hash for file {uri}: {e}")
        raise RuntimeError(f"Failed to compute row hash for file {uri}: {e}")
    return df

def align_and_combine_frames(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """
    Align columns across multiple DataFrames and concatenate them.

    Parameters
    ----------
    frames : list of pd.DataFrame
        List of DataFrames to align and combine.

    Returns
    -------
    pd.DataFrame
        Combined DataFrame with aligned columns.
    """
    try:
        all_cols = sorted({c for f in frames for c in f.columns})
        logger.debug(f"Aligning and combining {len(frames)} DataFrames with columns: {all_cols}")
        aligned_frames = [f.reindex(columns=all_cols, fill_value="") for f in frames]
        combined = pd.concat(aligned_frames, ignore_index=True)
        logger.debug(f"Combined DataFrame shape: {combined.shape}")
        return combined
    except Exception as e:
        logger.error(f"Failed to align and combine DataFrames: {e}")
        raise RuntimeError(f"Failed to align and combine DataFrames: {e}")

def get_ingest_true_files(s3_uri: str, exclude_directories: list[str] = None) -> list[str]:
    """
    Get a list of files from an S3 URI, excluding directories.

    Parameters
    ----------
    s3_uri : str
        S3 URI to list files from.
    exclude_directories : list[str], optional
        List of directories to exclude. Defaults to None.

    Returns
    -------
    list[str]
        List of file paths.
    """
    logger.debug(f"Getting files from {s3_uri}")
    try:
        file_paths = wr.s3.list_objects(path=s3_uri)
    except Exception as e:
        logger.error(f"Failed to list objects in S3 path {s3_uri}: {e}")
        raise RuntimeError(f"Failed to list objects in S3 path {s3_uri}: {e}")
    logger.debug(f"Found {len(file_paths)} files in {s3_uri}")
    if exclude_directories:
        file_paths = [f for f in file_paths if not any(f.startswith(d) for d in exclude_directories)]
        logger.debug(f"Found {len(file_paths)} files after excluding directories: {exclude_directories}")
    else:
        logger.debug(f"No directories excluded from {s3_uri}")
    
    ingest_files = []
    for path in file_paths:
        try:
            tags = get_tags(path)
        except Exception as e:
            logger.warning(f"Could not get tags for {path}: {e}")
            continue
        if not tags or "ingest" not in tags or tags["ingest"] != "true":
            logger.debug(f"Skipping {path}: missing or non-true 'ingest' tag")
            continue
        ingest_files.append(path)
    logger.debug(f"Found {len(ingest_files)} files with 'ingest' tag set to 'true'")
    return ingest_files

def ingest_table_to_parquet_dataset(
    s3_uri: str,
    database: str,
    table_prefix: str,
    athena_s3_output: str,
    workgroup: str = "primary",
    ingest_timezone: str = "Australia/Melbourne",
    ingest_submission_id: str = None,
    ingest_received_at: str = None,
    ingest_run_id: str = None,
) -> dict:
    """
    Ingest a single CSV or JSON file from S3, annotate with metadata, and write to an Iceberg table.
    The file is written to a Glue table named "{table_prefix}_{node}", where node is taken from the S3 tags.

    Steps:
      - Reads the file and its S3 tags/head metadata.
      - Annotates with ingest metadata and S3 tags.
      - Computes a row hash for deduplication/auditing.
      - Writes to an Iceberg table in Glue/Athena under table "{table_prefix}_{node}".

    Parameters
    ----------
    s3_uri : str
        S3 URI to a CSV or JSON file.
    database : str
        Glue database name.
    table_prefix : str
        Glue table prefix.
    athena_s3_output : str
        S3 URI for Athena query results and temporary staging.
    workgroup : str, optional
        Athena workgroup to use. Defaults to "primary".
    ingest_timezone : str, optional
        Timezone for ingest metadata. Defaults to "Australia/Melbourne".
    ingest_submission_id : str, optional
        Submission ID for this ingest. Defaults to None.
    ingest_received_at : str, optional
        Timestamp for when the ingest was received. If None, will use current UTC time.
    ingest_run_id : str, optional
        Unique ID for this ingest run. If None, will generate a new one.

    Returns
    -------
    dict
        Summary of the ingest run, including run ID, file count, and tables/partitions written.
    """
    if ingest_received_at is None:
        aest_tz = pytz.timezone("Australia/Melbourne")
        ingest_received_at = datetime.now(aest_tz).strftime("%Y-%m-%dT%H:%M:%S%z")
    if ingest_run_id is None:
        ingest_run_id = uuid.uuid4().hex[:16]
        
    results = []
    tables_written = set()
    partitions_written = {}

    logger.info(f"Starting ingest run {ingest_run_id} for file: {s3_uri}.")
    uri = s3_uri
    logger.debug(f"Processing file: {uri}")
    try:
        tags = get_tags(uri)
        logger.debug(f"Tags for {uri}: {tags}")
    except Exception as e:
        logger.error(f"Failed to get tags for file {uri}: {e}")
        raise RuntimeError(f"Failed to get tags for file {uri}: {e}")

    if "submission_date" not in tags:
        logger.error(f"{uri} is missing required S3 object tag 'submission_date'")
        raise ValueError(f"{uri} is missing required S3 object tag 'submission_date'")
    if "node" not in tags:
        logger.error(f"{uri} is missing required S3 object tag 'node'")
        raise ValueError(f"{uri} is missing required S3 object tag 'node'")

    study_id = tags["study_id"]
    node = tags["node"]
    table_name = node
    if table_prefix:
        table_name = f"{table_prefix}_{node}"
    tables_written.add(table_name)

    file_format = get_format(uri)
    logger.debug(f"File format for {uri}: {file_format}")
    try:
        if file_format == "json":
            df = read_json_robust(uri)
        elif file_format == "csv":
            df = read_csv_robust(uri)
        elif file_format == "xlsx":
            df_dict = read_xlsx_robust(uri)
            df = flatten_xlsx_dict(df_dict)
        else:
            logger.error(f"Unsupported file format: {file_format} for file {uri}")
            raise ValueError(f"Unsupported file format: {file_format}")
        logger.debug(f"Read {len(df)} rows from {uri} ({file_format})")
    except Exception as e:
        logger.error(f"Failed to read file {uri} (format: {file_format}): {e}")
        raise RuntimeError(f"Failed to read file {uri} (format: {file_format}): {e}")

    try:
        head_meta = get_head_meta(uri)
        logger.debug(f"Head meta for {uri}: {head_meta}")
    except Exception as e:
        logger.error(f"Failed to get head metadata for file {uri}: {e}")
        raise RuntimeError(f"Failed to get head metadata for file {uri}: {e}")

    try:
        annotated_df = prepare_ingest_metadata(
            df=df,
            uri=uri,
            tags=tags,
            head_meta=head_meta,
            study_id=study_id,
            ingest_run_id=ingest_run_id,
            ingest_received_at=ingest_received_at,
            ingest_timezone=ingest_timezone,
            ingest_submission_id=ingest_submission_id,
        )
        logger.debug(f"Annotated DataFrame for {uri} with {len(annotated_df)} rows")
    except Exception as e:
        logger.error(f"Failed to prepare ingest metadata for file {uri}: {e}")
        raise RuntimeError(f"Failed to prepare ingest metadata for file {uri}: {e}")

    # Write this file's DataFrame to its Iceberg table
    try:
        write_iceberg_to_db(
            df=annotated_df,
            database=database,
            table=table_name,
            athena_s3_output=athena_s3_output,
            workgroup=workgroup,
        )
        logger.debug(f"Successfully wrote {len(annotated_df)} rows to Iceberg table {database}.{table_name}")
    except Exception as e:
        logger.error(f"Failed to write DataFrame to Iceberg table {table_name}: {e}")
        raise RuntimeError(f"Failed to write DataFrame to Iceberg table {table_name}: {e}")

    # Track partitions written for this table
    try:
        partitions = sorted({
            (r["study_id"], r["submission_date"])
            for _, r in annotated_df[["study_id", "submission_date"]]
            .drop_duplicates().iterrows()
        })
        partitions_written.setdefault(table_name, []).extend(partitions)
        logger.debug(f"Partitions written for {table_name}: {partitions}")
    except Exception as e:
        logger.error(f"Failed to extract partitions from DataFrame for table {table_name}: {e}")
        raise RuntimeError(f"Failed to extract partitions from DataFrame for table {table_name}: {e}")

    results.append({
        "uri": uri,
        "table": table_name,
        "rows_written": len(annotated_df),
        "partitions": partitions,
    })

    logger.info(
        f"Ingest run {ingest_run_id} complete. "
        f"Files processed: 1. Tables: {sorted(tables_written)}"
    )

    return {
        "ingest_run_id": ingest_run_id,
        "ingest_submission_id": ingest_submission_id,
        "ingest_received_at": ingest_received_at,
        "files_processed": 1,
        "tables_written": sorted(tables_written),
        "partitions_written": partitions_written,
        "results": results
    }

def ingest_files_to_parquet_dataset(
    s3_uris: list,
    database: str,
    table_prefix: str,
    athena_s3_output: str,
    workgroup: str = "primary",
    ingest_submission_id: str = None,
    exclude_fn: list = ['program.json', 'project.json'],
):
    """
    Ingest multiple files from S3, annotate with metadata, and write to Iceberg tables in Glue/Athena.
    Each time this function is called, a single ingestion ID is created which will be attached to all the files
    in the list of s3_uris.

    Parameters
    ----------
    s3_uris : list
        List of S3 URIs to CSV or JSON files to ingest.
    database : str
        Glue database name.
    table_prefix : str
        Prefix for Glue table names.
    athena_s3_output : str
        S3 URI for Athena query results and temporary staging.
    workgroup : str, optional
        Athena workgroup to use. Defaults to "primary".
    ingest_submission_id : str, optional
        Submission ID for ingest metadata. Defaults to None.
    exclude_fn : list, optional
        List of file names from the s3_uris to exclude from ingestion.
        Example: ['program.json', 'project.json'] or ['randomDatafile.csv']
    """
    
    ingest_run_id = str(uuid.uuid4())
    
    # Generate timestamp in Australia/Melbourne timezone
    aest_tz = pytz.timezone("Australia/Melbourne")
    ingest_received_at = datetime.now(aest_tz).strftime("%Y-%m-%dT%H:%M:%S%z")
    
    # Hardcode the timezone for metadata
    ingest_timezone = "Australia/Melbourne"

    results = []
    for uri in s3_uris:
        if any(uri.endswith(exclude) for exclude in exclude_fn):
            continue
        resp = ingest_table_to_parquet_dataset(
            s3_uri=uri,
            database=database,
            table_prefix=table_prefix,
            athena_s3_output=athena_s3_output,
            workgroup=workgroup,
            ingest_timezone=ingest_timezone,
            ingest_submission_id=ingest_submission_id,
            ingest_run_id=ingest_run_id,
            ingest_received_at=ingest_received_at
        )
        results.append(resp)

    return results
