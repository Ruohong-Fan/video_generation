#!/usr/bin/env bash
# Generate a video from a text prompt using the Jimeng CLI.
# Usage: bash scripts/text2video.sh "<prompt>" [duration] [ratio] [resolution]
set -euo pipefail

PROMPT="${1:?Usage: $0 \"<prompt>\" [duration=5] [ratio=16:9] [resolution=720P]}"
DURATION="${2:-5}"
RATIO="${3:-16:9}"
RESOLUTION="${4:-720P}"
POLL="${POLL:-120}"

echo "Generating video..."
echo "  Prompt:     $PROMPT"
echo "  Duration:   ${DURATION}s"
echo "  Ratio:      $RATIO"
echo "  Resolution: $RESOLUTION"
echo ""

dreamina text2video \
  --prompt="$PROMPT" \
  --duration="$DURATION" \
  --ratio="$RATIO" \
  --video_resolution="$RESOLUTION" \
  --poll="$POLL"
