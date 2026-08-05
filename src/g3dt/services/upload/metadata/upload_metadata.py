import sys
import logging
import argparse
import yaml
import json
from g3dt.upload.metadata_submitter import (
    list_metadata_jsons_s3,
    find_data_import_order_file_s3,
    create_boto3_session,
    get_gen3_api_key_aws_secret,
    fetch_prior_uploads,
    infer_api_endpoint_from_jwt,
    MetadataSubmitter,
)


def setup_logger(debug=False):
    logger = logging.getLogger()
    logger.setLevel(logging.DEBUG if debug else logging.INFO)
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


def main():
    parser = argparse.ArgumentParser(description="Unified Metadata Upload Script")
    parser.add_argument(
        "--study", required=True, help="Study name (e.g., ausdiab, caughtcad, edcad)"
    )
    parser.add_argument(
        "--env", required=True,
        help="Environment to upload to (e.g., test, staging, prod, staging_ec2, prod_ec2)"
    )
    parser.add_argument("--specific-node", help="Submit only a specific node")
    parser.add_argument(
        "--force-reupload", action="store_true",
        help="Proceed even if the audit table already holds rows for this "
        "project + version + endpoint (uploads are additive: re-running "
        "duplicates the records)",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="Enable debug logging"
    )

    args = parser.parse_args()

    logger = setup_logger(debug=args.debug)

    # Env facts + resource names from SSM; the study registry from the marker
    # or s3://<metadata-bucket>/config/studies.yaml.
    try:
        env_cfg = g3dt_config.resolve_env(args.env)
        study_cfg = g3dt_config.resolve_study(args.study, args.env)
    except g3dt_config.ConfigError as exc:
        logger.error(str(exc))
        sys.exit(1)

    project_id = study_cfg.project_id
    program_id = study_cfg.program_id
    base_dir = study_cfg.s3_metadata_path

    aws_secret_name = env_cfg.aws_secret_name
    aws_profile = env_cfg.aws_profile
    aws_region = env_cfg.region

    rc = resolver.resolve(
        g3dt_config.require_project(),
        g3dt_config.env_base(args.env),
        profile=aws_profile,
    )
    # Upload-tracking table: conventional name in the env's metadata DB/bucket
    # (exactly like the CDK's `releases` table).
    database = rc.metadata_db
    table = g3dt_config.METADATA_UPLOAD_TABLE
    table_location = f"s3://{rc.metadata_bucket}/{g3dt_config.METADATA_UPLOAD_PREFIX}"
    athena_s3_output = rc.athena_output_location
    workgroup = rc.athena_workgroup

    logger.info(
        f"Starting metadata submission for {args.study} "
        f"in {args.env} environment."
    )
    logger.info(f"Project ID: {project_id}, Program ID: {program_id}")
    logger.info(f"S3 Path: {base_dir}")

    session = create_boto3_session(
        aws_profile=aws_profile, aws_region=aws_region
    )

    logger.info("Listing metadata JSON files in S3 directory.")
    metadata_files = list_metadata_jsons_s3(s3_uri=base_dir, session=session)
    if not metadata_files:
        raise FileNotFoundError(
            f"No metadata *.json files found in {base_dir}"
        )
    logger.info(
        f"Located {len(metadata_files)} metadata *.json files in {base_dir}"
    )

    logger.info("Finding DataImportOrder.txt in the S3 directory.")
    import_order_file_path = find_data_import_order_file_s3(
        s3_uri=base_dir, session=session
    )
    logger.info(f"Import order file found: {import_order_file_path}")

    # Fetch Gen3 API key
    if aws_secret_name.startswith('/'):
        logger.info(f"Loading Gen3 API key from local file: {aws_secret_name}")
        with open(aws_secret_name, 'r') as f:
            api_key = json.load(f)
    else:
        logger.info(
            f"Fetching Gen3 API key from AWS Secrets Manager: {aws_secret_name}"
        )
        api_key = get_gen3_api_key_aws_secret(
            secret_name=aws_secret_name,
            region_name=aws_region,
            session=session,
        )

    submitter = MetadataSubmitter(
        metadata_file_list=metadata_files,
        api_key=api_key,
        project_id=project_id,
        data_import_order_path=import_order_file_path,
        database=database,
        table=table,
        athena_s3_output=athena_s3_output,
        workgroup=workgroup,
        table_location=table_location,
        program_id=program_id,
        max_size_kb=100,  # Default to 100 as seen in most scripts
        max_retries=3,
        aws_profile=aws_profile,
        aws_region=aws_region,
    )

    # Pre-flight: uploads are additive, so re-running one silently doubles
    # the project's records at that version. Refuse when the audit table
    # already holds rows for this project + version + endpoint.
    if not args.force_reupload:
        version = submitter._collect_versions_from_metadata_file_list()
        api_endpoint = infer_api_endpoint_from_jwt(api_key["api_key"])
        prior = fetch_prior_uploads(
            database=database,
            table=table,
            project_id=project_id,
            version=version,
            api_endpoint=api_endpoint,
            athena_s3_output=athena_s3_output,
            workgroup=workgroup,
            boto3_session=session,
        )
        if not prior.empty:
            logger.error(
                "Refusing to upload: the audit table %s.%s already records an "
                "upload of project '%s' version %s to %s. Re-running would "
                "duplicate every record. Bump the release version, or pass "
                "--force-reupload if the duplication is intended.",
                database, table, project_id, version, api_endpoint,
            )
            sys.exit(2)

    try:
        logger.info("Submitting metadata to Gen3.")
        submitter.submit_metadata(specific_node=args.specific_node)
        logger.info(
            f"Finished submitting metadata for {args.study} ({project_id})"
        )
    except Exception as e:
        logger.error(f"An unexpected error occurred during submission: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()
