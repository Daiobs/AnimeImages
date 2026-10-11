#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
SERVICE_NAME="${SERVICE_NAME:-animeimages}"
RUN_HEALTH_CHECK="${RUN_HEALTH_CHECK:-1}"

log() {
    printf '[restart] %s\n' "$*"
}

run_sudo() {
    log "+ sudo $*"
    sudo "$@"
}

main() {
    cd "$APP_DIR"
    run_sudo systemctl daemon-reload
    run_sudo systemctl restart "$SERVICE_NAME"
    run_sudo systemctl status "$SERVICE_NAME" --no-pager -l

    if [[ "$RUN_HEALTH_CHECK" == "1" ]]; then
        bash "$SCRIPT_DIR/health_check.sh"
    fi
}

main "$@"
