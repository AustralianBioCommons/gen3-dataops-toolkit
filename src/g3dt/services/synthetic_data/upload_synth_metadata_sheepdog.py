import os
import argparse
import logging
from g3dt.upload.metadata_submitter import (
    find_data_import_order_file,
    list_metadata_jsons,
    create_boto3_session,
    get_gen3_api_key_aws_secret,
    MetadataSubmitter,
)

DEFAULT_REGION = "ap-southeast-2"
DEFAULT_PROFILE = None  # ambient credentials unless --aws-profile is given
DEFAULT_SUBMISSION_SIZE_KB = 50

def submit_synthetic_metadata(
    base_dir,
    project_id,
    aws_secret_name,
    aws_region=DEFAULT_REGION,
    aws_profile=DEFAULT_PROFILE,
    max_submission_size_kb=DEFAULT_SUBMISSION_SIZE_KB,
):
    """
    Main function to orchestrate metadata submission for multiple projects.
    """
    logger = logging.getLogger(__name__)
    script_path = os.path.dirname(os.path.abspath(__file__))
    os.chdir(script_path)
    logger.info("Changed working directory to %s", script_path)

    logger.info("Using base_dir=%s", base_dir)

    session = create_boto3_session(aws_profile)
    api_key = get_gen3_api_key_aws_secret(aws_secret_name, aws_region, session)

    logger.info(
        "Preparing to submit metadata for project_id=%s from %s",
        project_id,
        base_dir,
    )

    try:
        data_import_order_path = find_data_import_order_file(base_dir)
        file_list = list_metadata_jsons(base_dir)

        logger.info("Found %d metadata files for submission.", len(file_list))

        submitter = MetadataSubmitter(
            metadata_file_list=file_list,
            api_key=api_key,
            project_id=project_id,
            data_import_order_path=data_import_order_path,
            max_size_kb=max_submission_size_kb,
            aws_profile=aws_profile,
            upload_to_database=False,
            dataset_root=None,
            database=None,
            table=None
        )
        submitter.submit_metadata()
        logger.info("Finished submitting for project_id=%s", project_id)

    except FileNotFoundError as e:
        logger.error(
            "Could not find required files for project %s in %s. Error: %s",
            project_id,
            base_dir,
            e,
        )
        raise e
    except Exception as e:
        logger.error(
            "An unexpected error occurred during submission for project %s: %s",
            project_id,
            e,
        )
        raise e

def main():
    parser = argparse.ArgumentParser(
        description="Upload synthetic Gen3 metadata for all projects in a directory."
    )
    parser.add_argument(
        "--base-dir",
        required=True,
        help="Base directory where metadata project folders are found."
    )
    parser.add_argument(
        "--aws-secret-name",
        required=True,
        help="AWS secrets manager secret name containing the Gen3 API key."
    )
    parser.add_argument(
        "--aws-region",
        default=DEFAULT_REGION,
        help=f"AWS region for secrets manager (default: {DEFAULT_REGION})."
    )
    parser.add_argument(
        "--aws-profile",
        default=DEFAULT_PROFILE,
        help="AWS profile to use (default: None)."
    )
    parser.add_argument(
        "--submission-size-kb",
        type=int,
        default=DEFAULT_SUBMISSION_SIZE_KB,
        help="Maximum submission size in KB per chunk (default: 50)."
    )
    args = parser.parse_args()

    logging.basicConfig(
        format="%(asctime)s %(levelname)s:%(name)s:%(message)s", level=logging.INFO
    )
    logger = logging.getLogger(__name__)

    logger.info("Starting metadata submission script.")

    # Validate base_dir
    if not os.path.isdir(args.base_dir):
        logger.error(
            "The base directory %s does not exist or is not a directory.",
            args.base_dir
        )
        raise FileNotFoundError(
            "The base directory %s does not exist or is not a directory."
            % args.base_dir
        )

    # Find all projects: folders inside base_dir
    logger.info(
        "Looking for project subdirectories in base directory: %s",
        args.base_dir
    )
    project_id_list = os.listdir(args.base_dir)
    logger.info(
        "Found %d project subdirectories: %s",
        len(project_id_list),
        project_id_list
    )
    if not project_id_list:
        logger.error(
            "No project subdirectories found in base directory: %s",
            args.base_dir
        )
        raise FileNotFoundError(
            "No project subdirectories found in base directory: %s"
            % args.base_dir
        )

    for project_id in project_id_list:
        project_base_dir = os.path.join(args.base_dir, project_id)
        submit_synthetic_metadata(
            base_dir=project_base_dir,
            project_id=project_id,
            aws_secret_name=args.aws_secret_name,
            aws_region=args.aws_region,
            aws_profile=args.aws_profile,
            max_submission_size_kb=args.submission_size_kb,
        )

    logger.info("Script finished.")

if __name__ == "__main__":
    main()
