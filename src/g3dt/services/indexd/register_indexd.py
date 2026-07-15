"""Register S3 data files with Gen3 indexd.

Scans one or more S3 prefixes, collects file metadata (name, md5, size),
registers each file with the indexd API, and writes results to Glue
Iceberg tables for downstream use by dbt silver models.

Usage
-----
python register_indexd.py \
    --s3-paths s3://bucket/path1 s3://bucket/path2 \
    --study edcad \
    --env staging

Studies come from the g3dt.yaml marker or s3://<metadata-bucket>/config/studies.yaml.
"""

import sys
import logging
import argparse
import json

import yaml
from gen3.auth import Gen3Auth
from gen3.index import Gen3Index

from g3dt.upload.metadata_submitter import (
    create_boto3_session,
    get_gen3_api_key_aws_secret,
    infer_api_endpoint_from_jwt,
)
from g3dt.indexd.indexd_registrar import (
    scan_s3_files,
    register_files_with_indexd,
    write_to_glue,
)


def setup_logger():
    logger = logging.getLogger()
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = logging.StreamHandler(sys.stdout)
        formatter = logging.Formatter(
            "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
        )
        handler.setFormatter(formatter)
        logger.addHandler(handler)
    return logger


# Shared config resolution (SSM-backed) — see src/g3dt/config.py
from g3dt import config as g3dt_config, resolver  # noqa: E402


def main():
    logger = setup_logger()

    parser = argparse.ArgumentParser(
        description="Register S3 files with Gen3 indexd"
    )
    parser.add_argument(
        "--s3-paths",
        nargs="+",
        required=True,
        help="One or more S3 prefixes to scan for files",
    )
    parser.add_argument(
        "--study",
        required=True,
        help="Study key (e.g. edcad, ausdiab, caughtcad)",
    )
    parser.add_argument(
        "--env",
        required=True,
        help="Environment (e.g. test, staging, prod, "
        "staging_ec2, prod_ec2)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Scan and write file_metadata only; skip indexd "
        "registration",
    )

    args = parser.parse_args()

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
    authz = [f"/programs/{program_id}/projects/{project_id}"]

    aws_secret_name = env_cfg.aws_secret_name
    aws_profile = env_cfg.aws_profile
    aws_region = env_cfg.region

    rc = resolver.resolve(
        g3dt_config.require_project(),
        g3dt_config.env_base(args.env),
        profile=aws_profile,
    )
    athena_s3_output = rc.athena_output_location
    workgroup = rc.athena_workgroup

    # Indexd tables: conventional names in the env's metadata DB/bucket
    # (exactly like the CDK's `releases` table).
    fm_database = rc.metadata_db
    fm_table = g3dt_config.FILE_METADATA_TABLE
    reg_database = rc.metadata_db
    reg_table = g3dt_config.INDEXD_REGISTRY_TABLE
    indexd_s3_path = f"s3://{rc.metadata_bucket}/{g3dt_config.INDEXD_PREFIX}"

    # --- boto3 session ---
    session = create_boto3_session(
        aws_profile=aws_profile, aws_region=aws_region
    )

    # --- Fetch API key early so endpoint can be stamped on tables ---
    if aws_secret_name.startswith("/"):
        logger.info(
            "Loading Gen3 API key from local file: %s",
            aws_secret_name,
        )
        with open(aws_secret_name, "r") as f:
            api_key = json.load(f)
    else:
        logger.info(
            "Fetching Gen3 API key from Secrets Manager: %s",
            aws_secret_name,
        )
        api_key = get_gen3_api_key_aws_secret(
            secret_name=aws_secret_name,
            region_name=aws_region,
            session=session,
        )

    base_url = infer_api_endpoint_from_jwt(api_key["api_key"])
    indexd_endpoint = base_url.replace("api/v0", "index/index")
    logger.info("Indexd endpoint: %s", indexd_endpoint)

    # --- Phase 1: scan S3 ---
    logger.info(
        "Scanning %d S3 path(s): %s",
        len(args.s3_paths),
        args.s3_paths,
    )
    file_df = scan_s3_files(args.s3_paths, boto3_session=session)
    file_df["study_id"] = args.study
    file_df["indexd_endpoint"] = indexd_endpoint
    logger.info("Found %d files.", len(file_df))

    if file_df.empty:
        logger.warning("No files found. Exiting.")
        sys.exit(0)

    # --- Write file_metadata table ---
    fm_location = (
        f"{indexd_s3_path.rstrip('/')}/file_metadata/"
        if indexd_s3_path
        else None
    )
    logger.info(
        "Writing file metadata to %s.%s", fm_database, fm_table
    )
    write_to_glue(
        df=file_df,
        database=fm_database,
        table=fm_table,
        athena_s3_output=athena_s3_output,
        table_location=fm_location,
        workgroup=workgroup,
        partition_cols=["study_id", "indexd_endpoint"],
        merge_cols=["file_name", "study_id", "indexd_endpoint"],
        schema_evolution=True,
        boto3_session=session,
    )

    if args.dry_run:
        logger.info("Dry run — skipping indexd registration.")
        sys.exit(0)

    auth = Gen3Auth(refresh_token=api_key)
    index = Gen3Index(auth)

    logger.info(
        "Registering %d files with indexd (authz=%s)",
        len(file_df),
        authz,
    )
    registry_df = register_files_with_indexd(
        index, file_df, authz
    )
    logger.info(
        "Successfully registered %d / %d files.",
        len(registry_df),
        len(file_df),
    )

    if registry_df.empty:
        logger.warning(
            "No new registrations. All files may already exist."
        )
        sys.exit(0)

    # --- Write indexd_registry table ---
    reg_location = (
        f"{indexd_s3_path.rstrip('/')}/indexd_registry/"
        if indexd_s3_path
        else None
    )
    logger.info(
        "Writing indexd registry to %s.%s",
        reg_database,
        reg_table,
    )
    write_to_glue(
        df=registry_df,
        database=reg_database,
        table=reg_table,
        athena_s3_output=athena_s3_output,
        table_location=reg_location,
        workgroup=workgroup,
        partition_cols=["study_id", "indexd_endpoint"],
        merge_cols=["did"],
        schema_evolution=True,
        boto3_session=session,
    )

    logger.info("Done.")


if __name__ == "__main__":
    main()
