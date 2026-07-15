#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Exit code the per-study worker uses to signal "no data at this version —
# skipped" (see delete_metadata_by_guid.py SKIP_EXIT_CODE, shipped alongside
# this script).
SKIP_EXIT_CODE=3

usage() {
    cat <<EOF
Usage: $(basename "$0") --studies <comma-separated-studies> --env <environment> --version <version|all> [--node <node>]

Delete metadata for each study sequentially, in a single job.

Arguments:
  --studies   Comma-separated list of study config keys (e.g. ausdiab_staging,caughtcad_staging)
  --env       Environment string passed to the Python worker (e.g. staging_ec2)
  --version   Metadata version to delete (e.g. 0.9.8), or 'all' for every version
  --node      (optional) Restrict deletion to a single node type

Behaviour:
  * --version all          -> delete_all_metadata_for_project.py (deletes whole nodes)
  * --version <x.y.z>      -> delete_metadata_by_guid.py (Athena GUID lookup for that version)

  A study that exists but has no data at the requested version is skipped and the
  loop continues. Only genuine errors (Gen3/AWS failures) count as failures.

Run via the g3dt CLI:
  g3dt delete metadata \\
    --studies ausdiab_staging,caughtcad_staging \\
    --env staging_ec2 \\
    --version 0.9.8

Failure logs are written under ~/.g3dt/logs/.
EOF
    exit 1
}

# ---------- Parse arguments ----------
STUDIES=""
ENV=""
VERSION=""
NODE=""

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
        --version)
            VERSION="$2"
            shift 2
            ;;
        --node)
            NODE="$2"
            shift 2
            ;;
        *)
            echo "ERROR: Unknown argument: $1"
            usage
            ;;
    esac
done

if [[ -z "$STUDIES" || -z "$ENV" || -z "$VERSION" ]]; then
    echo "ERROR: --studies, --env and --version are required."
    usage
fi

# Lower-case the version so 'ALL'/'All' are treated as 'all'.
VERSION_LC="$(echo "$VERSION" | tr '[:upper:]' '[:lower:]')"

# ---------- Setup ----------
# Logs go outside the installed package.
LOG_DIR="$HOME/.g3dt/logs"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
FAILED_LOG="${LOG_DIR}/${TIMESTAMP}_delete_failed.log"
mkdir -p "${LOG_DIR}"

IFS=',' read -ra STUDY_LIST <<< "$STUDIES"
DELETED_COUNT=0
SKIPPED_COUNT=0
FAIL_COUNT=0

echo "============================================"
echo "Metadata delete started at $(date)"
echo "Environment : ${ENV}"
echo "Studies     : ${STUDIES}"
echo "Version     : ${VERSION}"
[[ -n "$NODE" ]] && echo "Node        : ${NODE}"
echo "Failure log : ${FAILED_LOG}"
echo "============================================"
echo ""

# ---------- Sequential execution ----------
for study in "${STUDY_LIST[@]}"; do
    echo "--------------------------------------------"
    echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Starting deletion for study: ${study} (version: ${VERSION})"
    echo "--------------------------------------------"

    if [[ "$VERSION_LC" == "all" ]]; then
        CMD=(python3 "${SCRIPT_DIR}/delete_all_metadata_for_project.py"
             --study "$study" --env "$ENV")
        [[ -n "$NODE" ]] && CMD+=(--node "$NODE")
    else
        CMD=(python3 "${SCRIPT_DIR}/delete_metadata_by_guid.py"
             --study "$study" --env "$ENV" --version "$VERSION" --skip-if-empty)
        [[ -n "$NODE" ]] && CMD+=(--node "$NODE")
    fi

    # Run the worker without aborting the loop on a non-zero exit.
    set +e
    "${CMD[@]}"
    EXIT_CODE=$?
    set -e

    if [[ $EXIT_CODE -eq 0 ]]; then
        echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Completed: ${study}"
        DELETED_COUNT=$((DELETED_COUNT + 1))
    elif [[ $EXIT_CODE -eq $SKIP_EXIT_CODE ]]; then
        echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Skipped (no data at version ${VERSION}): ${study}"
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        echo "[$(date +%Y-%m-%d\ %H:%M:%S)] FAILED: ${study} (exit code ${EXIT_CODE})"
        echo "[$(date +%Y-%m-%d\ %H:%M:%S)] ${study} exit_code=${EXIT_CODE}" >> "$FAILED_LOG"
    fi

    echo ""
done

# ---------- Summary ----------
echo "============================================"
echo "Metadata delete finished at $(date)"
echo "Total studies : ${#STUDY_LIST[@]}"
echo "Deleted       : ${DELETED_COUNT}"
echo "Skipped       : ${SKIPPED_COUNT}"
echo "Failures      : ${FAIL_COUNT}"

if [[ $FAIL_COUNT -gt 0 ]]; then
    echo "See failure details: ${FAILED_LOG}"
    exit 1
fi

echo "All studies processed (deleted ${DELETED_COUNT}, skipped ${SKIPPED_COUNT})."
exit 0
