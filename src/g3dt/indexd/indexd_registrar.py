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

def fetch_registered_files(
    database: str,
    table: str,
    study_id: str,
    indexd_endpoint: str,
    athena_s3_output: str,
    workgroup: str = "primary",
    boto3_session: Optional[boto3.Session] = None,
) -> pd.DataFrame:
    """Return the ``file_name``/``md5`` pairs already registered for a study.

    Scoped to one study and one indexd endpoint so a staging registration can
    never mask a missing prod one (and vice versa).

    A missing table means nothing has ever been registered for this
    environment, which is a normal first-run state, not an error — an empty
    frame is returned so the caller registers everything.

    Returns
    -------
    pd.DataFrame
        Columns ``file_name`` and ``md5``; empty if the table does not exist.
    """
    sql = f"""
        SELECT DISTINCT file_name, md5
        FROM "{database}"."{table}"
        WHERE study_id = '{study_id}'
          AND indexd_endpoint = '{indexd_endpoint}'
    """
    try:
        return wr.athena.read_sql_query(
            sql,
            database=database,
            ctas_approach=False,
            workgroup=workgroup,
            s3_output=athena_s3_output,
            boto3_session=boto3_session,
        )
    except Exception as exc:
        logger.warning(
            "Could not read existing registrations from %s.%s (%s). "
            "Treating every scanned file as unregistered.",
            database, table, exc,
        )
        return pd.DataFrame(columns=["file_name", "md5"])


def filter_unregistered(
    df: pd.DataFrame,
    registered: pd.DataFrame,
) -> pd.DataFrame:
    """Drop scanned files already registered with the same name and md5.

    Re-submitting a file to indexd does not overwrite it: because ``baseid``
    is derived from the filename, indexd creates a *new revision with a new
    did* every time. Those revisions accumulate in the registry table (which
    merges on ``did``), so an unfiltered re-run doubles the corpus and forces
    every downstream join to de-duplicate. Skipping files whose content has
    not changed makes a re-run a cheap no-op.

    An md5 that differs from the registered one means the file genuinely
    changed, so it is *not* skipped — that is exactly when a new revision is
    wanted.

    Parameters
    ----------
    df : pd.DataFrame
        Scanned files; must have ``file_name`` and ``md5``.
    registered : pd.DataFrame
        Existing registrations (see :func:`fetch_registered_files`).

    Returns
    -------
    pd.DataFrame
        The subset of *df* still needing registration.
    """
    if df.empty or registered.empty:
        return df

    already = set(
        zip(registered["file_name"].astype(str), registered["md5"].astype(str))
    )
    keep = [
        (str(name), str(md5)) not in already
        for name, md5 in zip(df["file_name"], df["md5"])
    ]
    return df[pd.Series(keep, index=df.index)]


def register_files_with_indexd(
    index: Gen3Index,
    df: pd.DataFrame,
    authz: List[str],
) -> pd.DataFrame:
    """Register files with Gen3 indexd and return results.

    For each row in *df*, calls ``Gen3Index.create_record`` with the file's
    hashes, size, S3 URL, baseid, and authz. Registers exactly what it is
    given: re-submitting a baseid creates a NEW revision with a new ``did``
    (indexd never overwrites), so callers must pre-filter already-registered
    files with :func:`fetch_registered_files` + :func:`filter_unregistered`
    unless duplicate revisions are intended.

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
