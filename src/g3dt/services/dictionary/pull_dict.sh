#!/bin/bash

show_help() {
    echo "Usage: $0 <dict_url> [output_file]"
    echo
    echo "Download a dictionary JSON file from the specified URL."
    echo
    echo "Arguments:"
    echo "  dict_url       The URL to download the dictionary JSON from."
    echo "  output_file    Optional. The file to save the dictionary as (default: acdc_schema.json)."
}

if [[ "$1" == "-h" || "$1" == "--help" || "$#" -lt 1 ]]; then
    show_help
    exit 1
fi

DICT_URL="$1"

# Downloads are written outside the installed package (never into site-packages).
SCHEMA_DIR="${G3DT_SCHEMA_DIR:-$HOME/.g3dt/schemas}"
mkdir -p "$SCHEMA_DIR"

# Extract version tag after '/tags/' or '/tag/' or '/refs/tags/' in the URL
if [[ "$DICT_URL" =~ /tags/([^/]+)/ ]]; then
    VERSION="${BASH_REMATCH[1]}"
elif [[ "$DICT_URL" =~ /refs/tags/([^/]+)/ ]]; then
    VERSION="${BASH_REMATCH[1]}"
else
    VERSION="unknown"
fi

# Set output filename, including version tag if not overridden by user
if [[ -n "$2" ]]; then
    OUTPUT_BASENAME="$2"
else
    # Get the filename from the URL
    BASE_NAME=$(basename "$DICT_URL")
    # Insert version before file extension if possible
    if [[ "$BASE_NAME" == *.* ]]; then
        EXTENSION="${BASE_NAME##*.}"
        NAME_NO_EXT="${BASE_NAME%.*}"
        OUTPUT_BASENAME="${NAME_NO_EXT}_${VERSION}.${EXTENSION}"
    else
        OUTPUT_BASENAME="${BASE_NAME}_${VERSION}"
    fi
fi

OUTPUT_FILE="${SCHEMA_DIR}/${OUTPUT_BASENAME}"

echo "Downloading dictionary from $DICT_URL..."
wget -O "$OUTPUT_FILE" "$DICT_URL"

if [ $? -eq 0 ]; then
    echo "Download complete: $OUTPUT_FILE"
else
    echo "Download failed." >&2
    exit 1
fi