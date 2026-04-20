#!/usr/bin/env bash
# Generate an image from a text prompt using the Jimeng CLI.
# Usage: bash scripts/text2image.sh "<prompt>" [ratio] [resolution]
set -euo pipefail

PROMPT="${1:?Usage: $0 \"<prompt>\" [ratio=16:9] [resolution=2k]}"
RATIO="${2:-16:9}"
RESOLUTION="${3:-2k}"
POLL="${POLL:-60}"

echo "Generating image..."
echo "  Prompt:     $PROMPT"
echo "  Ratio:      $RATIO"
echo "  Resolution: $RESOLUTION"
echo ""

dreamina text2image \
  --prompt="$PROMPT" \
  --ratio="$RATIO" \
  --resolution_type="$RESOLUTION" \
  --poll="$POLL"
