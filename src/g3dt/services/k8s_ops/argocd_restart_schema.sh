#!/bin/bash
# Script to restart argocd microservices for schema redeployment

# Function to display help message
usage() {
    echo "Usage: $0 [-d DOMAIN] [-a APPNAME] [-r RESOURCES] [-n NAMESPACE] [-k KIND] [-l] [-s]"
    echo "  -d DOMAIN      The domain for argocd login (example: cd.cad.test.biocommons.org.au)"
    echo "  -a APPNAME     The application name (example: uatgen3)"
    echo "  -r RESOURCES   Comma-separated string of microservice names to restart (default: \"sheepdog-deployment\" \"peregrine-deployment\" \"guppy-deployment\" \"portal-deployment\")"
    echo "  -n NAMESPACE   The namespace for the resources (default: cad)"
    echo "  -k KIND        The kind of resource to restart (default: Deployment)"
    echo "  -l             Bypass login"
    echo "  -s             Run 'argocd app sync' before restarts"
    exit 1
}

set -eo pipefail

# Set default values
RESOURCES=("sheepdog-deployment" "peregrine-deployment" "guppy-deployment" "portal-deployment")
NAMESPACE="cad"
KIND="Deployment"
LOGIN_REQUIRED=true
SYNC_APP=false

# Parse command line arguments
while getopts "d:a:r:n:k:hls" opt; do
    case ${opt} in
        d )
            DOMAIN=$OPTARG
            ;;
        a )
            APPNAME=$OPTARG
            ;;
        r )
            IFS=',' read -r -a RESOURCES <<< "$OPTARG"
            ;;
        n )
            NAMESPACE=$OPTARG
            ;;
        k )
            KIND=$OPTARG
            ;;
        l )
            LOGIN_REQUIRED=false
            ;;
        s )
            SYNC_APP=true
            ;;
        h )
            usage
            ;;
        \? )
            usage
            ;;
    esac
done

# Check if argocd is installed
if ! command -v argocd &> /dev/null
then
    echo "argocd CLI could not be found. Please install it to proceed."
    exit 1
fi

if [ "$LOGIN_REQUIRED" = true ]; then
    echo "logging into argocd via sso"
    argocd login --sso $DOMAIN
    echo "login successful"
else
    echo "Bypassing login as per user request"
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

# Iterate through resources
for RESOURCE in "${RESOURCES[@]}"; do
  echo "Restarting resource: $RESOURCE"
  
  # Run the restart action
  argocd app actions run $APPNAME restart --kind $KIND --resource-name $RESOURCE --namespace $NAMESPACE

  sleep 5
  
  # Wait for the resource to complete its restart
  echo "Waiting for $RESOURCE to finish..."
  while true; do
    echo "Checking $RESOURCE status..."
    STATUS=$(argocd app get $APPNAME -o json | jq --arg RESOURCE "$RESOURCE" '.status.resources[] | select(.name == $RESOURCE) | .health.status')
    echo "Status: $STATUS"
    if [ "$STATUS" == "\"Healthy\"" ]; then
      echo "$RESOURCE restarted successfully"
      break
    fi
    sleep 5 # Check every 5 seconds
  done
done
