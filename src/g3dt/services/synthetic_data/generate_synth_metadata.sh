#!/usr/bin/env bash
#
# Generate schema-valid synthetic Gen3 metadata using gen3-metadata-simulator.
# One run per study writes a self-validated folder containing <node>.json +
# project.json + DataImportOrder.txt — the exact layout the upload step
# (upload_synth_metadata_sheepdog.py) consumes.
#
# The tool takes a LOCAL bundled Gen3 schema file (pulled by pull_dict.sh into
# ~/.g3dt/schemas/acdc_schema_<version>.json, or $G3DT_SCHEMA_DIR if set). The
# default provider is keyless 'random'; pass --provider llm for LLM-realistic
# values, in which case LLM config is read from ~/.g3dt/.env (or $G3DT_ENV_FILE
# if set): LLM_PROVIDER / LLM_MODEL / LLM_API_KEY_FILE.

set -euo pipefail

# LLM provider config file (lives outside the installed package).
ENV_FILE="${G3DT_ENV_FILE:-$HOME/.g3dt/.env}"

usage() {
    cat <<EOF
Usage: $(basename "$0") --schema <path> --version <ver> [options]

Generate synthetic Gen3 metadata (one folder per study) with gen3-metadata-simulator.

Required:
  --schema <path>        Path to the bundled Gen3 JSON schema (cad.json).
  --version <ver>        Version label for the output dir (e.g. v1.1.5).

Options:
  --studies s1,s2        Comma-separated study/project names.
                         Default: ${DEFAULT_STUDIES}
  --num-records N|n1,n2  Records per study: one number for all, or a comma list
                         (one per study). Default: ${DEFAULT_NUM_RECORDS}
  --provider random|llm  Value strategy. Default: ${DEFAULT_PROVIDER}
                         'random' needs no key; 'llm' reads LLM config from
                         ${ENV_FILE}.
  --seed N               RNG seed for reproducible output.
  --output-root DIR      Root output dir. Default: ${DEFAULT_OUTPUT_ROOT}
  -h, --help             Show this help and exit.

Examples:
  $(basename "$0") --schema ~/.g3dt/schemas/acdc_schema_v1.1.5.json --version v1.1.5
  $(basename "$0") --schema schema.json --version v1.1.5 --provider random --num-records 5
  $(basename "$0") --schema schema.json --version v1.1.5 --num-records "30,60,20,55"
EOF
}

# Defaults (kept in sync with full_deploy_dd_and_synth.sh's 4-study record list)
DEFAULT_STUDIES="AusDiab_Simulated,Baker-Biobank_Simulated,BioHeart-CT_Simulated,CAUGHT-CAD_Simulated"
DEFAULT_NUM_RECORDS=30
DEFAULT_PROVIDER=random
# Generated data goes outside the installed package.
DEFAULT_OUTPUT_ROOT="${G3DT_SYNTH_DIR:-$HOME/.g3dt/synth_metadata}"

SCHEMA=""
VERSION=""
STUDIES="${DEFAULT_STUDIES}"
NUM_RECORDS="${DEFAULT_NUM_RECORDS}"
PROVIDER="${DEFAULT_PROVIDER}"
SEED=""
OUTPUT_ROOT="${DEFAULT_OUTPUT_ROOT}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --schema)       SCHEMA="$2"; shift 2 ;;
        --version)      VERSION="$2"; shift 2 ;;
        --studies)      STUDIES="$2"; shift 2 ;;
        --num-records)  NUM_RECORDS="$2"; shift 2 ;;
        --provider)     PROVIDER="$2"; shift 2 ;;
        --seed)         SEED="$2"; shift 2 ;;
        --output-root)  OUTPUT_ROOT="$2"; shift 2 ;;
        -h|--help)      usage; exit 0 ;;
        *) echo "Unknown argument: $1" >&2; usage; exit 1 ;;
    esac
done

if [[ -z "$SCHEMA" || -z "$VERSION" ]]; then
    echo "Error: --schema and --version are required." >&2
    usage
    exit 1
fi
if [[ ! -f "$SCHEMA" ]]; then
    echo "Error: schema file not found: ${SCHEMA}" >&2
    echo "Hint: pull it first, e.g. 'g3dt dict pull'." >&2
    exit 1
fi
if ! command -v gen3-metadata-simulator &>/dev/null; then
    echo "Error: gen3-metadata-simulator not found. Run 'g3dt synth install-simulator'." >&2
    exit 1
fi

IFS=',' read -r -a STUDY_ARRAY <<< "$STUDIES"

# --num-records is either a single count applied to every study, or a comma list
# with one count per study (which must line up with --studies).
PER_STUDY_COUNTS=0
if [[ "$NUM_RECORDS" == *,* ]]; then
    PER_STUDY_COUNTS=1
    IFS=',' read -r -a NUM_RECORDS_ARRAY <<< "$NUM_RECORDS"
    if [[ ${#NUM_RECORDS_ARRAY[@]} -ne ${#STUDY_ARRAY[@]} ]]; then
        echo "Error: --num-records has ${#NUM_RECORDS_ARRAY[@]} values but there are ${#STUDY_ARRAY[@]} studies." >&2
        exit 1
    fi
fi

echo "Generating synthetic metadata: provider=${PROVIDER}, version=${VERSION}, studies=${STUDIES}"

for i in "${!STUDY_ARRAY[@]}"; do
    STUDY="${STUDY_ARRAY[$i]}"
    if [[ "$PER_STUDY_COUNTS" -eq 1 ]]; then
        N="${NUM_RECORDS_ARRAY[$i]}"
    else
        N="$NUM_RECORDS"
    fi
    OUT="${OUTPUT_ROOT}/${VERSION}/${STUDY}"
    mkdir -p "$OUT"
    echo "==== ${STUDY} (n=${N}) -> ${OUT} ===="

    CMD=(gen3-metadata-simulator generate
         --schema "$SCHEMA"
         --output-dir "$OUT"
         --project-code "$STUDY"
         --num-records "$N"
         --provider "$PROVIDER")
    [[ -n "$SEED" ]] && CMD+=(--seed "$SEED")
    # Point the LLM provider at the user-level env file regardless of the caller's CWD.
    if [[ "$PROVIDER" == "llm" && -f "${ENV_FILE}" ]]; then
        CMD+=(--env-file "${ENV_FILE}")
    fi
    "${CMD[@]}"
done

echo "Done. Output under ${OUTPUT_ROOT}/${VERSION}/"
