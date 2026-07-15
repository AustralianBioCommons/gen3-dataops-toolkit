import logging
import uuid
import urllib.parse
from datetime import datetime
from typing import List, Dict, Any, Optional

import boto3
import awswrangler as wr
import pandas as pd
import pytz
from gen3.index import Gen3Index

from g3dt.utils.athena_utils import write_iceberg_to_db

logger = logging.getLogger(__name__)


# ---------- baseid helpers ----------

def filename_to_baseid(filename: str) -> str:
    """Generate a deterministic UUIDv5 baseid from a filename.

    Uses uuid.NAMESPACE_DNS as the namespace so the same filename
    always produces the same baseid, enabling idempotent indexd
    registration and version tracking.

    Parameters
    ----------
    filename : str
        The basename of the file (e.g. ``"data.csv"``).

    Returns
    -------
    str
        A UUID string derived from the filename.
    """
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, filename))


# ---------- S3 scanning ----------

def _parse_s3(uri: str):
    """Parse an S3 URI into (bucket, key)."""
    p = urllib.parse.urlparse(uri)
    return p.netloc, p.path.lstrip("/")


def scan_s3_files(
    s3_paths: List[str],
    boto3_session: Optional[boto3.Session] = None,
) -> pd.DataFrame:
    """Recursively list files under S3 prefixes and collect metadata.

    For each object found, retrieves ``ETag`` (md5), ``ContentLength``
    (file_size), and computes a deterministic ``baseid`` from the filename.

    Parameters
    ----------
    s3_paths : list[str]
        One or more S3 URI prefixes to scan (e.g. ``["s3://bucket/data/"]``).
    boto3_session : boto3.Session, optional
        A boto3 session. Uses the default session when *None*.

    Returns
    -------
    pd.DataFrame
        Columns: ``file_name``, ``md5``, ``file_size``, ``s3_url``, ``baseid``.
    """
    session = boto3_session or boto3.Session()
    s3_client = session.client("s3")

    records: List[Dict[str, Any]] = []

    for prefix in s3_paths:
        logger.info("Scanning S3 path: %s", prefix)
        file_uris = wr.s3.list_objects(
            prefix, boto3_session=session
        )

        total = len(file_uris)
        for i, uri in enumerate(file_uris, 1):
            bucket, key = _parse_s3(uri)
            file_name = key.rsplit("/", 1)[-1] if "/" in key else key
            logger.info(
                "[S3 scan %d/%d] %s", i, total, file_name
            )

            try:
                head = s3_client.head_object(Bucket=bucket, Key=key)
            except Exception as e:
                logger.error("Failed to head object %s: %s", uri, e)
                continue

            md5 = (head.get("ETag") or "").strip('"')
            file_size = head.get("ContentLength", 0)

            records.append(
                {
                    "file_name": file_name,
                    "md5": md5,
                    "file_size": file_size,
                    "s3_url": uri,
                    "baseid": filename_to_baseid(file_name),
                }
            )

    if not records:
        return pd.DataFrame(
            columns=["file_name", "md5", "file_size",
                     "s3_url", "baseid"]
        )

    return pd.DataFrame(records)


# ---------- indexd registration ----------

def register_files_with_indexd(
    index: Gen3Index,
    df: pd.DataFrame,
    authz: List[str],
) -> pd.DataFrame:
    """Register files with Gen3 indexd and return results.

    For each row in *df*, calls ``Gen3Index.create_record`` with the
    file's hashes, size, S3 URL, baseid, and authz.  Re-submitting
    the same baseid will create a new revision in indexd, making this
    safe to re-run (idempotent at the API level).

    Parameters
    ----------
    index : Gen3Index
        An authenticated Gen3Index client.
    df : pd.DataFrame
        Must contain columns: ``file_name``, ``md5``, ``file_size``,
        ``s3_url``, ``baseid``.
    authz : list[str]
        Authz paths for the files (e.g.
        ``["/programs/program1/projects/EDCAD-PMS"]``).

    Returns
    -------
    pd.DataFrame
        A copy of *df* with additional columns: ``did``, ``rev``,
        ``registered_at``.  Rows that failed are excluded.
    """
    results: List[Dict[str, Any]] = []
    tz = pytz.timezone("Australia/Melbourne")

    total = len(df)
    for i, (_, row) in enumerate(df.iterrows(), 1):
        baseid = row["baseid"]
        file_name = row["file_name"]
        logger.info(
            "[indexd upload %d/%d] %s", i, total, file_name
        )

        try:
            resp = index.create_record(
                hashes={"md5": row["md5"]},
                size=int(row["file_size"]),
                urls=[row["s3_url"]],
                urls_metadata={row["s3_url"]: {}},
                file_name=file_name,
                baseid=baseid,
                authz=authz,
            )
            logger.info(
                "Registered %s → did=%s baseid=%s",
                file_name, resp.get("did"), resp.get("baseid"),
            )
            results.append(
                {
                    **row.to_dict(),
                    "did": resp["did"],
                    "rev": resp["rev"],
                    "registered_at": datetime.now(tz).isoformat(),
                }
            )
        except Exception as e:
            logger.error("Failed to register %s: %s", file_name, e)

    if not results:
        cols = list(df.columns) + ["did", "rev", "registered_at"]
        return pd.DataFrame(columns=cols)

    return pd.DataFrame(results)


# ---------- Glue / Iceberg persistence ----------

def write_to_glue(
    df: pd.DataFrame,
    database: str,
    table: str,
    athena_s3_output: str,
    table_location: str,
    workgroup: str = "primary",
    partition_cols: Optional[List[str]] = None,
    merge_cols: Optional[List[str]] = None,
    schema_evolution: bool = False,
    boto3_session: Optional[boto3.Session] = None,
) -> None:
    """Write a DataFrame to a Glue Iceberg table.

    Thin wrapper around :func:`write_iceberg_to_db` that provides a
    consistent interface for the indexd module.

    Parameters
    ----------
    df : pd.DataFrame
        Data to write.
    database : str
        Glue database name.
    table : str
        Iceberg table name.
    athena_s3_output : str
        S3 URI for Athena query results.
    table_location : str
        S3 path for Iceberg table data files.
    workgroup : str
        Athena workgroup. Defaults to ``"primary"``.
    partition_cols : list[str], optional
        Columns to partition the Iceberg table by.
    merge_cols : list[str], optional
        Columns to use for MERGE INTO (upsert) semantics.
    schema_evolution : bool
        If True, allow schema evolution for new columns.
    boto3_session : boto3.Session, optional
        A boto3 session.
    """
    write_iceberg_to_db(
        df=df,
        database=database,
        table=table,
        athena_s3_output=athena_s3_output,
        workgroup=workgroup,
        table_location=table_location,
        partition_cols=partition_cols,
        merge_cols=merge_cols,
        schema_evolution=schema_evolution,
        boto3_session=boto3_session,
    )
    logger.info("Wrote %d rows to %s.%s", len(df), database, table)
