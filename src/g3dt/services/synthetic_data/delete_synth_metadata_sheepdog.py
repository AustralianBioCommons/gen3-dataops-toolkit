import os
import argparse
import logging
from g3dt.upload.metadata_deleter import (
    delete_project_metadata,
)
from g3dt.upload.metadata_submitter import (
    create_boto3_session,
    get_gen3_api_key_aws_secret,
    create_gen3_submission_class,
)

EXCLUDE_NODES = [
    "program",
    "project",
    "acknowledgement",
    "publication",
]


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


def delete_for_projects(
    gen3_submission,
    program_id,
    project_ids,
    nodes,
    prompt_for_confirmation,
):
    for project_id in project_ids:
        logging.info(
            "Attempting to delete metadata for project '%s'",
            project_id,
        )
        try:
            delete_project_metadata(
                gen3_submission=gen3_submission,
                program_id=program_id,
                project_id=project_id,
                nodes=nodes,
                prompt_for_confirmation=prompt_for_confirmation,
            )
            logging.info(
                "Successfully deleted metadata for project '%s'.",
                project_id,
            )
        except Exception as e:
            logging.error(
                "Error deleting metadata for project '%s': %s",
                project_id, e,
            )
            raise e


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format='[%(asctime)s] [%(levelname)s] %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S',
    )

    script_path = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_path)
    logging.info("Changed working directory to %s", script_path)

    default_projects = [
        "AusDiab_Simulated",
        "EDCAD-PMS_Simulated",
        "Baker-Biobank_Simulated",
        "CAUGHT-CAD_Simulated",
        "BioHeart-CT_Simulated",
    ]
    default_program_id = "program1"
    default_aws_profile = "default"
    default_aws_region = "ap-southeast-2"
    default_prompt_for_confirmation = False

    parser = argparse.ArgumentParser(
        description=(
            "Delete synthetic project metadata from Gen3 "
            "Sheepdog API."
        ),
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        '-i', '--data-import-order-file',
        type=str,
        required=True,
        help="Path to the DataImportOrder.txt file.",
    )
    parser.add_argument(
        '-p', '--projects',
        type=str,
        default=",".join(default_projects),
        help="Comma separated list of Project IDs.",
    )
    parser.add_argument(
        '--program-id',
        type=str,
        default=default_program_id,
        help="Gen3 program name.",
    )
    parser.add_argument(
        '-s', '--aws-secret-name',
        type=str,
        help="AWS secret name for Gen3 API key.",
    )
    parser.add_argument(
        '-profile', '--aws-profile',
        type=str,
        default=default_aws_profile,
        help="AWS profile name.",
    )
    parser.add_argument(
        '-r', '--aws-region',
        type=str,
        default=default_aws_region,
        help="AWS region.",
    )
    parser.add_argument(
        '-confirm', '--prompt',
        action='store_true',
        default=default_prompt_for_confirmation,
        help="Prompt for confirmation before deleting.",
    )

    args = parser.parse_args()

    parsed_projects = [
        proj.strip()
        for proj in args.projects.split(",")
        if proj.strip()
    ]

    logging.info("Projects to delete: %s", parsed_projects)
    logging.info(
        "Data import order file: %s",
        args.data_import_order_file,
    )
    logging.info("AWS Secret Name: %s", args.aws_secret_name)
    logging.info("AWS Profile: %s", args.aws_profile)
    logging.info("AWS Region: %s", args.aws_region)
    logging.info(
        "Prompt for confirmation: %s", args.prompt,
    )

    if not os.path.isfile(args.data_import_order_file):
        raise FileNotFoundError(
            f"Import order file does not exist: "
            f"{args.data_import_order_file}"
        )

    nodes = load_import_order(args.data_import_order_file)

    session = create_boto3_session(
        aws_profile=args.aws_profile,
    )
    api_key = get_gen3_api_key_aws_secret(
        secret_name=args.aws_secret_name,
        region_name=args.aws_region,
        session=session,
    )
    sub = create_gen3_submission_class(api_key)

    delete_for_projects(
        gen3_submission=sub,
        program_id=args.program_id,
        project_ids=parsed_projects,
        nodes=nodes,
        prompt_for_confirmation=args.prompt,
    )