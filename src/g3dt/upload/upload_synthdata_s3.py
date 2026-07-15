import boto3
from urllib import parse
import os
import logging

logger = logging.getLogger(__name__)
logger.setLevel(logging.INFO)
if not logger.hasHandlers():
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s %(levelname)s %(name)s: %(message)s')
    handler.setFormatter(formatter)
    logger.addHandler(handler)


def upload_file_with_tags(
    local_file_path: str,
    bucket_name: str,
    s3_object_key: str,
    tags: dict,
    s3_client=None,
):
    """
    Upload a file to S3 with tags.

    Args:
        local_file_path (str): Path to the local file.
        bucket_name (str): Name of the S3 bucket.
        s3_object_key (str): S3 object key (destination path in bucket).
        tags (dict): Dictionary of tags to apply.
        s3_client: Optional boto3 S3 client. If None, a new client is created.
    """
    if s3_client is None:
        s3_client = boto3.client("s3")
    tag_string = parse.urlencode(tags)
    try:
        s3_client.upload_file(
            Filename=local_file_path,
            Bucket=bucket_name,
            Key=s3_object_key,
            ExtraArgs={"Tagging": tag_string},
        )
        logger.info(f"Upload Successful: {local_file_path}")
    except FileNotFoundError:
        logger.error(f"The file was not found: {local_file_path}")
    except Exception as e:
        logger.exception(
            f"An error occurred during upload of {local_file_path}: {e}"
        )


def get_synth_files(synth_folder: str) -> list:
    """
    Recursively collects all files in the given synth_folder.

    Args:
        synth_folder (str): Path to the folder containing synth files.

    Returns:
        list: List of file paths (relative to synth_folder) for all files found.
    """
    synth_files = []
    try:
        for root, dirs, files in os.walk(synth_folder):
            rel_root = root.replace(synth_folder, "")
            for f in files:
                synth_files.append(os.path.join(rel_root, f))
    except Exception as e:
        logger.exception(
            f"An error occurred while collecting synth files: {e}"
        )
    return synth_files


def get_node_name(path):
    """
    Extracts the node name from a given file or folder path.

    Args:
        path (str): The file or folder path.

    Returns:
        str: The node name, which is the last component of the path without its extension.
    """
    try:
        return path.split("/")[-1].split(".")[0]
    except Exception as e:
        logger.exception(
            f"An error occurred while extracting node name from {path}: {e}"
        )
        return ""

def get_study_id(path):
    """
    Extracts the study ID from a given file or folder path.

    Args:
        path (str): The file or folder path.

    Returns:
        str: The study ID, which is the second component of the path.
    """
    try:
        return path.split("/")[0]
    except Exception as e:
        logger.exception(
            f"An error occurred while extracting study ID from {path}: {e}"
        )
        return ""

def upload_synth_folder_to_s3(
    local_folder_path: str,
    bucket_name: str,
    study_id: str,
    submission_date: str,
    ingest_tag: str = 'true',
    data_release_version: str = None
):
    """
    Uploads all JSON files from a local synth folder to an S3 bucket, tagging each file.

    Args:
        local_folder_path (str): Path to the local synth folder.
        bucket_name (str): Name of the S3 bucket.
        study_id (str): Study identifier to tag and organize files.
        submission_date (str): Submission date to tag and organize files.
        ingest_tag (str, optional): Value for the 'ingest' tag. Defaults to 'true'.
        data_release_version (str, optional): Data release version to tag and organize files.
    """
    try:
        synth_files = get_synth_files(local_folder_path)
        submission_date = submission_date.replace("_", "-")
        for f in synth_files:
            if not f.endswith(".json"):
                logger.warning(f"Skipping non-JSON file: {f}")
                continue

            node_name = get_node_name(f)
            study_id = get_study_id(f)

            tags = {
                "ingest": ingest_tag,
                "submission_date": submission_date,
                "study_id": study_id,
                "node": node_name,
                "data_release_version": data_release_version
            }

            try:
                upload_file_with_tags(
                    local_file_path=os.path.join(local_folder_path, f),
                    bucket_name=bucket_name,
                    s3_object_key=f"{submission_date}_synthetic_metadata/{f}",
                    tags=tags,
                )
                logger.info(
                    f"SUCCESS: Uploaded {os.path.join(local_folder_path, f)} "
                    f"with tags: {tags} to {bucket_name}/{f}"
                )
            except Exception as e:
                logger.exception(f"Failed to upload {f}: {e}")
    except Exception as e:
        logger.exception(
            f"An error occurred in upload_synth_folder_to_s3: {e}"
        )