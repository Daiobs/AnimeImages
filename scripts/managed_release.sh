#!/usr/bin/env bash
set -Eeuo pipefail

export PATH=/usr/local/bin:/usr/bin:/bin
export GIT_NO_REPLACE_OBJECTS=1
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
APP_DIR=/srv/animeimages
STATE_DIR=/home/ubuntu/.local/state/animeimages-managed-release
LOCK_FD=""
CHILD_PID=""
APPLY_STARTED=0

fail() { printf '[managed-release] ERROR: %s\n' "$*" >&2; exit 1; }
finish() {
    local result=$?
    if [[ "$result" != 0 && "$APPLY_STARTED" == 1 ]]; then
        printf '[managed-release] FAILED after maintenance began. Do not assume service state or reverse migrations. Inspect deploy journal under %s and verify service state before approved recovery.\n' "$STATE_DIR" >&2
    fi
    exit "$result"
}
trap finish EXIT
on_signal() {
    local result="$1" attempt
    trap '' HUP INT TERM
    if [[ -n "$CHILD_PID" ]]; then
        kill -TERM -- "-$CHILD_PID" 2>/dev/null || true
        for ((attempt=0; attempt<20; attempt++)); do
            kill -0 -- "-$CHILD_PID" 2>/dev/null || break
            sleep 0.1
        done
        kill -KILL -- "-$CHILD_PID" 2>/dev/null || true
        wait "$CHILD_PID" 2>/dev/null || true
        CHILD_PID=""
    fi
    printf '[managed-release] interrupted; actual service state:\n' >&2
    /usr/bin/systemctl show animeimages -p ActiveState -p SubState -p MainPID >&2 || true
    exit "$result"
}
trap 'on_signal 129' HUP
trap 'on_signal 130' INT
trap 'on_signal 143' TERM

[[ "$(id -un)" == ubuntu && "$(id -u)" != 0 ]] || fail 'run as ubuntu, never root'
[[ "$#" == 2 || "$#" == 3 ]] || fail 'usage: managed_release.sh prepare|verify SHA, or apply SHA --maintenance-approved'
mode="$1"
target="$2"
[[ "$target" =~ ^[0-9a-f]{40}$ ]] || fail 'target must be an explicitly approved full SHA'
case "$mode:$#:${3:-}" in
    prepare:2:|verify:2:|apply:3:--maintenance-approved) ;;
    *) fail 'apply requires --maintenance-approved; prepare/verify accept no extra arguments' ;;
esac
tool_root="$(cd "$SCRIPT_DIR/.." && pwd -P)"
[[ "$tool_root" != "$APP_DIR" && "$tool_root/" != "$APP_DIR/"* ]] || fail 'use a separate pinned tooling checkout'
tool_sha="$(git -C "$tool_root" rev-parse HEAD)"
[[ -z "$(git -C "$tool_root" status --porcelain)" ]] || fail 'tooling checkout must be clean'
[[ -d "$APP_DIR/.git" && "$(git -C "$APP_DIR" branch --show-current)" == main ]] || fail 'application checkout must be on main'
[[ -z "$(git -C "$APP_DIR" status --porcelain)" ]] || fail 'application worktree must be clean'

verify() {
    [[ "$(git -C "$APP_DIR" rev-parse HEAD)" == "$target" ]] || fail 'live HEAD differs from approved target'
    [[ -z "$(git -C "$APP_DIR" status --porcelain)" ]] || fail 'live worktree is not clean'
    [[ "$(/usr/bin/systemctl show animeimages -p User --value)" == www-data &&
       "$(/usr/bin/systemctl show animeimages -p Group --value)" == www-data &&
       "$(/usr/bin/systemctl show animeimages -p ActiveState --value)" == active &&
       "$(/usr/bin/systemctl show animeimages -p SubState --value)" == running ]] || fail 'unexpected service identity or state'
    sudo -n /usr/bin/systemctl status animeimages --no-pager -l
    readiness local http://127.0.0.1:8000/health/ready/
    readiness public https://download.cosflow.icu/health/ready/
    printf '[managed-release] verified target=%s tool=%s\n' "$target" "$tool_sha"
}

readiness() {
    local label="$1" url="$2" code=""
    code="$(curl -q --proxy '' --noproxy '*' --max-redirs 0 --connect-timeout 5 --max-time 20 -fsS -o /dev/null -w '%{http_code}' "$url")" || fail "$label readiness request failed"
    printf '%s_ready=%s\n' "$label" "$code"
    [[ "$code" == 200 ]] || fail "$label readiness must return HTTP 200"
}

# Lock preparation/application together; do not hold a global umask over deploy.
(
    umask 077
    mkdir -p "$STATE_DIR"
)
[[ ! -L "$STATE_DIR" && "$(stat -c '%u:%a' "$STATE_DIR")" == "$(id -u):700" ]] || fail 'state directory must be ubuntu-owned 0700'
[[ ! -L "$STATE_DIR/channel.lock" ]] || fail 'lock must not be a symlink'
exec {LOCK_FD}>"$STATE_DIR/channel.lock"
flock -n "$LOCK_FD" || fail 'another managed release is running'
[[ "$mode" != verify ]] || { verify; exit 0; }

deploy() {
    local result=0
    # With job control disabled, setsid becomes this background PID's group.
    set +m
    setsid env -i HOME=/home/ubuntu USER=ubuntu LOGNAME=ubuntu PATH=/usr/local/bin:/usr/bin:/bin GIT_NO_REPLACE_OBJECTS=1 \
        APP_DIR="$APP_DIR" ENV_FILE="$APP_DIR/.env.production" PYTHON_BIN="$APP_DIR/.venv/bin/python" \
        SERVICE_NAME=animeimages DEPLOY_USER=ubuntu DEPLOY_GROUP=ubuntu DEPLOY_BRANCH=main \
        WEB_USER=www-data STATIC_USER=www-data STATIC_GROUP=www-data CODE_UMASK=0022 \
        DEPLOY_DRY_RUN=0 DEPLOY_PREPARE_ONLY="$1" DEPLOY_STATE_DIR="$STATE_DIR" \
        DEPLOY_STAGE_PARENT=/srv/animeimages-deploy-staging DEPLOY_CONSUMER_PROBE_MODE=managed \
        DEPLOY_TOOL_SHA="$tool_sha" DEPLOY_TARGET_SHA="$target" bash "$SCRIPT_DIR/deploy.sh" &
    CHILD_PID=$!
    wait "$CHILD_PID" || result=$?
    CHILD_PID=""
    return "$result"
}

deploy 1
[[ "$mode" == apply ]] || exit 0
for command in 'daemon-reload' 'restart animeimages' 'status animeimages --no-pager -l' 'stop animeimages'; do
    read -r -a arguments <<< "$command"
    sudo -n -l -- /usr/bin/systemctl "${arguments[@]}" >/dev/null || fail "missing exact service permission: $command"
done
readiness local http://127.0.0.1:8000/health/ready/
readiness public https://download.cosflow.icu/health/ready/
APPLY_STARTED=1
sudo -n /usr/bin/systemctl stop animeimages
[[ "$(/usr/bin/systemctl show animeimages -p ActiveState --value)" == inactive &&
   "$(/usr/bin/systemctl show animeimages -p MainPID --value)" == 0 ]] || fail 'service did not reach inactive/PID=0'
deploy 0
verify
