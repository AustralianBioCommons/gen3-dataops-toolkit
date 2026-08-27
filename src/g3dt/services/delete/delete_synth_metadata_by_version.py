"""Registry-free, version-filtered deletion for synthetic metadata.

Synthetic uploads write no Athena receipts (upload_synth_metadata_sheepdog
submits with upload_to_database=False), so delete_metadata_by_guid.py's
receipt lookup can never find them. This worker instead filters records
server-side: per node (reverse DataImportOrder), it queries sheepdog GraphQL
for records whose `data_version` property equals --version and deletes them.
The --study value is used directly as the Gen3 project code — no SSM study
registry involvement.

A node whose schema does not declare `data_version` makes the GraphQL query
error; that node is warned about and skipped. If nothing is deleted anywhere,
the run exits with the skip code (3, with --skip-if-empty) plus a hint, so the
bulk loop counts it as skipped rather than failed.
"""
import sys
import time
import logging
import argparse

from gen3.submission import Gen3SubmissionQueryError

from g3dt.upload.metadata_submitter import (
    create_boto3_session,
    get_gen3_api_key_aws_secret,
    create_gen3_submission_class,
)
from g3dt.upload.metadata_deleter import delete_node_records_by_property
from g3dt.import_order import (
    ImportOrderError,
    resolve_import_order,
    to_deletion_order,
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

# Exit code that signals "project exists but has no data at this version —
# skipped". The bulk caller (services/delete/delete_metadata.sh) treats this as
# a skip-and-continue rather than a failure. Only emitted with --skip-if-empty.
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
from g3dt import config as g3dt_config  # noqa: E402


def main():
    logger = setup_logger()

    parser = argparse.ArgumentParser(
        description=(
            "Delete synthetic Gen3 metadata records at one data version. "
            "Filters each node (in reverse DataImportOrder) via sheepdog "
            "GraphQL on the records' data_version property and deletes the "
            "matches. --study is the Gen3 project code itself — no study "
            "registry lookup."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--study",
        required=True,
        help="Gen3 project code (e.g. synthetic_dataset_1)",
    )
    parser.add_argument(
        "--env",
        required=True,
        help="Environment to use (selects AWS secret, profile, etc.)",
    )
    parser.add_argument(
        "--version",
        required=True,
        help=(
            "Value the records' data_version property must equal, matched "
            "verbatim (e.g. v1.3.0)"
        ),
    )
    parser.add_argument(
        "--program-id",
        default="program1",
        help="Gen3 program the project lives under.",
    )
    parser.add_argument(
        "--import-order",
        default=None,
        help=(
            "Path or s3:// URI of DataImportOrder.txt. Default: auto "
            "(./DataImportOrder.txt, then derived from the dictionary — "
            "synthetic projects have no release bucket)."
        ),
    )
    parser.add_argument(
        "--dict-version",
        default=None,
        help=(
            "Dictionary git tag to derive the node order from (verbatim, "
            "e.g. v1.3.0). Default: the env's deployed dictionary. Only "
            "used when the order is derived."
        ),
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
            "If no records match the given version, exit with the skip "
            "code (3) instead of 0. Used by the bulk delete loop to "
            "skip-and-continue rather than treat it as a failure."
        ),
    )

    args = parser.parse_args()

    if args.import_order and args.dict_version:
        parser.error(
            "--import-order names the exact file; --dict-version derives one "
            "— pass only one."
        )

    if args.verbose:
        logger.setLevel(logging.DEBUG)

    # Env facts from SSM. Deliberately NO resolve_study: synthetic projects
    # are not in the study registry — the --study value IS the project code.
    try:
        env_cfg = g3dt_config.resolve_env(args.env)
    except g3dt_config.ConfigError as exc:
        logger.error(str(exc))
        sys.exit(1)

    project_id = args.study
    program_id = args.program_id

    aws_secret_name = env_cfg.aws_secret_name
    aws_profile = env_cfg.aws_profile
    aws_region = env_cfg.region

    compound_project_id = f"{program_id}-{project_id}"

    logger.info(
        "Synthetic | Env: %s | Project: %s | Version: %s",
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
    sub = create_gen3_submission_class(api_key)

    # Determine node list
    if args.node:
        nodes_to_delete = [args.node]
        logger.info(
            "%s[SINGLE NODE]%s Targeting node: %s",
            BLUE, RESET, args.node,
        )
    else:
        try:
            nodes, source = resolve_import_order(
                env_cfg=env_cfg,
                session=session,
                import_order=args.import_order,
                dict_version=args.dict_version,
                study_cfg=None,  # synthetic projects are never registered
            )
        except ImportOrderError as exc:
            logger.error(str(exc))
            sys.exit(1)
        nodes_to_delete = to_deletion_order(nodes, EXCLUDE_NODES)
        logger.info(
            "Import order source: %s (%s nodes, deletion order, "
            "excluding %s)",
            source,
            len(nodes_to_delete),
            EXCLUDE_NODES,
        )

    if args.prompt:
        confirm = input(
            f"Proceed with deletion for project "
            f"{compound_project_id}, data_version {args.version}, "
            f"{len(nodes_to_delete)} node(s)? (yes/no): "
        ).strip().lower()
        if confirm != "yes":
            logger.info("Deletion cancelled by user.")
            return

    # Process each node
    total_deleted = 0
    total_skipped = 0
    failed_nodes = []
    total_nodes = len(nodes_to_delete)

    for idx, node in enumerate(nodes_to_delete, start=1):
        logger.info(
            "%s[Node %d/%d]%s | Project: %-10s | "
            "Node: %-25s | Querying...",
            BLUE, idx, total_nodes, RESET,
            compound_project_id, node,
        )

        try:
            deleted = delete_node_records_by_property(
                gen3_submission=sub,
                program_id=program_id,
                project_id=project_id,
                node=node,
                property_name="data_version",
                property_value=args.version,
                batch_size=args.batch_size,
                batch_delay=args.batch_delay,
                verbose=args.verbose,
            )
        except Gen3SubmissionQueryError as exc:
            logger.warning(
                "%s[SKIP]%s    | Node: %-25s | GraphQL rejected the "
                "data_version filter (property not in this node's "
                "schema?): %s",
                YELLOW, RESET, node, exc,
            )
            total_skipped += 1
            continue
        except Exception as exc:
            logger.error(
                "%s[FAILED]%s  | Node: %-25s | %s",
                RED, RESET, node, exc,
            )
            failed_nodes.append(node)
            continue

        if deleted == 0:
            logger.info(
                "%s[SKIP]%s    | Project: %-10s | "
                "Node: %-25s | No records found",
                YELLOW, RESET,
                compound_project_id, node,
            )
            total_skipped += 1
            continue

        logger.info(
            "%s[SUCCESS]%s | Project: %-10s | "
            "Node: %-25s | Deleted: %s",
            GREEN, RESET,
            compound_project_id, node, deleted,
        )
        total_deleted += deleted

        if idx < total_nodes:
            time.sleep(args.delay)

    logger.info(
        "=========================================="
    )
    logger.info(
        "Deletion complete. Total deleted: %s | "
        "Nodes skipped: %s | Nodes failed: %s",
        total_deleted,
        total_skipped,
        len(failed_nodes),
    )

    if failed_nodes:
        logger.error(
            "Failed nodes: %s", ", ".join(failed_nodes)
        )
        sys.exit(1)

    # No records matched the requested version on any node. Usually the data
    # was generated without the property (see `g3dt synth generate
    # --data-version`), so surface the actionable hint rather than a silent
    # "0 deleted".
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
