# Asset 3 Production Activation v1

Date: 2026-09-04. Result: `ASSET_3_PRODUCTION_ACTIVATION_PARTIAL`.

This is an authorized operations record, not application-code development or a
new full-platform browser acceptance. This record was prepared in Draft PR #83
for Human review and may enter main as the reviewed partial-production-activation
record.

## Exact Deployment And Backup

| Item | Observed result |
| --- | --- |
| Live pre-deploy SHA | `38992e955f2f268c2a6d4ecd81d900feba83f048` |
| Reviewed target / live post-deploy SHA | `509ef74b76a4424f907bbf6152a824b5a9b2d633` |
| Target tree | `e83a89ac7cab33c236d2b448de7a5cd48576695e` |
| Target parent | `c9d9306f39c149baa8f76eae77267d80ea3fd670` |
| Production Git | `main`, clean; pre-deploy SHA is an ancestor of target |
| Target main CI | Django checks/tests, Node frontend and Nginx baseline successful; actual steps executed |
| Settings Guard | PR #82 exact Head successful; main push not triggered by design |
| PostgreSQL backup | Custom-format backup, 414529 bytes; `pg_restore --list` passed |
| Backup SHA-256 | `5de9069f6717dc8620db4c856fbc340f54d93d0db51b992856f26f0bcf67b996` |
| Backup controls | Root-owned 0700 directory; database/environment backups 0600; retained on server only |
| Deployment | Existing pre-deploy check and dry-run passed; existing exact-SHA deploy script succeeded |
| Migration | Existing migrations through 0024 retained; additive `0025_provider_daily_validation` applied |
| Static files / restart | Existing deploy script collected static files and restarted Web; later environment reload restarted Web |
| Application checks | `check` passed; migration dry-run reported no changes |
| `check --deploy` | Exit 0 with existing W004/W008/W012/W016 HTTPS/Cookie configuration warnings; not changed by this task |
| Health | Local and public live/ready, and public homepage, returned 200; Web active, NRestarts=0 |

The reviewed deployment span includes previously merged XHS and Provider Lab
application changes. This record does **not** claim that all behavior is
identical to the pre-deploy code. No unreviewed code, independent route override,
download policy change, application dependency declaration change, or production settings
patch was introduced during activation. The deployment span had no changes to
`settings.py`, requirements, deployment scripts or Nginx files; the only pending
database migration was 0025.

## Runtime And Selected Checks

The pinned videodl runtime was bootstrapped into a persistent directory outside
the application checkout and outside temporary storage. The deployment user owns
it; the Web service group can read/execute but cannot modify it; other users have
no access through its runtime directory.

```text
VIDEODL_PIN = 6cd1de0fe1209380b183b654c65e98a629637873
VIDEODL_PACKAGE = videofetch 0.9.1
VIDEODL_EXACT_PIN_CHECK = PASS
WEB_USER_WORKER_HANDSHAKE = PASS
UPSTREAM_PROVIDER_LAB_ENABLED = FALSE
VIDEODL_PROVIDER_LAB_ENABLED = TRUE
VIDEODL_SCOPE = HUMAN_AUTHORIZED_INTERNAL_NONCOMMERCIAL_VALIDATION
VIDEODL_SPECIFIC_BACKEND = NOT_SELECTED
VIDEODL_COMMON_DAILY_CHECK = NO
PROVIDER_DOWNLOADERS = NOT_CALLED
COOKIE_OR_PROXY_INHERITANCE = NO
FULL_MEDIA_DOWNLOAD = NO
```

The first operational Web-user handshake used an unsuitable working-directory
context. Retesting with the application working directory and a disposable,
Web-user-owned HOME/XDG context passed. No worker/application code was changed;
the real scheduled worker subsequently passed under systemd PrivateTmp.

The bootstrap's historical local-only license notice is not a general production
authorization. This activation relies on the task's explicit **internal
noncommercial** authorization and does not authorize commercial/public videodl
routes or opening Provider Lab.

Selected keys, in registry order:

1. `health_ready`.
2. `douyin_native_parse`, using the existing Human-configured public sample on
   `www.douyin.com`; execution-time public-URL/DNS admission passed.
3. `videodl_policy_inventory`, covering exact-pin inventory and denial policy,
   not invoking a dedicated/Common parsing backend.

Existing configured sample fields were preserved: `douyin_url`, `youtube_url`,
`third_party_url`, `gallerydl_url`. Their hosts are `www.douyin.com`,
`www.youtube.com` and `imgur.com`. Only the Douyin field was selected for initial
daily external parsing. No new sample URL was invented or saved. No TianTian or
KuKuTool Ultra quota-consuming check was selected.

## Execution Evidence

