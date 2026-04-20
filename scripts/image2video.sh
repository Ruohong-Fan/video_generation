#!/usr/bin/env bash
# Animate an existing image into a video using the Jimeng CLI.
# Usage: bash scripts/image2video.sh <image_path_or_url> "<motion prompt>" [duration]
set -euo pipefail

IMAGE="${1:?Usage: $0 <image_path_or_url> \"<motion prompt>\" [duration=5]}"
PROMPT="${2:?Usage: $0 <image_path_or_url> \"<motion prompt>\" [duration=5]}"
DURATION="${3:-5}"
POLL="${POLL:-120}"

echo "Animating image to video..."
echo "  Image:    $IMAGE"
echo "  Motion:   $PROMPT"
echo "  Duration: ${DURATION}s"
echo ""

dreamina image2video \
  --image "$IMAGE" \
  --prompt="$PROMPT" \
  --duration="$DURATION" \
  --poll="$POLL"
