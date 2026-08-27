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
# values. The LLM vendor/model arrive as $G3DT_LLM_PROVIDER / $G3DT_LLM_MODEL
# (resolved by g3dt: CLI flags > SSM app/llm_* > default) and are forwarded to
# the simulator as flags; the API key file path arrives as $LLM_API_KEY_FILE.
# The old ~/.g3dt/.env is no longer read.

set -euo pipefail

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
                         'random' needs no key; 'llm' uses \$G3DT_LLM_PROVIDER /
                         \$G3DT_LLM_MODEL / \$LLM_API_KEY_FILE (set by g3dt).
  --seed N               RNG seed for reproducible output.
  --data-version V       Stamp every record's data_version property with V
                         (requires the dictionary to declare data_version;
                         enables versioned deletion later).
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
DATA_VERSION=""
OUTPUT_ROOT="${DEFAULT_OUTPUT_ROOT}"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --schema)       SCHEMA="$2"; shift 2 ;;
        --version)      VERSION="$2"; shift 2 ;;
        --studies)      STUDIES="$2"; shift 2 ;;
        --num-records)  NUM_RECORDS="$2"; shift 2 ;;
        --provider)     PROVIDER="$2"; shift 2 ;;
        --seed)         SEED="$2"; shift 2 ;;
        --data-version) DATA_VERSION="$2"; shift 2 ;;
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
    # --set pins a declared data property to a constant on every record; the
    # simulator errors before generating if no node declares data_version.
    [[ -n "$DATA_VERSION" ]] && CMD+=(--set "data_version=${DATA_VERSION}")
    if [[ "$PROVIDER" == "llm" ]]; then
        # Vendor/model resolved by g3dt (flags > SSM > default) and forwarded
        # as simulator flags, because the simulator's own precedence puts
        # flags above any .env or environment variable.
        [[ -n "${G3DT_LLM_PROVIDER:-}" ]] && CMD+=(--llm-provider "$G3DT_LLM_PROVIDER")
        [[ -n "${G3DT_LLM_MODEL:-}" ]] && CMD+=(--llm-model "$G3DT_LLM_MODEL")
        # Neutralize any .env in the caller's CWD: /dev/null exists (satisfies
        # the simulator's exists=True check) and dotenv-parses to empty, so
        # only the flags above and the inherited $LLM_API_KEY_FILE apply.
        CMD+=(--env-file /dev/null)
    fi
    "${CMD[@]}"
done

echo "Done. Output under ${OUTPUT_ROOT}/${VERSION}/"
