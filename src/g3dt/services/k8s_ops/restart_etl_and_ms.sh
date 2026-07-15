#!/bin/bash

# Exit on any error
set -e
# Ensure pipeline failures are captured
set -o pipefail

# Usage: bash restart_etl_and_ms.sh <profile>
# <profile> is display-only; all configuration comes from G3DT_* environment
# variables exported by the g3dt CLI (g3dt.config.script_env).
PROFILE="${1:-}"

if [ -z "$PROFILE" ]; then
    echo "Usage: $0 <profile> (test|staging|prod) — run via the g3dt CLI"
    exit 1
fi

# Defining script paths
SCRIPT_DIR="$( cd -- "$( dirname -- "${BASH_SOURCE[0]}" )" &> /dev/null && pwd )"

# Configuration from G3DT_* env vars (fail loudly if a required one is missing)
CLUSTER_NAME="${G3DT_CLUSTER_NAME:?G3DT_CLUSTER_NAME not set — run via the g3dt CLI}"
DOMAIN="${G3DT_DOMAIN:?G3DT_DOMAIN not set — run via the g3dt CLI}"
APP_NAME="${G3DT_APP_NAME:?G3DT_APP_NAME not set — run via the g3dt CLI}"
NAMESPACE="${G3DT_NAMESPACE:?G3DT_NAMESPACE not set — run via the g3dt CLI}"
REGION="${G3DT_REGION:-ap-southeast-2}"
# This script lives alongside the argo scripts inside the package.
ARGO_SCRIPT_DIR="${SCRIPT_DIR}"

# Never export an empty AWS_PROFILE (empty means ambient credentials).
if [ -n "${G3DT_AWS_PROFILE:-}" ]; then
    export AWS_PROFILE="${G3DT_AWS_PROFILE}"
    echo "==== Configuring AWS PROFILE as '${AWS_PROFILE}' for profile '${PROFILE}' ===="
else
    echo "==== No AWS profile set for '${PROFILE}' — using ambient AWS credentials ===="
fi

echo "Updating kubeconfig for cluster: ${CLUSTER_NAME}"
if ! aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${REGION}"; then
    echo "ERROR: Failed to update kubeconfig. Check your AWS credentials/profile."
    exit 1
fi

echo "==== Restarting microservices (etl) ===="
bash "${ARGO_SCRIPT_DIR}/argocd_restart_etl.sh" \
    -d "${DOMAIN}" \
    -a "${APP_NAME}" \
    -n "${NAMESPACE}" \
    -s

echo "==== Restarting microservices (schema) ===="
bash "${ARGO_SCRIPT_DIR}/argocd_restart_ms.sh" \
    -d "${DOMAIN}" \
    -a "${APP_NAME}" \
    -n "${NAMESPACE}" \
    -r "sheepdog-deployment,guppy-deployment,peregrine-deployment,portal-deployment"
