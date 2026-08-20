#!/usr/bin/env bash
# Relaunch Catalant monitor after intentional process recycle or crash.
# Hard-timeouts hung Python so Chromium leaks cannot pin a dead worker forever.
set -uo pipefail

cd "$(dirname "$0")"

RECYCLE_HOURS="${PROCESS_RECYCLE_HOURS:-3}"
# Hard kill slightly after expected recycle window (default + 1 hour buffer).
DEFAULT_HARD_TIMEOUT="$(awk -v h="$RECYCLE_HOURS" 'BEGIN { printf "%d", (h * 3600) + 3600 }')"
HARD_TIMEOUT="${PROCESS_HARD_TIMEOUT_SECONDS:-$DEFAULT_HARD_TIMEOUT}"
RELAUNCH_DELAY="${PROCESS_RELAUNCH_DELAY_SECONDS:-5}"
RECYCLE_EXIT="${PROCESS_RECYCLE_EXIT_CODE:-75}"

echo "event=start_sh_boot recycle_hours=${RECYCLE_HOURS} hard_timeout_seconds=${HARD_TIMEOUT}"

while true; do
  set +e
  if command -v timeout >/dev/null 2>&1; then
    timeout --signal=TERM --kill-after=30 "${HARD_TIMEOUT}" python -u monitor.py "$@"
    code=$?
    # GNU timeout: 124 = timed out
    if [ "$code" -eq 124 ]; then
      echo "event=start_sh_hard_timeout seconds=${HARD_TIMEOUT}"
    fi
  else
    python -u monitor.py "$@"
    code=$?
  fi
  set -e

  if [ "$code" -eq "$RECYCLE_EXIT" ]; then
    echo "event=start_sh_relaunch reason=process_recycle exit_code=${code}"
  elif [ "$code" -eq 0 ]; then
    echo "event=start_sh_relaunch reason=clean_exit exit_code=${code}"
  else
    echo "event=start_sh_relaunch reason=crash_or_error exit_code=${code}"
  fi

  # Best-effort leftover Chromium cleanup between launches
  pkill -f 'chromedriver|chromium|crashpad' 2>/dev/null || true
  sleep "${RELAUNCH_DELAY}"
done
