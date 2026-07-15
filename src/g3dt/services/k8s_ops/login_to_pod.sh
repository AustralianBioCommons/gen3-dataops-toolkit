#!/bin/bash

# login_testenv_pod.sh
# Script to log into a specified pod in a Kubernetes environment using a grep pattern.

set -e

show_help() {
    cat << EOF
Usage: ${0##*/} [OPTIONS]

Log into a running pod on aws. Direct SSO identity is used by default.
Also make sure you have an aws SSO profile configured on AWS cli.

Defaults come from the G3DT_* environment variables exported by the g3dt CLI
when set; flags always override them.

Options:
  -p PROFILE        AWS CLI profile to use (default: \$G3DT_AWS_PROFILE, else ambient credentials)
  -r REGION         AWS region (default: \$G3DT_REGION, else ap-southeast-2)
  -c CLUSTER_NAME   EKS cluster name (default: \$G3DT_CLUSTER_NAME)
  -a ROLE_ARN       Optional AWS IAM role ARN to assume (default: \$G3DT_EKS_ARN)
  -n NAMESPACE      Kubernetes namespace (default: \$G3DT_NAMESPACE, else cad)
  -g GREP_PATTERN   Pattern to grep for pod name (default: guppy)
  -x                Print all resolved commands before running them
  -h                Show this help message and exit

Example:
  ${0##*/} -p myprofile -n mynamespace -g sheepdog

EOF
}

set -eo pipefail

# Default values (taken from G3DT_* env vars when the g3dt CLI exported them)
PROFILE="${G3DT_AWS_PROFILE:-}"
AWS_REGION="${G3DT_REGION:-ap-southeast-2}"
CLUSTER_NAME="${G3DT_CLUSTER_NAME:?G3DT_CLUSTER_NAME not set — run via the g3dt CLI or pass -c}"
ROLE_ARN="${G3DT_EKS_ARN:-}"
NAME_SPACE="${G3DT_NAMESPACE:?G3DT_NAMESPACE not set — run via the g3dt CLI or pass -n}"
GREP_PATTERN="guppy"
PRINT_COMMANDS=0

while getopts "p:r:c:a:n:g:xh" opt; do
  case $opt in
    p) PROFILE="$OPTARG" ;;
    r) AWS_REGION="$OPTARG" ;;
    c) CLUSTER_NAME="$OPTARG" ;;
    a) ROLE_ARN="$OPTARG" ;;
    n) NAME_SPACE="$OPTARG" ;;
    g) GREP_PATTERN="$OPTARG" ;;
    x) PRINT_COMMANDS=1 ;;
    h)
      show_help
      exit 0
      ;;
    \?)
      show_help >&2
      exit 1
      ;;
  esac
done

# Prepare commands
CMD_AWS_SSO_LOGIN="aws sso login --profile ${PROFILE}"
CMD_AWS_EKS_UPDATE="aws eks update-kubeconfig --name ${CLUSTER_NAME} --region ${AWS_REGION} --profile ${PROFILE}"
if [[ -n "$ROLE_ARN" ]]; then
    CMD_AWS_EKS_UPDATE="${CMD_AWS_EKS_UPDATE} --role-arn ${ROLE_ARN}"
fi
CMD_GET_POD="kubectl get pods -n \"${NAME_SPACE}\" | grep Running | grep \"${GREP_PATTERN}\" | awk '{print \$1}'"

if [[ $PRINT_COMMANDS -eq 1 ]]; then
    echo "Resolved commands to be run:"
    echo ""
    echo "# 1. AWS SSO Login"
    echo "$CMD_AWS_SSO_LOGIN"
    echo ""
    echo "# 2. Update kubeconfig"
    echo "$CMD_AWS_EKS_UPDATE"
    echo ""
    echo "# 3. Get pod name"
    echo "$CMD_GET_POD"
    echo ""
fi

echo "Logging into aws sso with profile: $PROFILE"
eval "$CMD_AWS_SSO_LOGIN"

echo "Updating Kubernetes context for cluster: $CLUSTER_NAME in region: $AWS_REGION"
eval "$CMD_AWS_EKS_UPDATE"

# Get pod name using grep pattern
POD_NAME=$(kubectl get pods -n "${NAME_SPACE}" | grep Running | grep "${GREP_PATTERN}" | awk '{print $1}')

if [[ -z "$POD_NAME" ]]; then
    echo "Error: No running pod matching pattern '${GREP_PATTERN}' found in namespace '${NAME_SPACE}'." >&2
    exit 1
fi

CMD_KUBECTL_EXEC="kubectl exec -it \"$POD_NAME\" -n \"${NAME_SPACE}\" -- bash"

if [[ $PRINT_COMMANDS -eq 1 ]]; then
    echo "# 4. Exec into pod"
    echo "$CMD_KUBECTL_EXEC"
    echo ""
fi

echo "Logging into pod: $POD_NAME"
kubectl exec -it "$POD_NAME" -n "${NAME_SPACE}" -- bash
