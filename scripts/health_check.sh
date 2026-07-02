#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
HEALTH_CHECK_URL="${HEALTH_CHECK_URL:-http://127.0.0.1:8000/health/ready/}"
HEALTH_CHECK_TIMEOUT_SECONDS="${HEALTH_CHECK_TIMEOUT_SECONDS:-8}"
HEALTH_CHECK_RETRIES="${HEALTH_CHECK_RETRIES:-5}"
HEALTH_CHECK_SLEEP_SECONDS="${HEALTH_CHECK_SLEEP_SECONDS:-2}"

log() {
    printf '[health-check] %s\n' "$*"
}

fail() {
    printf '[health-check] ERROR: %s\n' "$*" >&2
    exit 1
}

require_tools() {
    command -v curl >/dev/null 2>&1 || fail "curl is required"
}

check_url() {
    local attempt
    local body_file
    body_file="$(mktemp)"
    trap 'rm -f "$body_file"' EXIT

    for attempt in $(seq 1 "$HEALTH_CHECK_RETRIES"); do
        log "checking $HEALTH_CHECK_URL (attempt $attempt/$HEALTH_CHECK_RETRIES)"
        if curl --fail --silent --show-error --max-time "$HEALTH_CHECK_TIMEOUT_SECONDS" "$HEALTH_CHECK_URL" >"$body_file"; then
            log "ready: $(cat "$body_file")"
            return 0
        fi
        sleep "$HEALTH_CHECK_SLEEP_SECONDS"
    done
    fail "readiness endpoint did not become healthy"
}

main() {
    cd "$APP_DIR"
    require_tools
    check_url
}

main "$@"
