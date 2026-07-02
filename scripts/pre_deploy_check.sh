#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
DEPLOY_USER="${DEPLOY_USER:-ubuntu}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
ENV_FILE="${ENV_FILE:-$APP_DIR/.env.production}"
SETTINGS_FILE="${SETTINGS_FILE:-djangoProject/settings.py}"

log() {
    printf '[pre-deploy] %s\n' "$*"
}

fail() {
    printf '[pre-deploy] ERROR: %s\n' "$*" >&2
    exit 1
}

current_user() {
    id -un
}

require_deploy_user() {
    if [[ "$(id -u)" == "0" ]]; then
        fail "do not run deployment git checks as root; use $DEPLOY_USER"
    fi
    if [[ "$(current_user)" != "$DEPLOY_USER" ]]; then
        fail "deployment git checks must run as $DEPLOY_USER, current user is $(current_user)"
    fi
}

require_repo() {
    [[ -d "$APP_DIR/.git" ]] || fail "$APP_DIR is not a git repository"
    cd "$APP_DIR"
}

require_deploy_branch() {
    local branch
    branch="$(git rev-parse --abbrev-ref HEAD)"
    [[ "$branch" == "$DEPLOY_BRANCH" ]] || fail "deployment branch must be $DEPLOY_BRANCH, current branch is $branch"
}

require_clean_status() {
    local status
    status="$(git status --porcelain)"
    [[ -z "$status" ]] || fail "git status must be clean before deployment"
}

require_clean_settings() {
    local status
    status="$(git status --porcelain -- "$SETTINGS_FILE")"
    [[ -z "$status" ]] || fail "$SETTINGS_FILE has uncommitted changes; move production configuration to .env.production"
}

require_env_file() {
    [[ -f "$ENV_FILE" ]] || fail "$ENV_FILE does not exist"
    [[ -r "$ENV_FILE" ]] || fail "$ENV_FILE is not readable by $(current_user)"
}

main() {
    require_deploy_user
    require_repo
    require_deploy_branch
    require_clean_status
    require_clean_settings
    require_env_file
    log "ok: branch=$DEPLOY_BRANCH, git clean, settings clean, env file present"
}

main "$@"
