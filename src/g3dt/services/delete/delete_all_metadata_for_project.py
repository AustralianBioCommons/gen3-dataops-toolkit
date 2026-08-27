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
from g3dt.import_order import (
    ImportOrderError,
    resolve_import_order,
    to_deletion_order,
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
        "--synthetic",
        action="store_true",
        default=False,
        help=(
            "Registry-free mode for synthetic data: --study is used directly "
            "as the Gen3 project code (no SSM study registry lookup), with "
            "the program from --program-id."
        ),
    )
    parser.add_argument(
        "--program-id",
        default="program1",
        help="Gen3 program for --synthetic mode.",
    )
    parser.add_argument(
        "--import-order",
        default=None,
        help=(
            "Path or s3:// URI of DataImportOrder.txt. Default: auto "
            "(the study's release bucket, then ./DataImportOrder.txt, "
            "then derived from the dictionary)."
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

    args = parser.parse_args()

    if args.import_order and args.dict_version:
        parser.error(
            "--import-order names the exact file; --dict-version derives one "
            "— pass only one."
        )

    # Env facts from SSM; the study registry from the marker or
    # SSM /{project}/{env}/studies/* (legacy studies.yaml fallback until 5.0).
    # Synthetic projects are not registered studies: --synthetic skips the
    # registry and takes --study as the Gen3 project code itself.
    try:
        env_cfg = g3dt_config.resolve_env(args.env)
        if args.synthetic:
            study_cfg = None
            project_id = args.study
            program_id = args.program_id
        else:
            study_cfg = g3dt_config.resolve_study(args.study, args.env)
            project_id = study_cfg.project_id
            program_id = study_cfg.program_id
    except g3dt_config.ConfigError as exc:
        logger.error(str(exc))
        sys.exit(1)

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
        try:
            nodes, source = resolve_import_order(
                env_cfg=env_cfg,
                session=session,
                import_order=args.import_order,
                dict_version=args.dict_version,
                study_cfg=study_cfg,
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
