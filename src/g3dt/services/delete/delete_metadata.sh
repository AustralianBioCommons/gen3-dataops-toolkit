#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Exit code the per-study worker uses to signal "no data at this version —
# skipped" (see delete_metadata_by_guid.py SKIP_EXIT_CODE, shipped alongside
# this script).
SKIP_EXIT_CODE=3

usage() {
    cat <<EOF
Usage: $(basename "$0") --studies <name[:version|all],...> --env <environment> [--version <version|all>] [--node <node>] [--synthetic] [--program-id <program>]

Delete metadata for each study sequentially, in a single job.

Arguments:
  --studies    Comma-separated study config keys, each optionally qualified with
               its own version (e.g. ausdiab_staging:0.7.5,cdah_staging:0.8.1,
               or bare ausdiab_staging to take the --version default)
  --env        Environment string passed to the Python worker (e.g. staging_ec2)
  --version    Default version for bare --studies entries (e.g. 0.9.8), or 'all'
  --node       (optional) Restrict deletion to a single node type
  --synthetic  (optional) Registry-free synthetic-data mode: each study name is
               the Gen3 project code itself (no SSM study registry lookup)
  --program-id (optional) Gen3 program for --synthetic mode (default program1)
  --import-order (optional) Path or s3:// URI of DataImportOrder.txt. Default:
               auto — the study's release bucket (registered studies), then
               ./DataImportOrder.txt, then derived from the dictionary
  --dict-version (optional) Dictionary git tag to derive the node order from
               (default: the env's deployed dictionary)

Behaviour:
  * version 'all'          -> delete_all_metadata_for_project.py (deletes whole nodes)
  * version <x.y.z>        -> delete_metadata_by_guid.py (Athena GUID lookup for that version)
  * --synthetic + 'all'    -> delete_all_metadata_for_project.py --synthetic
  * --synthetic + version  -> delete_synth_metadata_by_version.py (GraphQL data_version filter)

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
SYNTHETIC=0
PROGRAM_ID="program1"
IMPORT_ORDER=""
DICT_VERSION=""

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
        --synthetic)
            SYNTHETIC=1
            shift
            ;;
        --program-id)
            PROGRAM_ID="$2"
            shift 2
            ;;
        --import-order)
            IMPORT_ORDER="$2"
            shift 2
            ;;
        --dict-version)
            DICT_VERSION="$2"
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

# Expand '--studies name[:version],...' into two parallel arrays. An entry with
# no ':version' takes the --version default. Validating the whole list up front
# means a typo in the last entry cannot leave the earlier studies already
# deleted.
IFS=',' read -ra STUDY_ENTRIES <<< "$STUDIES"
STUDY_NAMES=()
STUDY_VERSIONS=()
for entry in "${STUDY_ENTRIES[@]}"; do
    name="${entry%%:*}"
    # Test for the ':' explicitly: for a bare 'name', "${entry#*:}" expands to
    # 'name' rather than to the empty string, which would silently become the
    # version.
    if [[ "$entry" == *:* ]]; then
        entry_version="${entry#*:}"
    else
        entry_version="$VERSION"
    fi
    if [[ -z "$name" ]]; then
        echo "ERROR: empty study name in --studies entry '${entry}'."
        usage
    fi
    if [[ -z "$entry_version" ]]; then
        echo "ERROR: study '${name}' has no version: use '${name}:<version|all>' in --studies, or pass --version as the default."
        usage
    fi
    STUDY_NAMES+=("$name")
    STUDY_VERSIONS+=("$entry_version")
done

# ---------- Setup ----------
# Logs go outside the installed package.
LOG_DIR="$HOME/.g3dt/logs"
TIMESTAMP="$(date +%Y%m%d_%H%M%S)"
FAILED_LOG="${LOG_DIR}/${TIMESTAMP}_delete_failed.log"
mkdir -p "${LOG_DIR}"

DELETED_COUNT=0
SKIPPED_COUNT=0
FAIL_COUNT=0

