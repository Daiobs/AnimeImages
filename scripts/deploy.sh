#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
DEPLOY_USER="${DEPLOY_USER:-ubuntu}"
DEPLOY_GROUP="${DEPLOY_GROUP:-$DEPLOY_USER}"
SERVICE_NAME="${SERVICE_NAME:-animeimages}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env.production}"
PYTHON_BIN="${PYTHON_BIN:-$APP_DIR/.venv/bin/python}"
PIP_BIN="${PIP_BIN:-$APP_DIR/.venv/bin/pip}"
DRY_RUN="${DEPLOY_DRY_RUN:-0}"

log() {
    printf '[deploy] %s\n' "$*"
}

fail() {
    printf '[deploy] ERROR: %s\n' "$*" >&2
    exit 1
}

run() {
    log "+ $*"
    if [[ "$DRY_RUN" == "1" ]]; then
        return 0
    fi
    "$@"
}

run_sudo() {
    log "+ sudo $*"
    if [[ "$DRY_RUN" == "1" ]]; then
        return 0
    fi
    sudo "$@"
}

current_user() {
    id -un
}

require_deploy_user() {
    if [[ "$(id -u)" == "0" ]]; then
        fail "do not run deploy.sh as root; use $DEPLOY_USER"
    fi
    if [[ "$(current_user)" != "$DEPLOY_USER" ]]; then
        fail "deploy.sh must run as $DEPLOY_USER, current user is $(current_user)"
    fi
}

require_repo() {
    [[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git repository"
    cd "$APP_DIR"
}

ensure_repo_ownership() {
    if find "$APP_DIR" \( ! -user "$DEPLOY_USER" -o ! -group "$DEPLOY_GROUP" \) -print -quit | grep -q .; then
        log "mixed ownership detected under $APP_DIR; fixing to $DEPLOY_USER:$DEPLOY_GROUP before git operations"
        run_sudo chown -R "$DEPLOY_USER:$DEPLOY_GROUP" "$APP_DIR"
    else
        log "$APP_DIR ownership is already $DEPLOY_USER:$DEPLOY_GROUP"
    fi
}

git_process_active() {
    pgrep -u "$DEPLOY_USER" -x git >/dev/null 2>&1
}

cleanup_stale_git_lock() {
    local lock_file="$APP_DIR/.git/index.lock"
    if [[ ! -e "$lock_file" ]]; then
        return 0
    fi
    if git_process_active; then
        fail "$lock_file exists while a git process is active; aborting"
    fi
    log "removing stale git lock: $lock_file"
    run rm -f "$lock_file"
}

run_pre_deploy_check() {
    log "+ env ENV_FILE=$ENV_FILE APP_DIR=$APP_DIR DEPLOY_USER=$DEPLOY_USER $APP_DIR/scripts/pre_deploy_check.sh"
    env \
        ENV_FILE="$ENV_FILE" \
        APP_DIR="$APP_DIR" \
        DEPLOY_USER="$DEPLOY_USER" \
        "$APP_DIR/scripts/pre_deploy_check.sh"
}

load_environment() {
    if [[ "$DRY_RUN" == "1" ]]; then
        log "dry run: skipping environment load from $ENV_FILE"
        return 0
    fi
    [[ -f "$ENV_FILE" ]] || fail "$ENV_FILE does not exist"
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
}

main() {
    require_deploy_user
    require_repo
    ensure_repo_ownership
    cleanup_stale_git_lock
    run_pre_deploy_check

    run git fetch origin
    cleanup_stale_git_lock
    run git reset --hard origin/main

    load_environment
    run "$PIP_BIN" install -r requirements.txt
    run "$PYTHON_BIN" manage.py migrate
    run "$PYTHON_BIN" manage.py collectstatic --noinput
    run_sudo systemctl restart "$SERVICE_NAME"
    log "deployment finished"
}

main "$@"
