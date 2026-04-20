#!/usr/bin/env bash
# Poll the result of an async Jimeng generation task.
# Usage: bash scripts/query.sh <submit_id> [interval_seconds] [max_attempts]
set -euo pipefail

SUBMIT_ID="${1:?Usage: $0 <submit_id> [interval=15] [max_attempts=20]}"
INTERVAL="${2:-15}"
MAX="${3:-20}"

echo "Polling task: $SUBMIT_ID"
echo "  Interval: ${INTERVAL}s  Max attempts: $MAX"
echo ""

for i in $(seq 1 "$MAX"); do
  echo "Attempt $i/$MAX..."
  result=$(dreamina query_result --submit_id="$SUBMIT_ID" 2>&1)
  echo "$result"

  if echo "$result" | grep -q '"status":"done"'; then
    echo ""
    echo "Task complete."
    exit 0
  fi

  if [ "$i" -lt "$MAX" ]; then
    sleep "$INTERVAL"
  fi
done

echo ""
echo "Max attempts reached. Task may still be processing." >&2
exit 1