| Gate | Evidence |
| --- | --- |
| Initial settings | daily/email false; threshold 2; cooldown 24 hours; recovery true; retention 90 days; transaction + full_clean |
| Dry-run | Exit 0; Suite/child row delta 0/0; no external call, mail, or history deletion |
| No-email canary | `c9777e527f9d473a91b9b61b8bd65b2a`; command trigger; success 3, failed 0, skipped 0, unavailable 0; 2489 ms |
| Closed-gate service run | `e448be0abf30452b83549fbe6a163779`; scheduled/skipped; `DAILY_VALIDATION_DISABLED`; zero child Provider executions |
| First enabled scheduled Suite | `87ac0161d84f407f800a38f28ba02abf`; normal service command, no force/override/no-email flag |
| Scheduled result | success 3, failed 0, skipped 0, unavailable 0; finished in 2335 ms |
| Scheduled children | readiness 3 ms; Native Douyin 1151 ms; videodl inventory 1167 ms; all successful |
| Coordination | Single-flight released after each completed run; no stale Suite, Worker or daily-command residue |
| Persistence | Host-only child records; no full source/media URL or credential markers in inspected result fields |
| Reports | Selected keys, child statuses and 7/30-day aggregates available through ORM/report builder |
| Admin HTTP/browser | Unauthenticated HTTP returned 404 as expected; Chrome control timed out before browser acceptance |
| Notification | `not_needed` for successful Suites; both email gates disabled; no email attempted |

Asset 3 service and timer passed `systemd-analyze verify`; installed files are
byte-exact copies of the reviewed repository candidates. The timer was enabled
while daily was still false, before the final gate was opened. The first enabled
run used the normal scheduled command and exited with status 0.

```text
DAILY_ENABLED = TRUE
TIMER_ENABLED = TRUE
SERVER_TIMEZONE = Asia/Hong_Kong
CALENDAR = 03:30:00 daily, server timezone
RANDOMIZED_DELAY = at most 15 minutes
NEXT_RUN_OBSERVED_SERVER = 2026-09-05 03:44:16 +08:00
NEXT_RUN_OBSERVED_UTC = 2026-09-04 19:44:16 UTC
NEXT_RUN_OBSERVED_ASIA_SINGAPORE = 2026-09-05 03:44:16 +08:00
```

This next-run value is an observation, not a fixed future schedule. The server
timezone was not changed, and no cron or Codex automation was created.

## Partial Gates

- `PARTIAL_EMAIL_NOT_CONFIGURED`: no existing approved SMTP configuration or
  recipients. `EMAIL_DB_GATE=DISABLED`, `EMAIL_ENV_GATE=DISABLED`.
- `SMTP_CANARY_ACCEPTED=NOT_CONFIGURED`; inbox delivery was not verified. No
  address was inferred from an administrator account, history or browser data.
- `PARTIAL_ADMIN_BROWSER_SMOKE_NOT_AVAILABLE`: browser tooling timed out. No
  login session was fabricated, no Cookie was extracted, and no password was
  reset. ORM/HTTP evidence is not relabeled as a browser PASS.

Daily validation can safely continue with Admin-stored results but **without
email alerts**. Completing activation requires approved SMTP/from/recipients,
the same-path real email canary, and an authenticated Admin browser check.

Local record validation also passed: 68 daily-validation Django tests, 246 Node
tests, Django check, migration dry-run, whitespace checks and the three changed
documents' seven local links. No real Provider was called by automated tests.

The operator control connection ended during the original supplemental closing
checks. Those steps were initially left unverified, not labeled PASS. The
separately authorized closeout below completed them after reconnection, without
repeating deployment or a canary. No application failure or rollback was observed.

## Reconnection Closeout

Closeout verified on 2026-09-04, approximately 19:49 +08:00. A fresh connection
confirmed the deployment identity, clean production `main` and unchanged exact
target. Privileged access was reauthenticated rather than assumed from the old
session. Only read-only operational checks and scoped evidence-preserving cleanup
were performed; no application/configuration changes were made.

