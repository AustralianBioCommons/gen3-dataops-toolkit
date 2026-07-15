import os
import sys
import time
import logging
from gen3.submission import Gen3Submission
from typing import Optional, List
import pandas as pd
from g3dt.utils.athena_utils import AthenaQuery, AthenaConfig

logger = logging.getLogger(__name__)


def query_metadata_upload_guids(
    database: str,
    table: str,
    project_id: str,
    api_endpoint: str,
    version: str,
    athena_s3_output: str,
    workgroup: str = "primary",
    aws_region: str = "ap-southeast-2",
    aws_profile: Optional[str] = None,
    node: Optional[str] = None,
) -> pd.DataFrame:
    """
    Queries the Athena metadata upload table for records matching a given
    project_id, version, and api_endpoint. Optionally filters by node.

    Args:
        database (str): The Athena database name.
        table (str): The Athena table name.
        project_id (str): The compound project ID (e.g. "program1-CDAH").
        api_endpoint (str): The Gen3 API endpoint URL.
        version (str): The metadata version to filter on (e.g. "0.8.1").
        athena_s3_output (str): S3 URI for Athena query results.
        workgroup (str, optional): Athena workgroup. Default is "primary".
        aws_region (str, optional): AWS region. Default is "ap-southeast-2".
        aws_profile (str, optional): AWS profile name.
        node (str, optional): Node name to filter on (e.g. "subject").

    Returns:
        pd.DataFrame: DataFrame of matching records including gen3_guid.
    """
    athena_config = AthenaConfig(
        aws_region=aws_region,
        aws_profile=aws_profile,
        athena_s3_output=athena_s3_output,
    )
    athena_query = AthenaQuery(athena_config)

    sql = (
        f'SELECT * FROM "{database}"."{table}" '
        f"WHERE project_id = '{project_id}' "
        f"AND version = '{version}' "
        f"AND api_endpoint = '{api_endpoint}'"
    )
    if node:
        sql += f" AND node = '{node}'"

    logger.info(
        "Querying Athena for metadata upload records: "
        "project_id=%s, version=%s, api_endpoint=%s, node=%s",
        project_id,
        version,
        api_endpoint,
        node or "ALL",
    )
    df = athena_query.query_athena(
        sql=sql,
        athena_database=database,
        ctas_approach=False,
    )
    logger.info("Query returned %s records.", len(df))
    return df


def delete_records_by_guid(
    gen3_submission: Gen3Submission,
    program_id: str,
    project_id: str,
    uuids: List[str],
    batch_size: int = 40,
    batch_delay: float = 0.5,
    verbose: bool = False,
):
    """
    Deletes Gen3 records one at a time using the SDK's
    delete_record method. UUIDs are grouped into batches
    for rate-limiting only (a pause between each batch).

    Errors are caught and logged per-UUID so that one
    failure does not stop the rest of the deletions.

    Args:
        gen3_submission (Gen3Submission): An authenticated
            Gen3Submission instance.
        program_id (str): The Gen3 program name.
        project_id (str): The Gen3 project name.
        uuids (list[str]): List of gen3_guid UUIDs to delete.
        batch_size (int, optional): Number of UUIDs to process
            before pausing. Default is 40.
        batch_delay (float, optional): Seconds to pause between
            batches. Default is 0.5.
        verbose (bool, optional): If True, log the full API
            response JSON for each request. Default is False.
    """
    if not uuids:
        logger.info("No UUIDs provided for deletion. Skipping.")
        return

    batches = [
        uuids[i: i + batch_size]
        for i in range(0, len(uuids), batch_size)
    ]
    total_batches = len(batches)
    total = len(uuids)

    logger.info(
        "Deleting %s records in %s batches "
        "(batch_size=%s)...",
        total, total_batches, batch_size,
    )

    success_count = 0
    failed_ids = []

    for idx, batch in enumerate(batches, start=1):
        batch_success = 0
        for uuid in batch:
            try:
                if verbose:
                    resp = gen3_submission.delete_record(
                        program_id, project_id, uuid,
                    )
                    logger.debug(
                        "%s | response: %s",
                        uuid, resp,
                    )
                else:
                    with open(os.devnull, "w") as devnull:
                        old_stdout = sys.stdout
                        sys.stdout = devnull
                        try:
                            gen3_submission.delete_record(
                                program_id, project_id,
                                uuid,
                            )
                        finally:
                            sys.stdout = old_stdout
                batch_success += 1
            except Exception as e:
                failed_ids.append(uuid)
                try:
                    body = e.response.json()
                    code = body.get("code", "?")
                    if verbose:
                        msg = body
                    else:
                        ents = body.get("entities", [])
                        errs = ents[0].get("errors", [])
                        msg = errs[0].get(
                            "message", str(e),
                        )
                except Exception:
                    code = "?"
                    msg = str(e)
                logger.warning(
                    "\033[91m[FAIL]\033[0m %s | %s | %s",
                    uuid, code, msg,
                )

        success_count += batch_success
        if batch_success == len(batch):
            logger.info(
                "\033[92m[Batch %d/%d]\033[0m "
                "Deleted %d/%d",
                idx, total_batches,
                batch_success, len(batch),
            )
        else:
            logger.info(
                "[Batch %d/%d] Deleted %d/%d",
                idx, total_batches,
                batch_success, len(batch),
            )

        if idx < total_batches:
            time.sleep(batch_delay)

    if failed_ids:
        logger.info(
            "Deletion complete. "
            "\033[92mSuccessful: %s\033[0m, "
            "\033[91mFailed: %s\033[0m",
            success_count, len(failed_ids),
        )
    else:
        logger.info(
            "\033[92mDeletion complete. "
            "Successful: %s, Failed: 0\033[0m",
            success_count,
        )


def delete_project_metadata(
    gen3_submission: Gen3Submission,
    program_id: str,
    project_id: str,
    nodes: List[str],
    prompt_for_confirmation: bool = True,
):
    """
    Deletes all metadata for a project by iterating through nodes
    and calling Gen3's delete_nodes API.

    Nodes should be provided in deletion order (reverse of import
    order), with any excluded nodes already filtered out.

    Args:
        gen3_submission (Gen3Submission): An authenticated
            Gen3Submission instance.
        program_id (str): The Gen3 program name.
        project_id (str): The Gen3 project name.
        nodes (list[str]): Ordered list of node names to delete.
        prompt_for_confirmation (bool): Whether to prompt for
            confirmation before deletion.

    Returns:
        None
    """
    if not nodes:
        logger.info("No nodes provided for deletion. Skipping.")
        return

    if prompt_for_confirmation:
        confirm = input(
            "Do you want to delete the metadata? (yes/no): "
        ).strip().lower()
        if confirm != "yes":
            logger.info("Deletion cancelled by user.")
            return

    total_nodes = len(nodes)
    for idx, node in enumerate(nodes, start=1):
        logger.info(
            "\033[94m[Node %d/%d]\033[0m | "
            "Project: %-10s | Node: %-25s | Deleting...",
            idx, total_nodes, project_id, node,
        )
        try:
            gen3_submission.delete_nodes(
                program_id, project_id, [node]
            )
            logger.info(
                "\033[92m[SUCCESS]\033[0m | "
                "Project: %-10s | Node: %-25s",
                project_id, node,
            )
        except Exception as e:
            logger.error(
                "\033[91m[FAILED]\033[0m  | "
                "Project: %-10s | Node: %-25s | "
                "Error: %s",
                project_id, node, e,
            )
