#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

usage() {
    cat <<EOF
Usage: $(basename "$0") --studies <comma-separated-studies> --env <environment>

Run upload_metadata.py sequentially for each study.

Arguments:
  --studies   Comma-separated list of study config keys (e.g. ausdiab_staging,caughtcad_staging)
  --env       Environment string passed to the Python script (e.g. staging_ec2)

Run via the g3dt CLI:
  g3dt metadata upload-all \\
    --studies ausdiab_staging,caughtcad_staging,edcad_staging \\
    --env staging_ec2

Failure logs are written under ~/.g3dt/logs/.
EOF
    exit 1
}

# ---------- Parse arguments ----------
STUDIES=""
ENV=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --studies)
            STUDIES="$2"
            shift 2
            ;;
        --env)
            ENV="$2"
            shift 2
            ;;
        *)
            echo "ERROR: Unknown argument: $1"
            usage
            ;;
    esac
done

if [[ -z "$STUDIES" || -z "$ENV" ]]; then
    echo "ERROR: --studies and --env are required."
    usage
fi

# ---------- Production safety check ----------
if echo "$ENV $STUDIES" | grep -qi "prod"; then
    echo "ERROR: Production environment detected in arguments."
    echo "       This script is not intended for production use. Aborting."
    exit 1
fi

# ---------- Setup ----------
# Logs go outside the installed package.
LOG_DIR="$HOME/.g3dt/logs"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
FAILED_LOG="${LOG_DIR}/${TIMESTAMP}_bulk_upload_failed.log"
mkdir -p "${LOG_DIR}"

IFS=',' read -ra STUDY_LIST <<< "$STUDIES"
FAIL_COUNT=0

echo "============================================"
echo "Bulk upload started at $(date)"
echo "Environment : ${ENV}"
echo "Studies     : ${STUDIES}"
echo "Failure log : ${FAILED_LOG}"
echo "============================================"
echo ""

# ---------- Sequential execution ----------
for study in "${STUDY_LIST[@]}"; do
    echo "--------------------------------------------"
    echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Starting upload for study: ${study}"
    echo "--------------------------------------------"

    if python3 "${SCRIPT_DIR}/upload_metadata.py" \
        --study "$study" --env "$ENV"; then
        echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Completed successfully: ${study}"
    else
        EXIT_CODE=$?
        FAIL_COUNT=$((FAIL_COUNT + 1))
        echo "[$(date +%Y-%m-%d\ %H:%M:%S)] FAILED: ${study} (exit code ${EXIT_CODE})"
        echo "[$(date +%Y-%m-%d\ %H:%M:%S)] ${study} exit_code=${EXIT_CODE}" >> "$FAILED_LOG"
    fi

    echo ""
done

# ---------- Summary ----------
echo "============================================"
echo "Bulk upload finished at $(date)"
echo "Total studies : ${#STUDY_LIST[@]}"
echo "Failures      : ${FAIL_COUNT}"

if [[ $FAIL_COUNT -gt 0 ]]; then
    echo "See failure details: ${FAILED_LOG}"
    exit 1
fi

echo "All studies uploaded successfully."
exit 0