| Gate | Closeout observation |
| --- | --- |
| Environment | Owner/group and 0600 mode preserved; PostgreSQL and Redis configuration present |
| Activation environment comparison | Only the four previously authorized Asset 3 variable names differ from the pre-activation backup; no additional changes |
| Dependencies | `pip check` passed; installed `pip freeze` matches the activation backup; no installation performed |
| Daily budget | Safe default of 300 seconds active; no override introduced |
| Daily settings | Enabled; the same three keys; threshold 2, cooldown 24 hours, recovery true, retention 90 days |
| Email | Both gates disabled; SMTP host/credentials/from/recipients absent; no mail attempted |
| videodl | Persistent exact pin/package; deployment user owns it; Web identity can read/execute but not modify; other access denied |
| Scope | Provider Lab gate closed; no Common or specific backend daily check selected |
| Infrastructure | Pre-activation Nginx/Web hashes still match; installed validation service/timer remain repository byte-exact |
| Latest scheduled Suite | Still `87ac0161d84f407f800a38f28ba02abf`, success 3/0/0/0; three successful child rows; no new run triggered |
| Next automatic run | Not yet due; observed 2026-09-05 03:44:16 +08:00 in both server Asia/Hong_Kong and Asia/Singapore |
| Persistence/coordination | Host-only inspected child records; single-flight released; task-owned process residue 0 |
| Backup | Original database dump size and SHA-256 verified unchanged; environment backups and infrastructure manifest retained |
| Cleanup | 9 allowlisted temporary files removed, including the closeout helper; temporary directory removed |
| Evidence retention | Four temporary audit logs copied into restricted backup storage with byte-hash equality before temporary copies were removed |
| Unrelated temporary data | 31 other top-level temporary entries observed and left untouched; no global cleanup or process termination |
| Privilege closeout | `sudo -k` exited 0; no subsequent sudo command executed |
| Final health | Web active, NRestarts=0; local/public live and ready all 200; timer active/enabled; target Git still clean |

Production filesystem writes during closeout were limited to the temporary
read-only audit helper, preserving four audit-log copies in restricted backup
storage, and deleting the enumerated task temporary files. This is not described
as a zero-write session. No production code, environment, database settings,
service units, routes or download configuration were changed. No redeploy,
migration, static collection, restart, bootstrap, timer enablement or canary was
repeated. The administrator's existing session and SSH configuration were not
deleted or changed.

```text
SSH_CLOSEOUT = PASS
SUPPLEMENTAL_ENVIRONMENT_CHECK = PASS
DEPENDENCY_CHECK = PASS
OPERATIONAL_TEMP_CLEANUP = PASS
TASK_OWNED_TEMP_FILES_FOUND = 9
TASK_OWNED_TEMP_FILES_REMOVED = 9
UNKNOWN_TEMP_FILES_LEFT_UNTOUCHED = YES
SUDO_CREDENTIAL_CACHE_REVOCATION = PASS
LATEST_SCHEDULED_SUITE = SUCCESS_3_OF_3_EXISTING_EVIDENCE
POST_CLOSEOUT_WEB_HEALTH = PASS
POST_CLOSEOUT_TIMER = ACTIVE_AND_ENABLED
SECRET_LEAKAGE = NO
FULL_URL_LEAKAGE = NO
PRODUCTION_CONFIG_CHANGE_DURING_CLOSEOUT = NO
PRODUCTION_REDEPLOY = NO
MIGRATION_RERUN = NO
CANARY_RERUN = NO
```

Closeout record validation passed Django check, 68 focused daily-validation
tests, whitespace checks, local Markdown links and a sensitive-content scan.
PR #83 records the partial production activation for Human review.
Merging this documentation record does not redeploy or modify the production
runtime. The closeout does not convert the existing ORM/report evidence into
browser acceptance or change the outstanding email configuration gate.

## Boundaries And Rollback

- Pre/post Provider Runtime settings hashes matched. Existing sample fields
  were preserved; only reviewed daily-validation fields were changed.
- Nginx and Web-unit hashes/topology matched the pre-deploy manifest. Only the
  two approved daily-validation units were installed.
- Journal inspection found no source/media URL or credential disclosure.
  URL matches were listener addresses and site Referers on Service Worker
  script requests, not media downloads. No traceback/import/database error was
  found in the observed activation window.
- Provider Lab stays closed. No Common, legacy auto, grabber, browser observer,
  full media download, automatic routing or Provider disabling was introduced.
- PR #62 remains Open/Draft at its recorded exact Head and was not changed.
  No production checkpoint was moved.
- The environment and PostgreSQL backup, pre-deploy exact SHA, migrations,
  dependency list and infrastructure hashes remain in restricted server backup
  storage. They are not committed or uploaded.

Activation rollback first closes daily and both email gates, then disables the
timer, while retaining the additive schema and Suite/Run history. Application
failure rollback uses the recorded previous exact SHA, previous requirements,
static collection and Web restart, followed by health checks. No reverse
migration or history deletion is authorized by this record.

```text
FINAL_STATUS = ASSET_3_PRODUCTION_ACTIVATION_PARTIAL
ROLLBACK_READY = YES
FULL_COMPLETE = NO
RECORD_PR = REVIEWED_PARTIAL_ACTIVATION_RECORD
OPERATIONAL_TEMP_CLEANUP = PASS
SUDO_CREDENTIAL_CACHE_REVOCATION = PASS
FINAL_ACTIVATION_STATUS = PARTIAL_EMAIL_NOT_CONFIGURED
ADMIN_BROWSER_SMOKE = NOT_AVAILABLE_TOOLING_TIMEOUT
NEXT_PRODUCT_TASK = NOT_STARTED
```
