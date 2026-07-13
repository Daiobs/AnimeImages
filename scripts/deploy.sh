#!/usr/bin/env bash
set -Eeuo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_DIR="${APP_DIR:-$(cd "$SCRIPT_DIR/.." && pwd)}"
DEPLOY_USER="${DEPLOY_USER:-ubuntu}"
DEPLOY_GROUP="${DEPLOY_GROUP:-$DEPLOY_USER}"
DEPLOY_BRANCH="${DEPLOY_BRANCH:-main}"
DEPLOY_TARGET_SHA="${DEPLOY_TARGET_SHA:-}"
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

path_owner_group() {
    local path="$1"
    if stat -c '%U:%G' "$path" >/dev/null 2>&1; then
        stat -c '%U:%G' "$path"
        return 0
    fi
    stat -f '%Su:%Sg' "$path"
}

fail_app_dir_ownership() {
    local actual_owner_group="${1:-unknown}"
    printf '[deploy] ERROR:\n' >&2
    printf 'application directory ownership must be %s:%s\n\n' "$DEPLOY_USER" "$DEPLOY_GROUP" >&2
    printf 'Current:\n%s: %s\n\n' "$APP_DIR" "$actual_owner_group" >&2
    printf 'Fix:\nsudo chown -R %s:%s %s\n' "$DEPLOY_USER" "$DEPLOY_GROUP" "$APP_DIR" >&2
    exit 1
}

require_app_dir_ownership() {
    local actual_owner_group=""
    local expected_owner_group="$DEPLOY_USER:$DEPLOY_GROUP"
    actual_owner_group="$(path_owner_group "$APP_DIR")" || fail "could not inspect ownership for $APP_DIR"
    if [[ "$actual_owner_group" != "$expected_owner_group" ]]; then
        fail_app_dir_ownership "$actual_owner_group"
    fi
    log "$APP_DIR ownership is $expected_owner_group"
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
        DEPLOY_GROUP="$DEPLOY_GROUP" \
        DEPLOY_BRANCH="$DEPLOY_BRANCH" \
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

resolve_deploy_target_sha() {
    local target_sha="$DEPLOY_TARGET_SHA"

    run git fetch origin "+refs/heads/$DEPLOY_BRANCH:refs/remotes/origin/$DEPLOY_BRANCH"
    cleanup_stale_git_lock
    if [[ -z "$target_sha" ]]; then
        target_sha="$(git rev-parse "origin/$DEPLOY_BRANCH")" || fail "could not resolve origin/$DEPLOY_BRANCH"
    fi
    if [[ ! "$target_sha" =~ ^[0-9a-fA-F]{40,64}$ ]]; then
        fail "DEPLOY_TARGET_SHA must be a full commit SHA"
    fi
    if ! git cat-file -e "${target_sha}^{commit}" >/dev/null 2>&1; then
        fail "DEPLOY_TARGET_SHA does not resolve to a commit: $target_sha"
    fi
    if ! git merge-base --is-ancestor "$target_sha" "origin/$DEPLOY_BRANCH"; then
        fail "DEPLOY_TARGET_SHA is not reachable from origin/$DEPLOY_BRANCH: $target_sha"
    fi
    DEPLOY_TARGET_SHA="$(git rev-parse "${target_sha}^{commit}")"
    log "pinned deployment target: $DEPLOY_TARGET_SHA"
}

checkout_deploy_target() {
    local deployed_head=""

    run git reset --hard "$DEPLOY_TARGET_SHA"
    if [[ "$DRY_RUN" == "1" ]]; then
        return 0
    fi
    deployed_head="$(git rev-parse HEAD)" || fail "could not verify deployed HEAD"
    if [[ "$deployed_head" != "$DEPLOY_TARGET_SHA" ]]; then
        fail "deployed HEAD $deployed_head does not match target $DEPLOY_TARGET_SHA"
    fi
}

main() {
    require_deploy_user
    require_repo
    require_app_dir_ownership
    cleanup_stale_git_lock
    run_pre_deploy_check

    resolve_deploy_target_sha
    checkout_deploy_target

    load_environment
    run "$PIP_BIN" install -r requirements.txt
    run "$PYTHON_BIN" manage.py migrate
    run "$PYTHON_BIN" manage.py collectstatic --noinput
    run env APP_DIR="$APP_DIR" SERVICE_NAME="$SERVICE_NAME" "$APP_DIR/scripts/restart.sh"
    log "deployment finished at $DEPLOY_TARGET_SHA"
}

main "$@"
