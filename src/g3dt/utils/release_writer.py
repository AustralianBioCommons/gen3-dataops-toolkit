import argparse
from g3dt.utils.athena_utils import AthenaConfig, AthenaQuery, AthenaValidationWriter
from g3dt.utils.dbt_utils import get_model_names
import logging
from typing import Optional

# Setup logger for this script
logger = logging.getLogger("DBTRelease")
logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s"
)

def safe_sql_string(value: Optional[str]) -> str:
    """
    Safely escapes and prepares a string value for inclusion in SQL statements.

    - If the input is None or an empty string, it returns the string 'NULL'.
    - Otherwise, it escapes any single quotes by doubling them and encloses
      the entire string in single quotes.

    Args:
        value (Optional[str]): The string value to escape.

    Returns:
        str: The escaped and quoted string ready for SQL insertion.
    """
    if value is None or value == "":
        return "NULL"
    return "'" + str(value).replace("'", "''") + "'"

def insert_release_row(
    athena_config: AthenaConfig,
    model_name: str,
    db_name: Optional[str],
    snapshot_id: Optional[int],
    committed_at: Optional[str],
    release_db: str,
    release_table: str,
    release_tag: str,
    github_sha: str
) -> None:
    """
    Insert a new row into the release tracking table for a given model and snapshot.

    This function checks if a row with the same release_tag, model_name, and db_name already exists
    in the release table to prevent duplicate entries. If no such row exists, it inserts a new row
    with the provided snapshot_id, committed_at timestamp, and other metadata.
    """
    athena_query = AthenaQuery(athena_config)
    if not db_name:
        logger.warning(f"Skipping model '{model_name}': No database found for this model.")
        return

    logger.debug(
        f"Checking for existing release row: release_tag={release_tag!r}, model_name={model_name!r}, db_name={db_name!r}"
    )
    check_sql = f"""
        SELECT COUNT(*) AS cnt
        FROM "{release_db}"."{release_table}"
        WHERE release_tag = {safe_sql_string(release_tag)}
          AND model_name  = {safe_sql_string(model_name)}
          AND db_name     = {safe_sql_string(db_name)}
    """
    try:
        cnt_df = athena_query.query_athena(check_sql, release_db)
        cnt = cnt_df.iloc[0]['cnt'] if not cnt_df.empty else 0
    except Exception as e:
        logger.error(
            f"Could not query existing release rows for release_tag='{release_tag}', model='{model_name}', db='{db_name}': {e}",
            exc_info=True
        )
        raise

    if cnt > 0:
        logger.info(f"[SKIP] Release row already exists: {release_tag} / {db_name}.{model_name}")
        return

    snap_val = "NULL" if snapshot_id is None else str(snapshot_id)
    commit_val = f"TIMESTAMP '{committed_at}'" if committed_at else "NULL"
    sha_val = safe_sql_string(github_sha)

    insert_sql = f"""
        INSERT INTO "{release_db}"."{release_table}"
            (release_tag, db_name, model_name, snapshot_id, committed_at, inserted_at, github_sha)
        VALUES (
            {safe_sql_string(release_tag)},
            {safe_sql_string(db_name)},
            {safe_sql_string(model_name)},
            {snap_val},
            {commit_val},
            CURRENT_TIMESTAMP,
            {sha_val}
        )
    """
    logger.info(
        f"Inserting new release row for {db_name}.{model_name} [release_tag={release_tag}, snapshot_id={snapshot_id}, committed_at={committed_at}]"
    )
    try:
        athena_query.query_athena(insert_sql, release_db, ctas_approach=False)
        logger.info(f"[OK] Inserted release row for {db_name}.{model_name}.{release_tag}")
    except Exception as e:
        logger.error(
            f"Failed to insert release row for {db_name}.{model_name}.{release_tag}: {e}",
            exc_info=True
        )
        raise

def parse_args():
    parser = argparse.ArgumentParser(
        description="Write dbt model snapshot info for all models as a release row to Athena. "
                    "Ensures all tracked dbt models have a row in the release iceberg table with latest snapshot/commit.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter
    )
    parser.add_argument("--dbt-schema-path", type=str, required=True,
                        help="Path to dbt schema file (usually schema.yml) containing list of models to track.")
    parser.add_argument("--release-db", type=str, required=True,
                        help="Athena database for the release tracking table.")
    parser.add_argument("--release-table", type=str, required=True,
                        help="Athena table for the release tracking table.")
    parser.add_argument("--data-release-version", type=str, required=True,
                        help="The release version tag to record. (e.g., v1.2.3)")
    parser.add_argument("--commit-id", type=str, required=True,
                        help="The git commit SHA for this release (for auditing).")
    parser.add_argument("--aws-region", type=str, required=True,
                        help="AWS Region for Athena/S3.")
    parser.add_argument("--aws-profile", type=str, required=False,
                        help="AWS CLI profile to use for authentication.")
    parser.add_argument("--athena-s3-output", type=str, required=True,
                        help="S3 URI for Athena query results (e.g., 's3://athena-results-bucket/').")
    parser.add_argument("--release-s3-location", type=str, required=True,
                        help="S3 location for the release table (e.g., 's3://<metadata-bucket>/').")
    parser.add_argument("-v", "--verbose", action="store_true", default=False,
                        help="Enable debug logging.")
    return parser.parse_args()

def main():
    args = parse_args()
    log_level = logging.DEBUG if args.verbose else logging.INFO
    logging.getLogger().setLevel(log_level)

    logger.info("==== DBT release snapshot info writer started ====")
    logger.debug(f"Parsed arguments: {args}")

    athena_config = AthenaConfig(
        aws_region=args.aws_region,
        aws_profile=args.aws_profile,
        athena_s3_output=args.athena_s3_output
    )

    dbt_models = get_model_names(args.dbt_schema_path)
    athena_query = AthenaQuery(athena_config)
    
    logger.info(f"Creating release table: {args.release_db}.{args.release_table}")
    athena_query.create_release_table(
        args.release_db, args.release_table, args.release_s3_location
    )

    logger.info(f"Processing DBT models from schema: {dbt_models}")

    for model_name in dbt_models:
        logger.info(f"--- Processing model: {model_name}")
        db_name = athena_query.find_db_for_model(model_name)
        if not db_name:
            logger.warning(f"Database not found for model '{model_name}'. Skipping...")
            continue

        snapshot_writer = AthenaValidationWriter(athena_config, db_name, model_name)
        snapshot_id, commit_datetime = snapshot_writer._get_latest_snapshot_id(return_commit_datetime=True)
        logger.debug(f"Latest snapshot for {db_name}.{model_name}: {snapshot_id} @ {commit_datetime}")

        insert_release_row(
            athena_config=athena_config,
            model_name=model_name,
            db_name=db_name,
            snapshot_id=snapshot_id,
            committed_at=commit_datetime,
            release_db=args.release_db,
            release_table=args.release_table,
            release_tag=args.data_release_version,
            github_sha=args.commit_id
        )
        logger.info(f"[SUCCESS] Release info recorded for {db_name}.{model_name}")

    logger.info(f"Finished release process for DBT models: {dbt_models}")

if __name__ == "__main__":
    main()