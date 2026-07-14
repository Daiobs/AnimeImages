#!/usr/bin/env bash
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "${script_dir}/.." && pwd)"
template_dir="${repo_root}/ops/release-executor"
helper="${template_dir}/animeimages-release-submit"
unit="${template_dir}/animeimages-deploy@.service"
sudoers="${template_dir}/animeimages-release.sudoers"
readme="${template_dir}/README.md"

pass() {
  echo "[release-executor-check] PASS: $1"
}

skip() {
  echo "[release-executor-check] SKIPPED: $1"
}

fail() {
  echo "[release-executor-check] ERROR: $1" >&2
  exit 1
}

require_file() {
  [[ -f "$1" ]] || fail "missing template: $1"
}

require_exact_line() {
  local file="$1"
  local expected="$2"
  grep -Fqx -- "$expected" "$file" || fail "missing required line in ${file}: ${expected}"
}

expect_helper_rejects() {
  local description="$1"
  shift
  local status=0
  "$helper" "$@" >/dev/null 2>&1 || status=$?
  [[ "$status" -ne 0 ]] || fail "helper accepted ${description}"
}

for file in "$helper" "$unit" "$sudoers" "$readme"; do
  require_file "$file"
done
pass "required templates are present"

expected_helper="$(cat <<'EOF'
#!/bin/bash
set -euo pipefail

if [[ "$#" -ne 1 ]]; then
  echo "usage: animeimages-release-submit <numeric-job-id>" >&2
  exit 64
fi

job_id="$1"
if [[ ! "$job_id" =~ ^[1-9][0-9]{0,18}$ ]]; then
  echo "job ID must be a positive integer" >&2
  exit 64
fi

exec /bin/systemctl start --no-block "animeimages-deploy@${job_id}.service"
EOF
)"
expected_unit="$(cat <<'EOF'
[Unit]
Description=AnimeImages release deployment job %i
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=ubuntu
Group=ubuntu
WorkingDirectory=/srv/animeimages
EnvironmentFile=/srv/animeimages/.env.production
ExecStart=/srv/animeimages/.venv/bin/python /srv/animeimages/manage.py run_release_deploy_job --job-id %i
TimeoutStartSec=20min
UMask=0022
KillMode=control-group
Restart=no
PrivateTmp=true
EOF
)"
expected_sudoers="$(cat <<'EOF'
# Install as /etc/sudoers.d/animeimages-release with root:root ownership and mode 0440.
# The helper is the only privileged entry point; it validates exactly one numeric job ID.
Cmnd_Alias ANIMEIMAGES_RELEASE_SUBMIT = /usr/local/sbin/animeimages-release-submit *
www-data ALL=(root) NOPASSWD: NOSETENV: ANIMEIMAGES_RELEASE_SUBMIT
EOF
)"

[[ "$(cat "$helper")" == "$expected_helper" ]] || fail "helper contains an unapproved or missing statement"
[[ "$(cat "$unit")" == "$expected_unit" ]] || fail "systemd template contains an unapproved or missing directive"
[[ "$(cat "$sudoers")" == "$expected_sudoers" ]] || fail "sudoers template contains an unapproved or missing rule"
pass "privileged templates match the complete allowlist"

bash -n "$helper"
require_exact_line "$helper" 'set -euo pipefail'
require_exact_line "$helper" '#!/bin/bash'
require_exact_line "$helper" 'if [[ "$#" -ne 1 ]]; then'
require_exact_line "$helper" 'if [[ ! "$job_id" =~ ^[1-9][0-9]{0,18}$ ]]; then'
require_exact_line "$helper" 'exec /bin/systemctl start --no-block "animeimages-deploy@${job_id}.service"'
[[ "$(grep -Fc -- '/bin/systemctl' "$helper")" -eq 1 ]] || fail "helper must contain one fixed systemctl call"
expect_helper_rejects "no arguments"
expect_helper_rejects "zero job ID" 0
expect_helper_rejects "leading-zero job ID" 01
expect_helper_rejects "non-numeric job ID" invalid
expect_helper_rejects "overlong job ID" 12345678901234567890
expect_helper_rejects "extra arguments" 1 extra
pass "helper syntax and rejection paths are safe"

require_exact_line "$unit" 'Type=oneshot'
require_exact_line "$unit" 'User=ubuntu'
require_exact_line "$unit" 'Group=ubuntu'
require_exact_line "$unit" 'WorkingDirectory=/srv/animeimages'
require_exact_line "$unit" 'EnvironmentFile=/srv/animeimages/.env.production'
require_exact_line "$unit" 'ExecStart=/srv/animeimages/.venv/bin/python /srv/animeimages/manage.py run_release_deploy_job --job-id %i'
if grep -Eq '^[[:space:]]*\[Install\][[:space:]]*$' "$unit"; then
  fail "on-demand one-shot template must not contain an Install section"
fi
pass "systemd template uses the approved runtime identity and command"

active_sudoers="$(grep -Ev '^[[:space:]]*(#|$)' "$sudoers")"
[[ "$(printf '%s\n' "$active_sudoers" | wc -l | tr -d ' ')" -eq 2 ]] || fail "sudoers template must contain exactly two active lines"
printf '%s\n' "$active_sudoers" | grep -Fqx -- 'Cmnd_Alias ANIMEIMAGES_RELEASE_SUBMIT = /usr/local/sbin/animeimages-release-submit *' || fail "sudoers command alias is not restricted to the helper"
printf '%s\n' "$active_sudoers" | grep -Fqx -- 'www-data ALL=(root) NOPASSWD: NOSETENV: ANIMEIMAGES_RELEASE_SUBMIT' || fail "sudoers principal is not restricted to www-data and the helper alias"
if printf '%s\n' "$active_sudoers" | grep -Eq -- '(/bin/(ba)?sh|deploy\.sh|/bin/systemctl|/usr/bin/systemctl|/bin/git|/usr/bin/git)'; then
  fail "sudoers template grants a forbidden direct command"
fi
pass "sudoers template exposes only the fixed helper"

require_exact_line "$readme" 'sudo install -o root -g root -m 0755 animeimages-release-submit /usr/local/sbin/animeimages-release-submit'
require_exact_line "$readme" 'sudo install -o root -g root -m 0644 animeimages-deploy@.service /etc/systemd/system/animeimages-deploy@.service'
require_exact_line "$readme" 'sudo install -o root -g root -m 0440 animeimages-release.sudoers /etc/sudoers.d/animeimages-release'
pass "production target paths and ownership instructions are documented"

if command -v shellcheck >/dev/null 2>&1; then
  shellcheck "$helper" "$0"
  pass "shellcheck"
else
  skip "shellcheck is not installed"
fi

if command -v systemd-analyze >/dev/null 2>&1 \
  && [[ -x /srv/animeimages/.venv/bin/python ]] \
  && [[ -f /srv/animeimages/manage.py ]]; then
  systemd-analyze verify "$unit"
  pass "systemd-analyze verify"
elif command -v systemd-analyze >/dev/null 2>&1; then
  skip "systemd-analyze is available, but production target paths are not installed"
else
  skip "systemd-analyze is not available on this platform"
fi

if command -v visudo >/dev/null 2>&1; then
  visudo -cf "$sudoers"
  pass "visudo syntax check"
else
  skip "visudo is not available on this platform"
fi

echo "[release-executor-check] complete: repository templates validated; nothing was installed or started"
