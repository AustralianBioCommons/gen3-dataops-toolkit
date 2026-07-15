#!/bin/bash

# Exit on any error
set -e
set -o pipefail

# 1. Pull the dictionary at a specific version.
# 2. Upload the dictionary to S3.
# 3. Restart microservices (schema).

# Usage: bash deploy_dd.sh <profile>
# <profile> is display-only; all configuration comes from G3DT_* environment
# variables exported by the g3dt CLI (g3dt.config.script_env).
PROFILE=$1

if [ -z "$PROFILE" ]; then
    echo "Usage: $0 <profile> (test|staging|prod) — run via the g3dt CLI"
    exit 1
fi

# Defining script paths (sibling scripts ship together inside the package)
SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"
SERVICE_DIR="${SCRIPT_DIR}/.."

# Configuration from G3DT_* env vars (fail loudly if a required one is missing)
VERSION="${G3DT_DICTIONARY_VERSION:?G3DT_DICTIONARY_VERSION not set — run via the g3dt CLI}"
SCHEMA_S3_URI="${G3DT_SCHEMA_S3_URI:?G3DT_SCHEMA_S3_URI not set — run via the g3dt CLI}"
DOMAIN="${G3DT_DOMAIN:?G3DT_DOMAIN not set — run via the g3dt CLI}"
APP_NAME="${G3DT_APP_NAME:?G3DT_APP_NAME not set — run via the g3dt CLI}"
NAMESPACE="${G3DT_NAMESPACE:?G3DT_NAMESPACE not set — run via the g3dt CLI}"
CLUSTER_NAME="${G3DT_CLUSTER_NAME:?G3DT_CLUSTER_NAME not set — run via the g3dt CLI}"
SCHEMA_REPO="${G3DT_SCHEMA_REPO:?G3DT_SCHEMA_REPO not set — run via the g3dt CLI}"
REGION="${G3DT_REGION:-ap-southeast-2}"
EKS_ARN="${G3DT_EKS_ARN:-}"
# Downloaded schemas live outside the installed package.
SCHEMA_DIR="${G3DT_SCHEMA_DIR:-$HOME/.g3dt/schemas}"
ARGO_SCRIPT_DIR="${SERVICE_DIR}/k8s_ops"

# Never export an empty AWS_PROFILE (empty means ambient credentials).
if [ -n "${G3DT_AWS_PROFILE:-}" ]; then
    export AWS_PROFILE="${G3DT_AWS_PROFILE}"
    echo "==== [0] Configuring AWS PROFILE as '${AWS_PROFILE}' for profile '${PROFILE}' ===="
else
    echo "==== [0] No AWS profile set for '${PROFILE}' — using ambient AWS credentials ===="
fi

echo "Updating kubeconfig for cluster: ${CLUSTER_NAME}"
# eks_arn is optional; only pass --role-arn when it is set.
ROLE_ARG=""
if [ -n "${EKS_ARN}" ] && [ "${EKS_ARN}" != "null" ]; then
    ROLE_ARG="--role-arn ${EKS_ARN}"
fi
if ! aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${REGION}" ${ROLE_ARG}; then
    echo "Error: Failed to update kubeconfig for cluster ${CLUSTER_NAME}"
    exit 1
fi

echo "==== [1] Pulling dictionary for version ${VERSION} ===="
bash "${SERVICE_DIR}/dictionary/pull_dict.sh" "https://raw.githubusercontent.com/${SCHEMA_REPO}/refs/tags/${VERSION}/dictionary/prod_dict/acdc_schema.json"

echo "==== [2] Uploading dictionary to S3: s3://${SCHEMA_S3_URI} ===="
python3 "${SERVICE_DIR}/dictionary/upload_dictionary.py" "${SCHEMA_DIR}/acdc_schema_${VERSION}.json" "s3://${SCHEMA_S3_URI}"

echo "==== [3] Restarting microservices (schema) ===="
bash "${ARGO_SCRIPT_DIR}/argocd_restart_schema.sh" -d "${DOMAIN}" -a "${APP_NAME}" -n "${NAMESPACE}"
