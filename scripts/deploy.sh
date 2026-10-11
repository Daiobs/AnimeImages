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
DRY_RUN="${DEPLOY_DRY_RUN:-0}"
DEPLOY_TOOL_SHA="${DEPLOY_TOOL_SHA:-}"
CODE_UMASK="${CODE_UMASK:-0022}"
WEB_USER="${WEB_USER:-}"
STATIC_USER="${STATIC_USER:-}"
STATIC_GROUP="${STATIC_GROUP:-}"
DEPLOY_STATE_DIR="${DEPLOY_STATE_DIR:-}"
DEPLOY_STAGE_PARENT="${DEPLOY_STAGE_PARENT:-/tmp}"
DEPLOY_PREPARE_ONLY="${DEPLOY_PREPARE_ONLY:-0}"
DEPLOY_CONSUMER_PROBE_MODE="${DEPLOY_CONSUMER_PROBE_MODE:-direct}"
if [[ "$DEPLOY_CONSUMER_PROBE_MODE" == managed ]]; then export GIT_NO_REPLACE_OBJECTS=1; fi
STAGE=""
JOURNAL=""
LIVE_CHANGED=0
RESTART_ATTEMPTED=0
PREVIOUS_SHA=""

finish() {
    local result=$?
    trap - EXIT
    if [[ "$result" != 0 && "$LIVE_CHANGED" == 1 ]]; then
        if [[ "$RESTART_ATTEMPTED" == 1 ]]; then
            printf '[deploy] Restart was attempted; service state is not assumed stopped. Obtain an approved stop and verify inactive/PID=0 before restoring files.\n' >&2
        else
            printf '[deploy] STOPPED after live update; no restart attempted. Verify maintenance remains in effect before restoring files.\n' >&2
        fi
        printf '[deploy] Restore verified code/static backup for %s; keep database migrations/data. Recovery record: %s\n' "$PREVIOUS_SHA" "$JOURNAL" >&2
    fi
    if [[ -n "$STAGE" ]]; then rm -rf -- "$STAGE"; fi
    exit "$result"
}
trap finish EXIT

# Private preparation never changes the mask inherited by code subprocesses.
private_record() (
    umask 077
    mkdir "$JOURNAL"
    printf 'tool=%s\ntarget=%s\nprevious=%s\n' "$DEPLOY_TOOL_SHA" "$DEPLOY_TARGET_SHA" "$PREVIOUS_SHA" > "$JOURNAL/recovery.txt"
    # Preserve current tracked bytes and modes, not a re-created Git archive.
    # Database, untracked configuration and collected static backups stay separate.
    git ls-tree -r --name-only -z HEAD | tar --create --gzip \
        --file "$JOURNAL/code-before.tar.gz" --null --verbatim-files-from --files-from=-
)

code_run() (
    umask "$CODE_UMASK"
    "$@"
)

require_tool_and_identities() {
    local tool_root actual user uid
    tool_root="$(cd "$SCRIPT_DIR/.." && pwd -P)"
    APP_DIR="$(cd "$APP_DIR" && pwd -P)"
    [[ "$tool_root" != "$APP_DIR" && "$tool_root/" != "$APP_DIR/"* ]] || fail "launch reviewed tooling from a separate checkout outside APP_DIR"
    [[ "$DEPLOY_TOOL_SHA" =~ ^[0-9a-f]{40}$ ]] || fail "DEPLOY_TOOL_SHA must be a full reviewed commit"
    actual="$(git -C "$tool_root" rev-parse HEAD)"
    [[ "$actual" == "$DEPLOY_TOOL_SHA" ]] || fail "tooling HEAD differs from DEPLOY_TOOL_SHA"
    [[ -z "$(git -C "$tool_root" status --porcelain)" ]] || fail "deployment tool checkout has uncommitted changes"
    for user in "$WEB_USER" "$STATIC_USER"; do
        [[ -n "$user" ]] || fail "WEB_USER and STATIC_USER must identify actual consumers"
        uid="$(id -u "$user")"
        [[ "$uid" != 0 && "$uid" != "$(id -u)" ]] || fail "consumer must be non-root and distinct from deploy user"
    done
    [[ "$(systemctl show "$SERVICE_NAME" -p User --value)" == "$WEB_USER" ]] || fail "WEB_USER differs from service User"
    WEB_GROUP="$(systemctl show "$SERVICE_NAME" -p Group --value)"
    WEB_GROUP="${WEB_GROUP:-$(id -gn "$WEB_USER")}"
    STATIC_GROUP="${STATIC_GROUP:-$(id -gn "$STATIC_USER")}"
    [[ "$CODE_UMASK" == 0022 || "$CODE_UMASK" == 0027 ]] || fail "CODE_UMASK must be 0022 or 0027"
    [[ -d "$DEPLOY_STATE_DIR" && ! -L "$DEPLOY_STATE_DIR" ]] || fail "DEPLOY_STATE_DIR must be an existing private directory"
    [[ "$(stat -c '%u:%a' "$DEPLOY_STATE_DIR")" == "$(id -u):700" ]] || fail "DEPLOY_STATE_DIR must be deploy-owned mode 0700"
    RUNTIME_ROOT="$("$PYTHON_BIN" -I -c 'import sys; print(sys.prefix)')"
    require_consumer_probe_mode "$tool_root"
    log "release tool=$DEPLOY_TOOL_SHA; code mask=$CODE_UMASK; web=$WEB_USER; static=$STATIC_USER"
}

