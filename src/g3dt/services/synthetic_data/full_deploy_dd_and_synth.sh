#!/bin/bash

# Exit on any error
set -e
# Ensure pipeline failures are captured
set -o pipefail

# Usage: bash full_deploy_dd_and_synth.sh <profile>
# <profile> is display-only; all configuration comes from G3DT_* environment
# variables exported by the g3dt CLI (g3dt.config.script_env).
PROFILE=$1

if [ -z "$PROFILE" ]; then
    echo "Usage: $0 <profile> (test|staging|prod) — run via the g3dt CLI"
    exit 1
fi

# Any configured profile is allowed here. The production warning/confirmation is
# enforced by the CLI entrypoint (`g3dt synth deploy`) before this script runs,
# since that has a TTY for the prompt.

# Defining script paths (sibling scripts ship together inside the package)
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" &>/dev/null && pwd)"
SERVICE_DIR="${SCRIPT_DIR}/.."

# Configuration from G3DT_* env vars (fail loudly if a required one is missing)
VERSION="${G3DT_DICTIONARY_VERSION:?G3DT_DICTIONARY_VERSION not set — run via the g3dt CLI}"
AWS_SECRET_NAME="${G3DT_AWS_SECRET_NAME:?G3DT_AWS_SECRET_NAME not set — run via the g3dt CLI}"
SCHEMA_S3_URI="${G3DT_SCHEMA_S3_URI:?G3DT_SCHEMA_S3_URI not set — run via the g3dt CLI}"
DOMAIN="${G3DT_DOMAIN:?G3DT_DOMAIN not set — run via the g3dt CLI}"
APP_NAME="${G3DT_APP_NAME:?G3DT_APP_NAME not set — run via the g3dt CLI}"
NAMESPACE="${G3DT_NAMESPACE:?G3DT_NAMESPACE not set — run via the g3dt CLI}"
CLUSTER_NAME="${G3DT_CLUSTER_NAME:?G3DT_CLUSTER_NAME not set — run via the g3dt CLI}"
SCHEMA_REPO="${G3DT_SCHEMA_REPO:?G3DT_SCHEMA_REPO not set — run via the g3dt CLI}"
REGION="${G3DT_REGION:-ap-southeast-2}"
EKS_ARN="${G3DT_EKS_ARN:-}"
ARGO_SCRIPT_DIR="${SERVICE_DIR}/k8s_ops"

# Writable data lives outside the installed package.
SCHEMA_DIR="${G3DT_SCHEMA_DIR:-$HOME/.g3dt/schemas}"
SYNTH_BASE="${G3DT_SYNTH_DIR:-$HOME/.g3dt/synth_metadata}"

# Derived variables
PREV_VERSION="v1.0.0"
SYNTH_META_DIR="${SYNTH_BASE}/${VERSION}/"
DATA_IMPORT_ORDER_FILE="${SYNTH_BASE}/${PREV_VERSION}/AusDiab_Simulated/DataImportOrder.txt"

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
if ! aws eks update-kubeconfig \
    --name "${CLUSTER_NAME}" \
    --region "${REGION}" \
    ${ROLE_ARG}; then
    echo "ERROR: Failed to update kubeconfig. Check your AWS credentials/profile."
    exit 1
fi

echo "==== [1] Pulling dictionary for version ${VERSION} ===="
DICT_URL="https://raw.githubusercontent.com/${SCHEMA_REPO}"
DICT_URL="${DICT_URL}/refs/tags/${VERSION}"
DICT_URL="${DICT_URL}/dictionary/prod_dict/acdc_schema.json"
bash "${SERVICE_DIR}/dictionary/pull_dict.sh" "${DICT_URL}"

echo "==== [2] Uploading dictionary to S3: s3://${SCHEMA_S3_URI} ===="
UPLOAD_DICT_ARGS=("${SCHEMA_DIR}/acdc_schema_${VERSION}.json" "s3://${SCHEMA_S3_URI}")
# The optional trailing positional is the AWS profile; omit it for ambient credentials.
if [ -n "${G3DT_AWS_PROFILE:-}" ]; then
    UPLOAD_DICT_ARGS+=("${G3DT_AWS_PROFILE}")
fi
python3 "${SERVICE_DIR}/dictionary/upload_dictionary.py" "${UPLOAD_DICT_ARGS[@]}"

echo "==== [3] Restarting microservices (schema) ===="
bash "${ARGO_SCRIPT_DIR}/argocd_restart_schema.sh" \
    -d "${DOMAIN}" \
    -a "${APP_NAME}" \
    -n "${NAMESPACE}"

echo "==== [4] Deleting old synthetic data for version ${PREV_VERSION} ===="
DELETE_SYNTH_ARGS=(
    -p "AusDiab_Simulated,EDCAD-PMS_Simulated,PREDICT_Simulated,Baker-Biobank_Simulated,CAUGHT-CAD_Simulated,BioHeart-CT_Simulated"
    -s "${AWS_SECRET_NAME}"
    -i "${DATA_IMPORT_ORDER_FILE}"
)
if [ -n "${G3DT_AWS_PROFILE:-}" ]; then
    DELETE_SYNTH_ARGS+=(-profile "${G3DT_AWS_PROFILE}")
fi
python3 "${SERVICE_DIR}/synthetic_data/delete_synth_metadata_sheepdog.py" "${DELETE_SYNTH_ARGS[@]}"

echo "==== [5] Generating new synthetic data for version ${VERSION} (LLM-realistic) ===="
bash "${SERVICE_DIR}/synthetic_data/generate_synth_metadata.sh" \
    --schema "${SCHEMA_DIR}/acdc_schema_${VERSION}.json" \
    --version "${VERSION}" \
    --provider llm \
    --num-records "30,60,20,55" \
    --output-root "${SYNTH_BASE}"

echo "==== [6] Uploading new synthetic data for version ${VERSION} ===="
UPLOAD_SYNTH_ARGS=(
    --base-dir "${SYNTH_META_DIR}"
    --aws-secret-name "${AWS_SECRET_NAME}"
)
if [ -n "${G3DT_AWS_PROFILE:-}" ]; then
    UPLOAD_SYNTH_ARGS+=(--aws-profile "${G3DT_AWS_PROFILE}")
fi
python3 "${SERVICE_DIR}/synthetic_data/upload_synth_metadata_sheepdog.py" "${UPLOAD_SYNTH_ARGS[@]}"

echo "==== [7] Restarting microservices (schema and etl) ===="
bash "${ARGO_SCRIPT_DIR}/argocd_restart_etl.sh" \
    -d "${DOMAIN}" \
    -a "${APP_NAME}" \
    -n "${NAMESPACE}" \
    -s