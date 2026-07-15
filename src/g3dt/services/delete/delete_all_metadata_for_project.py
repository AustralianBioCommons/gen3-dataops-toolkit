import sys
import logging
import argparse
import yaml
from g3dt.upload.metadata_submitter import (
    create_boto3_session,
    get_gen3_api_key_aws_secret,
    create_gen3_submission_class,
)
from g3dt.upload.metadata_deleter import (
    delete_project_metadata,
)

# ANSI colour codes
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
            "Delete ALL metadata for a Gen3 project. Iterates "
            "through nodes in reverse DataImportOrder and calls "
            "Gen3's delete_nodes API for each."
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

    args = parser.parse_args()

    # Env facts from SSM; the study registry from the marker or
    # s3://<metadata-bucket>/config/studies.yaml.
    try:
        env_cfg = g3dt_config.resolve_env(args.env)
        study_cfg = g3dt_config.resolve_study(args.study, args.env)
    except g3dt_config.ConfigError as exc:
        logger.error(str(exc))
        sys.exit(1)

    project_id = study_cfg.project_id
    program_id = study_cfg.program_id

    aws_secret_name = env_cfg.aws_secret_name
    aws_profile = env_cfg.aws_profile
    aws_region = env_cfg.region

    logger.info(
        "Study: %s | Env: %s | Program: %s | Project: %s",
        args.study, args.env, program_id, project_id,
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
        nodes_to_delete = load_import_order(args.import_order)
        logger.info(
            "Loaded %s nodes from %s (deletion order, "
            "excluding %s)",
            len(nodes_to_delete),
            args.import_order,
            EXCLUDE_NODES,
        )

    # Delete
    delete_project_metadata(
        gen3_submission=sub,
        program_id=program_id,
        project_id=project_id,
        nodes=nodes_to_delete,
        prompt_for_confirmation=args.prompt,
    )

    logger.info(
        "=========================================="
    )
    logger.info("Deletion complete.")


if __name__ == "__main__":
    main()