require_consumer_probe_mode() {
    local tool_root="$1" path owner permissions expected installed
    [[ "$DEPLOY_CONSUMER_PROBE_MODE" == direct || "$DEPLOY_CONSUMER_PROBE_MODE" == managed ]] || fail "unknown DEPLOY_CONSUMER_PROBE_MODE"
    [[ "$DEPLOY_CONSUMER_PROBE_MODE" == managed ]] || return 0
    [[ "$APP_DIR" == /srv/animeimages && "$ENV_FILE" == /srv/animeimages/.env.production &&
       "$PYTHON_BIN" == /srv/animeimages/.venv/bin/python && "$RUNTIME_ROOT" == /srv/animeimages/.venv &&
       "$DEPLOY_USER:$DEPLOY_GROUP:$DEPLOY_BRANCH:$SERVICE_NAME" == ubuntu:ubuntu:main:animeimages &&
       "$WEB_USER:$WEB_GROUP:$STATIC_USER:$STATIC_GROUP" == www-data:www-data:www-data:www-data &&
       "$DEPLOY_STAGE_PARENT" == /srv/animeimages-deploy-staging && "$CODE_UMASK" == 0022 ]] || fail "managed consumer probes require the fixed production profile"
    for path in /usr /usr/local /usr/local/sbin /usr/local/libexec /usr/local/libexec/animeimages-release \
        /usr/local/sbin/animeimages-consumer-probe /usr/local/libexec/animeimages-release/check_deploy_readability.py; do
        [[ -e "$path" && ! -L "$path" ]] || fail "managed probe installation missing or symlinked: $path"
        read -r owner permissions < <(stat -c '%u %a' -- "$path")
        [[ "$owner" == 0 && "$permissions" =~ ^[0-7]{3,4}$ ]] || fail "managed probe installation must be root-owned: $path"
        (( (8#$permissions & 0022) == 0 )) || fail "managed probe installation is writable by non-root: $path"
    done
    for path in animeimages-consumer-probe check_deploy_readability.py; do
        if [[ "$path" == animeimages-consumer-probe ]]; then
            expected="$tool_root/ops/managed-release/$path"
            installed="/usr/local/sbin/$path"
        else
            expected="$SCRIPT_DIR/$path"
            installed="/usr/local/libexec/animeimages-release/$path"
        fi
        cmp -s -- "$expected" "$installed" || fail "installed managed probe differs from reviewed tooling: $path"
    done
    # Loading production settings must not silently switch the reviewed channel.
    readonly DEPLOY_CONSUMER_PROBE_MODE APP_DIR ENV_FILE PYTHON_BIN RUNTIME_ROOT SCRIPT_DIR
    readonly DEPLOY_USER DEPLOY_GROUP DEPLOY_BRANCH SERVICE_NAME WEB_USER WEB_GROUP STATIC_USER STATIC_GROUP
    readonly DEPLOY_STAGE_PARENT CODE_UMASK DEPLOY_TOOL_SHA DEPLOY_STATE_DIR DEPLOY_PREPARE_ONLY DRY_RUN
    readonly GIT_NO_REPLACE_OBJECTS
}

probe_code() {
    local root="$1" sha="$2"
    if [[ "$DEPLOY_CONSUMER_PROBE_MODE" == managed ]]; then
        git ls-tree -r --name-only -z "$sha" | sudo -n -u www-data -g www-data -- \
            /usr/local/sbin/animeimages-consumer-probe code "$root"
        return
    fi
    git ls-tree -r --name-only -z "$sha" | sudo -n -u "$WEB_USER" -g "$WEB_GROUP" -- \
        env -i PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
        "$PYTHON_BIN" -I -B "$SCRIPT_DIR/check_deploy_readability.py" \
        --expected-uid "$(id -u "$WEB_USER")" --root "$root" --runtime-root "$RUNTIME_ROOT" --paths-stdin
}

prepare_candidate() {
    STAGE="$(mktemp -d "$DEPLOY_STAGE_PARENT/animeimages-candidate.XXXXXXXX")"
    # Only this newly created directory is changed; existing artifacts are never repaired.
    chgrp "$DEPLOY_GROUP" "$STAGE"
    if [[ "$CODE_UMASK" == 0027 ]]; then chmod 2750 "$STAGE"; else chmod 2755 "$STAGE"; fi
    (umask "$CODE_UMASK"; git archive "$DEPLOY_TARGET_SHA" | tar -x -C "$STAGE")
    probe_code "$STAGE" "$DEPLOY_TARGET_SHA"
    probe_static_root "$STAGE/static"
    probe_code "$APP_DIR" HEAD
}

require_maintenance() {
    [[ "$(systemctl show "$SERVICE_NAME" -p ActiveState --value)" == inactive ]] || fail "service must already be stopped in the approved maintenance window"
    [[ "$(systemctl show "$SERVICE_NAME" -p MainPID --value)" == 0 ]] || fail "service still has a main process"
}

probe_static_root() {
    local root="$1"
    [[ -n "$root" && "$root" != / ]] || fail "invalid STATIC_ROOT"
    if [[ "$DEPLOY_CONSUMER_PROBE_MODE" == managed ]]; then
        sudo -n -u www-data -g www-data -- /usr/local/sbin/animeimages-consumer-probe static "$root"
        return
    fi
    sudo -n -u "$STATIC_USER" -g "$STATIC_GROUP" -- env -i PATH=/usr/local/bin:/usr/bin:/bin PYTHONDONTWRITEBYTECODE=1 \
        "$PYTHON_BIN" -I -B "$SCRIPT_DIR/check_deploy_readability.py" \
        --expected-uid "$(id -u "$STATIC_USER")" --root "$root" --static
}

probe_static() {
    local root
    root="$("$PYTHON_BIN" -B -c 'import os; os.environ.setdefault("DJANGO_SETTINGS_MODULE", "djangoProject.settings"); from django.conf import settings; print(settings.STATIC_ROOT)')"
    probe_static_root "$root"
}

log() {
    printf '[deploy] %s\n' "$*"
    if [[ -n "$JOURNAL" && -d "$JOURNAL" ]]; then
        (umask 077; printf '%s %s\n' "$(date -u +%FT%TZ)" "$*" >> "$JOURNAL/phases.log")
    fi
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
    printf 'Inspect and repair only explicitly identified ownership errors; do not recursively change secrets or runtime data.\n' >&2
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
    log "running reviewed pre-deploy checks"
    env \
        ENV_FILE="$ENV_FILE" \
        APP_DIR="$APP_DIR" \
        DEPLOY_USER="$DEPLOY_USER" \
        DEPLOY_GROUP="$DEPLOY_GROUP" \
        DEPLOY_BRANCH="$DEPLOY_BRANCH" \
        bash "$SCRIPT_DIR/pre_deploy_check.sh"
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
    [[ -n "$target_sha" ]] || fail "DEPLOY_TARGET_SHA must be explicitly pinned"
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
    if [[ "$DEPLOY_CONSUMER_PROBE_MODE" == managed ]]; then readonly DEPLOY_TARGET_SHA; fi
    log "pinned deployment target: $DEPLOY_TARGET_SHA"
}

checkout_deploy_target() {
    local deployed_head=""

    LIVE_CHANGED=1
    code_run git reset --hard "$DEPLOY_TARGET_SHA"
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
    [[ "$DRY_RUN" == 0 ]] || fail "use DEPLOY_PREPARE_ONLY=1 for a real read-only candidate gate instead of DEPLOY_DRY_RUN"
    [[ "$DEPLOY_PREPARE_ONLY" == 0 || "$DEPLOY_PREPARE_ONLY" == 1 ]] || fail "DEPLOY_PREPARE_ONLY must be 0 or 1"
    require_tool_and_identities
    cleanup_stale_git_lock
    run_pre_deploy_check

    resolve_deploy_target_sha
    PREVIOUS_SHA="$(git rev-parse HEAD)"
    JOURNAL="$DEPLOY_STATE_DIR/release-$(date -u +%Y%m%dT%H%M%SZ)-$$"
    private_record
    log "checking isolated candidate and existing live paths"
    prepare_candidate
    if [[ "$DEPLOY_PREPARE_ONLY" == 1 ]]; then
        log "candidate and existing-path gates passed; live files and service untouched"
        return
    fi
    require_maintenance
    log "maintenance verified; applying pinned code"
    checkout_deploy_target
    probe_code "$APP_DIR" "$DEPLOY_TARGET_SHA"

    load_environment
    log "installing target requirements under code permission scope"
    code_run "$PYTHON_BIN" -m pip install -r requirements.txt
    probe_code "$APP_DIR" "$DEPLOY_TARGET_SHA"
    log "applying migrations; database reversal is not automatic"
    code_run "$PYTHON_BIN" manage.py migrate
    log "collecting and checking public static artifacts"
    code_run "$PYTHON_BIN" manage.py collectstatic --noinput
    probe_static
    probe_code "$APP_DIR" "$DEPLOY_TARGET_SHA"
    require_maintenance
    RESTART_ATTEMPTED=1
    run env APP_DIR="$APP_DIR" SERVICE_NAME="$SERVICE_NAME" bash "$SCRIPT_DIR/restart.sh"
    log "deployment finished at $DEPLOY_TARGET_SHA"
}

main "$@"
