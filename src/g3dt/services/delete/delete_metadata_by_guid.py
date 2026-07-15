import sys
import time
import logging
import argparse
import yaml
from g3dt.upload.metadata_submitter import (
    create_boto3_session,
    get_gen3_api_key_aws_secret,
    infer_api_endpoint_from_jwt,
    create_gen3_submission_class,
)
from g3dt.upload.metadata_deleter import (
    query_metadata_upload_guids,
    delete_records_by_guid,
)

# ANSI colour codes (matching metadata_submitter.py style)
GREEN = "\033[92m"
RED = "\033[91m"
YELLOW = "\033[93m"
BLUE = "\033[94m"
RESET = "\033[0m"

EXCLUDE_NODES = [
    "program",
    "project",
    "acknowledgement",
    "publication",
]

# Exit code that signals "study exists but has no data at this version —
# skipped". A bulk caller (services/delete/delete_metadata.sh) treats this as a
# skip-and-continue rather than a failure. Only emitted with --skip-if-empty.
SKIP_EXIT_CODE = 3


def setup_logger():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


# Shared config resolution (SSM-backed) — see src/g3dt/config.py
from g3dt import config as g3dt_config, resolver  # noqa: E402


def load_import_order(import_order_path, exclude_nodes=None):
    """
    Reads the DataImportOrder.txt file and returns the node list
    in deletion order (reversed, with excluded nodes removed).
    """
    if exclude_nodes is None:
        exclude_nodes = EXCLUDE_NODES
    with open(import_order_path, 'r', encoding='utf-8') as f:
        nodes = [line.strip() for line in f if line.strip()]
    nodes = [n for n in nodes if n not in exclude_nodes]
    nodes.reverse()
    return nodes