echo "============================================"
echo "Metadata delete started at $(date)"
echo "Environment : ${ENV}"
echo "Studies     : ${STUDIES}"
echo "Version     : ${VERSION:-(per study, from --studies)}"
[[ -n "$NODE" ]] && echo "Node        : ${NODE}"
[[ $SYNTHETIC -eq 1 ]] && echo "Synthetic   : yes (program: ${PROGRAM_ID})"
echo "Failure log : ${FAILED_LOG}"
echo "============================================"
echo ""

# ---------- Sequential execution ----------
for i in "${!STUDY_NAMES[@]}"; do
    study="${STUDY_NAMES[$i]}"
    study_version="${STUDY_VERSIONS[$i]}"
    # Lower-cased for the 'all' comparison only; the worker gets the original.
    study_version_lc="$(echo "$study_version" | tr '[:upper:]' '[:lower:]')"

    echo "--------------------------------------------"
    echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Starting deletion for study: ${study} (version: ${study_version})"
    echo "--------------------------------------------"

    if [[ "$study_version_lc" == "all" ]]; then
        CMD=("${G3DT_PYTHON:-python3}" "${SCRIPT_DIR}/delete_all_metadata_for_project.py"
             --study "$study" --env "$ENV")
        [[ $SYNTHETIC -eq 1 ]] && CMD+=(--synthetic --program-id "$PROGRAM_ID")
    elif [[ $SYNTHETIC -eq 1 ]]; then
        CMD=("${G3DT_PYTHON:-python3}" "${SCRIPT_DIR}/delete_synth_metadata_by_version.py"
             --study "$study" --env "$ENV" --version "$study_version"
             --program-id "$PROGRAM_ID" --skip-if-empty)
    else
        CMD=("${G3DT_PYTHON:-python3}" "${SCRIPT_DIR}/delete_metadata_by_guid.py"
             --study "$study" --env "$ENV" --version "$study_version" --skip-if-empty)
    fi
    [[ -n "$NODE" ]] && CMD+=(--node "$NODE")
    [[ -n "$IMPORT_ORDER" ]] && CMD+=(--import-order "$IMPORT_ORDER")
    [[ -n "$DICT_VERSION" ]] && CMD+=(--dict-version "$DICT_VERSION")

    # Run the worker without aborting the loop on a non-zero exit.
    set +e
    "${CMD[@]}"
    EXIT_CODE=$?
    set -e

    if [[ $EXIT_CODE -eq 0 ]]; then
        echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Completed: ${study}"
        DELETED_COUNT=$((DELETED_COUNT + 1))
    elif [[ $EXIT_CODE -eq $SKIP_EXIT_CODE ]]; then
        echo "[$(date +%Y-%m-%d\ %H:%M:%S)] Skipped (no data at version ${study_version}): ${study}"
        SKIPPED_COUNT=$((SKIPPED_COUNT + 1))
    else
        FAIL_COUNT=$((FAIL_COUNT + 1))
        echo "[$(date +%Y-%m-%d\ %H:%M:%S)] FAILED: ${study} (exit code ${EXIT_CODE})"
        # The version is recorded because one job can now delete two versions
        # of the same study.
        echo "[$(date +%Y-%m-%d\ %H:%M:%S)] ${study} version=${study_version} exit_code=${EXIT_CODE}" >> "$FAILED_LOG"
    fi

    echo ""
done

# ---------- Summary ----------
echo "============================================"
echo "Metadata delete finished at $(date)"
echo "Total studies : ${#STUDY_NAMES[@]}"
echo "Deleted       : ${DELETED_COUNT}"
echo "Skipped       : ${SKIPPED_COUNT}"
echo "Failures      : ${FAIL_COUNT}"

if [[ $FAIL_COUNT -gt 0 ]]; then
    echo "See failure details: ${FAILED_LOG}"
    exit 1
fi

echo "All studies processed (deleted ${DELETED_COUNT}, skipped ${SKIPPED_COUNT})."
exit 0
