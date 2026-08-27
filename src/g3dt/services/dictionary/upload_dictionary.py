import boto3
import json
import logging
import sys
from botocore.exceptions import ClientError

from g3dt.config import ConfigError, normalize_s3_location

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[logging.StreamHandler()]
)
logger = logging.getLogger(__name__)


def get_s3_client(profile_name=None):
    """
    Creates an S3 client with optional AWS profile.

    Args:
        profile_name (str, optional): AWS profile name.

    Returns:
        boto3.client: S3 client.
    """
    if profile_name:
        session = boto3.Session(profile_name=profile_name)
        return session.client("s3")
    return boto3.client("s3")


def get_dict_version(dict_file_path):
    """
    Extracts the dictionary version from the provided JSON/YAML combo settings.

    Args:
        dict_file_path (str): Path to the dictionary file.

    Returns:
        str: Dictionary version.
    """
    with open(dict_file_path, "r", encoding="utf-8") as f:
        dict_data = json.load(f)
    return dict_data.get("_settings.yaml", {}).get("_dict_version", None)


def upload_dict_to_s3(dict_file_path, s3_target_uri, dict_version, profile_name=None):
    """
    Uploads a dictionary file to S3 with metadata.

    Args:
        dict_file_path (str): Local dictionary path.
        s3_target_uri (str): Target like s3://bucket/key. Forgiving: bare
            bucket/key, a doubled s3:// scheme, and S3 endpoint https URLs
            are all accepted (see g3dt.config.normalize_s3_location).
        dict_version (str): Dictionary version string.
        profile_name (str, optional): AWS profile name.

    Returns:
        bool: True on success, False otherwise.
    """
    try:
        location = normalize_s3_location(s3_target_uri, param="s3_uri argument")
    except ConfigError as e:
        logger.error(str(e))
        return False
    if "/" not in location:
        logger.error(f"S3 location missing an object key: {s3_target_uri}")
        return False

    try:
        bucket, key = location.split("/", 1)
        # Set S3 metadata key to "version" instead of "dict_version"
        extra_args = {"Metadata": {"version": dict_version or "unknown"}}
        s3_client = get_s3_client(profile_name)
        s3_client.upload_file(dict_file_path, bucket, key, ExtraArgs=extra_args)
        logger.info(
            f"Successfully uploaded '{dict_file_path}' (version: {dict_version}) to {s3_target_uri}"
        )
        return True
    except ClientError as e:
        logger.error(f"Failed to upload file to {s3_target_uri}: {e}")
        raise
    except Exception as e:
        logger.error(f"Unexpected error uploading file: {e}")
        raise


def main():
    if len(sys.argv) < 3 or len(sys.argv) > 4:
        print(
            "Usage: python upload_dictionary.py <local_file_path> <s3_uri> [aws_profile]",
            file=sys.stderr,
        )
        sys.exit(1)

    local_file_path = sys.argv[1]
    s3_uri = sys.argv[2]
    profile_name = sys.argv[3] if len(sys.argv) == 4 else None

    dict_version = get_dict_version(local_file_path)
    if dict_version is None:
        logger.error(f"Could not determine dictionary version from {local_file_path}")
        sys.exit(1)

    success = upload_dict_to_s3(local_file_path, s3_uri, dict_version, profile_name)
    if not success:
        sys.exit(1)
    # You may add a completion message if desired


if __name__ == "__main__":
    main()
