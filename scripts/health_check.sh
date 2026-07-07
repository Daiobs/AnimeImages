#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
HEALTH_CHECK_URL="${HEALTH_CHECK_URL:-http://127.0.0.1:8000/health/ready/}"
HEALTH_CHECK_TIMEOUT_SECONDS="${HEALTH_CHECK_TIMEOUT_SECONDS:-8}"
HEALTH_CHECK_RETRIES="${HEALTH_CHECK_RETRIES:-5}"
HEALTH_CHECK_SLEEP_SECONDS="${HEALTH_CHECK_SLEEP_SECONDS:-2}"
body_file=""
error_file=""

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

cleanup() {
    if [[ -n "$body_file" && -f "$body_file" ]]; then
        rm -f "$body_file"
    fi
    if [[ -n "$error_file" && -f "$error_file" ]]; then
        rm -f "$error_file"
    fi
}

check_url() {
    local attempt=0
    local curl_status=0
    local curl_error=""
    local failure_message=""
    local response_body=""
    body_file="$(mktemp)"
    error_file="$(mktemp)"

    for attempt in $(seq 1 "$HEALTH_CHECK_RETRIES"); do
        log "checking $HEALTH_CHECK_URL (attempt $attempt/$HEALTH_CHECK_RETRIES)"
        if curl --fail --silent --show-error --max-time "$HEALTH_CHECK_TIMEOUT_SECONDS" "$HEALTH_CHECK_URL" >"$body_file" 2>"$error_file"; then
            log "ready: $(cat "$body_file")"
            return 0
        else
            curl_status=$?
        fi
        if [[ "$attempt" -lt "$HEALTH_CHECK_RETRIES" ]]; then
            sleep "$HEALTH_CHECK_SLEEP_SECONDS"
        fi
    done
    curl_error="$(tr '\n' ' ' <"$error_file")"
    response_body="$(cat "$body_file")"
    failure_message="readiness endpoint did not become healthy after $HEALTH_CHECK_RETRIES attempt(s): $HEALTH_CHECK_URL (last curl exit: $curl_status)"
    if [[ -n "$curl_error" ]]; then
        failure_message="$failure_message; last curl error: $curl_error"
    fi
    if [[ -n "$response_body" ]]; then
        failure_message="$failure_message; last response: $response_body"
    fi
    fail "$failure_message"
}

main() {
    cd "$APP_DIR"
    require_tools
    check_url
}

trap cleanup EXIT
main "$@"
