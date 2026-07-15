#!/bin/bash
# Script to restart gen3 etl cronjob
usage() {
    echo "Usage: $0 [-e ENVIRONMENT] [-d DOMAIN] [-a APPNAME] [-n NAMESPACE] [-c ETL_CRONJOB] [-t CONTAINER] [-l] [-s]"
    echo "  -e ENVIRONMENT Environment profile (e.g. test, staging, prod). Config comes from G3DT_* env vars set by the g3dt CLI."
    echo "  -d DOMAIN      The domain for argocd login (example: cd.cad.test.biocommons.org.au)"
    echo "  -a APPNAME     The application name (example: uatgen3)"
    echo "  -n NAMESPACE   The namespace for the resources (example: cad)"
    echo "  -c ETL_CRONJOB The name of the ETL cronjob to run (default: etl-cronjob)"
    echo "  -t CONTAINER   The name of the container to check logs from (default: tube)"
    echo "  -l             Bypass login"
    echo "  -s             Sync the argocd app before restarting resources"
    exit 1
}

set -eo pipefail

# default values
ETL_CRONJOB="etl-cronjob"
CONTAINER_TO_CHECK="tube"
LOGIN_REQUIRED=true
SYNC_APP=false
ENVIRONMENT=""

while getopts "e:d:a:n:c:t:hls" opt; do
    case ${opt} in
        e ) ENVIRONMENT=$OPTARG ;;
        d ) DOMAIN=$OPTARG ;;
        a ) APPNAME=$OPTARG ;;
        n ) NAMESPACE=$OPTARG ;;
        c ) ETL_CRONJOB=$OPTARG ;;
        t ) CONTAINER_TO_CHECK=$OPTARG ;;
        l ) LOGIN_REQUIRED=false ;;
        s ) SYNC_APP=true ;;
        h ) usage ;;
        \? ) usage ;;
    esac
done

if ! command -v argocd &> /dev/null; then
    echo "argocd CLI could not be found. Please install it to proceed."
    exit 1
fi

# If an environment is specified (display-only), set up AWS profile, kubeconfig,
# and ArgoCD details from G3DT_* env vars exported by the g3dt CLI.
if [ -n "$ENVIRONMENT" ]; then
    CLUSTER_NAME="${G3DT_CLUSTER_NAME:?G3DT_CLUSTER_NAME not set — run via the g3dt CLI}"
    REGION="${G3DT_REGION:-ap-southeast-2}"

    # Set DOMAIN, APPNAME, NAMESPACE from env vars if not already set via flags
    DOMAIN="${DOMAIN:-${G3DT_DOMAIN:?G3DT_DOMAIN not set — run via the g3dt CLI}}"
    APPNAME="${APPNAME:-${G3DT_APP_NAME:?G3DT_APP_NAME not set — run via the g3dt CLI}}"
    NAMESPACE="${NAMESPACE:-${G3DT_NAMESPACE:?G3DT_NAMESPACE not set — run via the g3dt CLI}}"

    # Never export an empty AWS_PROFILE (empty means ambient credentials).
    if [ -n "${G3DT_AWS_PROFILE:-}" ]; then
        export AWS_PROFILE="${G3DT_AWS_PROFILE}"
        echo "==== Configuring AWS PROFILE as '${AWS_PROFILE}' for environment '${ENVIRONMENT}' ===="
    else
        echo "==== No AWS profile set for '${ENVIRONMENT}' — using ambient AWS credentials ===="
    fi

    echo "Updating kubeconfig for cluster: ${CLUSTER_NAME}"
    if ! aws eks update-kubeconfig --name "${CLUSTER_NAME}" --region "${REGION}"; then
        echo "ERROR: Failed to update kubeconfig. Check your AWS credentials/profile."
        exit 1
    fi
fi

# Save the current kubectl context before argocd login (which changes it)
KUBE_CONTEXT=$(kubectl config current-context 2>/dev/null)

if [ "$LOGIN_REQUIRED" = true ]; then
    echo "logging into argocd via sso"
    argocd login --sso $DOMAIN
    echo "login successful"
else
    echo "Bypassing login as per user request"
fi

# Restore the kubectl context after argocd login
if [ -n "$KUBE_CONTEXT" ]; then
    echo "Restoring kubectl context to: ${KUBE_CONTEXT}"
    kubectl config use-context "$KUBE_CONTEXT"
fi

if [ "$SYNC_APP" = true ]; then
    echo "Syncing ArgoCD app: $APPNAME"
    argocd app sync $APPNAME
    if [ $? -eq 0 ]; then
        echo "App $APPNAME synced successfully."
    else
        echo "Failed to sync app $APPNAME."
        exit 1
    fi
fi

RESOURCE="${ETL_CRONJOB}-$(date +%Y%m%d%H%M)"
JOB_NAME=$(kubectl create job --from=cronjob/${ETL_CRONJOB} ${RESOURCE} --namespace ${NAMESPACE} -o name | awk -F'/' '{print $2}')

if [ -z "$JOB_NAME" ]; then
  echo "Failed to create job. Exiting."
  exit 1
fi

echo "Job '$JOB_NAME' created. Waiting for it to complete..."

while true; do
  SUCCEEDED=$(kubectl get job $JOB_NAME -n $NAMESPACE -o jsonpath='{.status.succeeded}')
  FAILED=$(kubectl get job $JOB_NAME -n $NAMESPACE -o jsonpath='{.status.failed}')

  if [ "$SUCCEEDED" == "1" ]; then
    echo "✅ Job '$JOB_NAME' completed successfully!"
    break
  elif [ "$FAILED" == "1" ]; then
    echo "⚠️ Job '$JOB_NAME' failed."
    break
  else
    echo "⏳ Job '$JOB_NAME' is still running... (Succeeded: ${SUCCEEDED:-0}, Failed: ${FAILED:-0})"
    sleep 10
  fi
done


# Even if pod fails, it may still have passed, need to check logs
POD_NAME=$(kubectl get pods -n $NAMESPACE -l job-name=$JOB_NAME -o jsonpath='{.items[0].metadata.name}')
LOGS=$(kubectl logs $POD_NAME -n $NAMESPACE -c $CONTAINER_TO_CHECK)


if echo "$LOGS" | grep -q "Exit code: 0"; then
    echo "✅ ✅ ✅Log check passed! Found 'Exit code: 0'. The job is truly successful."
else
    echo "⚠️ Job Failed, as well as the log validation! Please review output:"
    echo "--------------------- LOGS START ---------------------"
    echo "$LOGS"
    echo "---------------------- LOGS END ----------------------"
fi

echo "Script finished."