def main():
    logger = setup_logger()

    parser = argparse.ArgumentParser(
        description=(
            "Delete Gen3 metadata records by GUID. Queries the Athena "
            "metadata_upload table for matching records per node "
            "(in reverse DataImportOrder) and deletes them from Gen3."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--study",
        required=True,
        help=(
            "Study key (bare or env-suffixed) "
            "(e.g. ausdiab, caughtcad, edcad, cdah)"
        ),
    )
    parser.add_argument(
        "--env",
        required=True,
        help="Environment to use (selects AWS secret, profile, etc.)",
    )
    parser.add_argument(
        "--version",
        required=True,
        help="Metadata version to filter on (e.g. 0.8.1)",
    )
    parser.add_argument(
        "--import-order",
        default="DataImportOrder.txt",
        help="Path to DataImportOrder.txt",
    )
    parser.add_argument(
        "--node",
        default=None,
        help=(
            "Delete only a specific node (e.g. 'subject'). "
            "If omitted, all nodes are processed in reverse "
            "DataImportOrder."
        ),
    )
    parser.add_argument(
        "--prompt",
        action="store_true",
        default=False,
        help="Prompt for confirmation before deleting.",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=40,
        help="Number of UUIDs per DELETE request.",
    )
    parser.add_argument(
        "--batch-delay",
        type=float,
        default=0.5,
        help="Seconds to pause between batches.",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="Log full API response JSON for each request.",
    )
    parser.add_argument(
        "--delay",
        type=float,
        default=1.0,
        help="Seconds to wait between nodes.",
    )
    parser.add_argument(
        "--skip-if-empty",
        action="store_true",
        default=False,
        help=(
            "If the study has no data at the given version, exit with the skip "
            "code (3) instead of 0. Used by the bulk delete loop to "
            "skip-and-continue rather than treat it as a failure."
        ),
    )

    args = parser.parse_args()

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Env facts + resource names from SSM; the study registry from the marker
    # or s3://<metadata-bucket>/config/studies.yaml.
    try:
        env_cfg = g3dt_config.resolve_env(args.env)
    except g3dt_config.ConfigError as exc:
        logger.error(str(exc))
        sys.exit(1)
    try:
        study_cfg = g3dt_config.resolve_study(args.study, args.env)
    except g3dt_config.ConfigError as exc:
        if args.skip_if_empty:
            logger.warning(
                "Study '%s' not found in configuration — skipping.", args.study
            )
            sys.exit(SKIP_EXIT_CODE)
        logger.error(str(exc))
        sys.exit(1)

    project_id = study_cfg.project_id
    program_id = study_cfg.program_id

    aws_secret_name = env_cfg.aws_secret_name
    aws_profile = env_cfg.aws_profile
    aws_region = env_cfg.region

    rc = resolver.resolve(
        g3dt_config.require_project(),
        g3dt_config.env_base(args.env),
        profile=aws_profile,
    )
    # Upload-tracking table: conventional name in the env's metadata DB
    # (exactly like the CDK's `releases` table).
    database = rc.metadata_db
    table = g3dt_config.METADATA_UPLOAD_TABLE
    athena_s3_output = rc.athena_output_location
    workgroup = rc.athena_workgroup

    # Construct compound project_id for Athena query
    compound_project_id = f"{program_id}-{project_id}"

    logger.info(
        "Study: %s | Env: %s | Project: %s | Version: %s",
        args.study,
        args.env,
        compound_project_id,
        args.version,
    )

    # AWS and Gen3 authentication
    session = create_boto3_session(aws_profile=aws_profile)
    api_key = get_gen3_api_key_aws_secret(
        secret_name=aws_secret_name,
        region_name=aws_region,
        session=session,
    )

    # Derive api_endpoint from JWT
    api_endpoint = infer_api_endpoint_from_jwt(api_key['api_key'])
    logger.info("Derived API endpoint: %s", api_endpoint)

    # Create Gen3Submission instance
    sub = create_gen3_submission_class(api_key)

    # Determine node list
    if args.node:
        nodes_to_delete = [args.node]
        logger.info(
            "%s[SINGLE NODE]%s Targeting node: %s",
            BLUE, RESET, args.node,
        )
    else:
        nodes_to_delete = load_import_order(args.import_order)
        logger.info(
            "Loaded %s nodes from %s (deletion order, "
            "excluding %s)",
            len(nodes_to_delete),
            args.import_order,
            EXCLUDE_NODES,
        )

    if args.prompt:
        confirm = input(
            f"Proceed with deletion for project "
            f"{compound_project_id}, version {args.version}, "
            f"{len(nodes_to_delete)} node(s)? (yes/no): "
        ).strip().lower()
        if confirm != "yes":
            logger.info("Deletion cancelled by user.")
            return

    # Process each node
    total_deleted = 0
    total_skipped = 0
    total_nodes = len(nodes_to_delete)

    for idx, node in enumerate(nodes_to_delete, start=1):
        logger.info(
            "%s[Node %d/%d]%s | Project: %-10s | "
            "Node: %-25s | Querying...",
            BLUE, idx, total_nodes, RESET,
            compound_project_id, node,
        )

        df = query_metadata_upload_guids(
            database=database,
            table=table,
            project_id=compound_project_id,
            api_endpoint=api_endpoint,
            version=args.version,
            athena_s3_output=athena_s3_output,
            workgroup=workgroup,
            aws_region=aws_region,
            aws_profile=aws_profile,
            node=node,
        )

        if df.empty:
            logger.info(
                "%s[SKIP]%s    | Project: %-10s | "
                "Node: %-25s | No records found",
                YELLOW, RESET,
                compound_project_id, node,
            )
            total_skipped += 1
            continue

        uuids = df['gen3_guid'].dropna().unique().tolist()
        logger.info(
            "%s[DELETE]%s  | Project: %-10s | "
            "Node: %-25s | Records: %s",
            BLUE, RESET,
            compound_project_id, node, len(uuids),
        )

        delete_records_by_guid(
            gen3_submission=sub,
            program_id=program_id,
            project_id=project_id,
            uuids=uuids,
            batch_size=args.batch_size,
            batch_delay=args.batch_delay,
            verbose=args.verbose,
        )

        logger.info(
            "%s[SUCCESS]%s | Project: %-10s | "
            "Node: %-25s | Deleted: %s",
            GREEN, RESET,
            compound_project_id, node, len(uuids),
        )
        total_deleted += len(uuids)

        if idx < total_nodes:
            time.sleep(args.delay)

    logger.info(
        "=========================================="
    )
    logger.info(
        "Deletion complete. Total deleted: %s | "
        "Nodes skipped: %s | Nodes processed: %s",
        total_deleted,
        total_skipped,
        total_nodes - total_skipped,
    )

    # No records matched the requested version across any node. This usually
    # means the version was never uploaded (or the data was uploaded without a
    # version), so surface an actionable hint rather than a silent "0 deleted".
    if total_deleted == 0:
        logger.warning(
            "Data version '%s' not found for study '%s'. Ensure each data node "
            "has a `data_version` property for versioning to work.",
            args.version,
            args.study,
        )
        if args.skip_if_empty:
            sys.exit(SKIP_EXIT_CODE)


if __name__ == "__main__":
    main()
