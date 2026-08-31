# Codex Handoff

Updated: 2026-08-29

This is the short handoff for a new AnimeImages Codex task. Read [Current Version Status](current-version-status.md) for deployment/history detail and [Architecture](architecture.md) for stable runtime structure.

## Baseline

```text
current development baseline = LATEST_FETCHED_ORIGIN_MAIN
documented application baseline = 457139b98d67d6e5f79b099f9a4e91d60a26904f
last recorded production runtime = 38992e955f2f268c2a6d4ecd81d900feba83f048
repository main ahead of recorded production = YES
ahead type = APPLICATION_CODE_AND_DOCUMENTATION
production reverified by documentation task = NO
```

The hard-coded application SHA is a documentation snapshot, not a permanent
`main` pointer. Every new task must fetch and resolve `origin/main` again. Do
not infer deployment from `main`. PR #74, #75 and #76 are merged development
capabilities and are not recorded as deployed by this documentation task.

## First Commands

Run these before editing:

```bash
pwd
git status --short --branch
git fetch origin --prune
git rev-parse HEAD
git rev-parse origin/main
git worktree list
gh pr list --state open
```

Confirm the assigned worktree, current branch, expected exact SHA, remote alignment and clean status. Unknown local or remote changes are a blocker, not permission to reset or force-push.

## Worktree Rules

- Use the existing long-lived development worktree assigned by the task.
- Start a new task branch from the latest fetched `origin/main` unless the task names an existing branch/PR.
- Use one task, one branch and one PR.
- Do not create another worktree when the task explicitly says to continue the current one.
- Do not use a production checkout as a development baseline.
- Never reset, clean or delete files that may belong to the user or another task.
- Preserve unrelated dirty changes and work with concurrent updates.
- Delete branches/worktrees only after exact merge/tree/cleanup gates authorize it.

Production deploy, service restart, Nginx reload, migration, checkpoint movement and runtime setting changes always require separate explicit authorization.

## Repository Shape

AnimeImages is a Django monolith with platform adapters, fixed Provider routes, a browser-owned ordinary media data plane and Staff operations surfaces.

Primary pages:

| Path | Purpose |
| --- | --- |
| `/` | Single parse/download |
| `/xhs/` | XHS entry |
| `/tiktok/` | Douyin entry |
| `/bilibili/` | Bilibili video entry |
| `/batch/` | Progressive Batch |
| `/library/` | Local image library |
| `/feedback/` | User feedback |
| `/admin/` | Staff control plane |
| `/admin/provider-lab/` | Staff-only Provider Lab |

The old Public Submissions surface is retired. Its historical model/migrations remain. Current `UserFeedback` is a separate protected workflow.

## Platform Routes

### Xiaohongshu

Ordinary video:

```text
Native exact identity/title/author/cover/filename
  -> TianTian high-quality video when Ultra is enabled
  -> Native video fallback
```

Images:

```text
Native exact note identity and source order
  -> ParseHub raw original candidate
  -> Native exact same-item fallback
```

Rules:

- Images and Live Photo make zero TianTian calls.
- ParseHub cannot replace Native identity/order.
- Preview is separate from original download authority.
- Admitted originals preserve JPEG/PNG/GIF/WebP/AVIF/HEIC.
- ParseHub disabled/unavailable/rejected means exact same-item Native fallback.

Live Photo:

- Anonymous Single: flat static images only.
- Active Staff Single: badge, one-shot playback, ended restore, replay, still + motion archive.
- Batch: flat static images only; no motion, playback, pair token or pair ZIP.
- Archive is original still + motion MP4, not automatic iOS Photos import.

### Douyin

Normal uses Native baseline.

Single Ultra:

```text
TianTian preferred -> KuKuTool fallback/race reserve -> Native fallback
```

Batch Ultra:

```text
TianTian -> Native fallback
```

Short links, canonical links, modal links and share text resolve to canonical content identity before Provider work. Keep existing Provider deadlines, request attempt accounting, continuation and source-bound redirect policy unless a task explicitly changes them.

### Bilibili

```text
scope = VIDEO_ONLY
formal route = NATIVE
```

Supported current forms include BV, AV, `b23.tv` and `p=` part. Native uses official View/PlayURL and prioritizes complete DURL media. It reads the title, UP owner name and cover from official View data; title and owner name feed the download filename semantics, while the cover enters the media result. Independent structured `ParseResult.metadata.author` is not yet a stable contract.

Provider Lab compared three real samples: Native, TianTian, videodl and official DURL bytes were equivalent; ParseHub added metadata only; yt-dlp failed as a known Provider-specific limitation. No quality improvement was proven, so do not start a Bilibili route change from PR #76.

Dynamic, Opus, Live Photo, member quality and DASH merge are deferred independent scopes.

### YouTube And Long Tail

yt-dlp and gallery-dl are optional generic capabilities behind capability/runtime/platform/safety gates. They are not blanket stable support.

YouTube video/audio merge is a server-generated output class. It must remain separate from ordinary Provider/CDN browser streaming.

## Download Contract

Ordinary video:

```text
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk
```

Images and static archive:

```text
Image CDN -> Browser image/archive implementation -> Disk
```

Staff Live Photo archive:

```text
Still CDN + Motion CDN -> Browser archive -> Disk
```

AnimeImages is the control/validation/descriptor authority. Ordinary media must not silently fall back to Server Relay. Server-generated merge/archive outputs are separate resource types and may use bounded server processing.

## Batch Contract

- Progressive item results; no all-at-once wait.
- `success`, `failed`, `timeout` and `skipped` remain distinct.
- One timeout does not block later links.
- Retry replaces only one card and uses current saved preferences.
- Response deadline matches the displayed budget and has a fail-safe boundary.
- Douyin Batch Provider order remains TianTian -> Native.
- XHS Batch remains flat static for Live Photo notes.

Do not change Provider order, timeout, completion grace or progressive token as incidental cleanup.

## Provider Gate Model

Formal Provider routes use separate capability, runtime, platform, safety and rollout gates. Daily exposure lives in `ProviderRuntimeSetting` and `/admin/provider-settings/`.

Provider Lab uses a different Staff-only gate:

```text
UPSTREAM_PROVIDER_LAB_ENABLED
VIDEODL_PROVIDER_LAB_ENABLED
active Staff
CSRF
rate limit
single-flight
```

Lab has no anonymous `public_gray/public` release mode and cannot be exposed by Admin Provider Settings.

## Provider Lab

Provider Lab compares fixed Providers without changing platform routes:

- `animeimages_current`
- `parsehub_repo`
- `videodl_repo`
- `yt_dlp_existing`
- `tiantian_existing`

Browser concurrency max is three. Each Provider uses a separate POST; Django does not fan out automatically. Existing wrappers run through current integrations, while ParseHub and videodl use isolated exact-pin whole-repository Workers.

```text
ParseHub pin = 144260f4349c5bb57415f7555a572fcd48c3b8ba
videodl pin = 6cd1de0fe1209380b183b654c65e98a629637873
```

videodl is PolyForm Noncommercial 1.0.0 and is limited to noncommercial local Staff Lab use. It is not a production candidate.

Provider Lab defaults off, does not persist results, does not choose a winner and does not perform full-media Relay. Stable operations live in [Provider Lab](provider-lab.md); research history lives in [Provider Lab research](research/provider_lab/01_UPSTREAM_PROVIDER_LAB_V1.md).

videodl controlled execution is Provider-Lab-local. Use only fixed inventory
IDs and the closed modes documented in
[Asset 1/2 outcome](research/videodl/ASSET_1_2_IMPLEMENTATION_OUTCOME_V1.md).
Do not reintroduce hidden common/grabber fallback, arbitrary import paths,
client-controlled timeouts, or global observation/catalog abstractions.
Asset 3 (daily production validation, Staff reporting and notification) is
deferred, not cancelled, and requires a separate authorization.

ParseHub Provider Lab Worker and ParseHub XHS Runtime are separate capabilities. Never reuse one flag or venv path to imply the other is enabled.

## Recent PR Timeline

| PR | State | Development effect |
| --- | --- | --- |
| #65 | Merged and last recorded production runtime | Douyin reviewed final redirect policy and terminal download failure UX |
| #66 | Closed, not merged | Superseded research path; not a current capability |
| #67-#72 | Merged | XHS Live Photo evidence, mapping, transport and Asset Spec foundations |
| #73 | Merged | Active Staff XHS Live Photo one-shot preview |
| #74 | Merged | XHS TianTian ordinary-video Hybrid |
| #75 | Merged | ParseHub raw originals, shared preview and Staff Live Photo archive |
| #76 | Merged | Staff-only Upstream Provider Lab |
| #62 | OPEN / Draft / deferred | External tool progress materials; not in main |

Do not continue modifying PR #76. The next stage is documentation and separately scoped platform research or deployment work.

## Admin Pages

- `/admin/provider-settings/`: formal Provider exposure.
- `/admin/provider-health-tests/`: Provider readiness/smoke.
- `/admin/provider-audit-logs/`: runtime setting audit.
- `/admin/ops-dashboard/`: observability.
- `/admin/xhs_download/batchparseruntimesetting/`: Django Admin `BatchParseRuntimeSetting` access and budgets.
- `/admin/provider-lab/`: isolated Staff comparison.

Public pages must not expose Provider names, raw media URLs, strategy matrices, Staff diagnostics or Lab controls.

## Local Verification

Use the project virtualenv selected for the worktree. Unset production database variables for local SQLite gates when the task requires it.

```bash
python manage.py check
python manage.py makemigrations --check --dry-run
python manage.py test xhs_download --noinput
node --test tests_js/*.test.js
node --check static/js/*.js
git diff --check
```

Do not call real Providers from automated tests. Real sample work must use a separately authorized, sanitized browser/research gate.

## Before A PR

1. Fetch and confirm exact base/head.
2. Confirm clean worktree and no unrelated changes.
3. Review full diff scope.
4. Run focused and full gates proportional to risk.
5. Run secret/URL/privacy scans for Provider work.
6. Update stable docs when behavior changes.
7. Create Draft PR first.
8. Wait for exact-head CI with real executed steps.
9. Keep Human Browser gates distinct from automated tests.
10. Do not Ready, merge or deploy unless the current task explicitly authorizes each action.

PR descriptions should separate:

- Summary / Why
- Design / Implementation
- Scope
- Safety
- Verification
- Runtime Behavior Changes
- Deployment Notes
- Rollback
- Non-goals

Never include real sample URLs, media URLs, signed queries, Cookie, credentials, user input or local personal paths.

## Known Pitfalls

- Treating merged main as production.
- Treating a Provider Lab success as formal route readiness.
- Treating metadata-only ParseHub output as downloadable Bilibili media.
- Treating TianTian or videodl equivalence as a Bilibili quality upgrade.
- Enabling Staff Live Photo for Anonymous or Batch.
- Replacing XHS Native item order with Provider array order.
- Mixing ParseHub XHS Runtime and Provider Lab Worker configuration.
- Installing Provider-specific venvs into the application `.venv`.
- Using Browser Relay/Server Relay terminology interchangeably.
- Logging signed URLs or user inputs while diagnosing failures.
- Modifying PR #62 as incidental cleanup.

## Production Boundary

The last recorded runtime is `38992e955f2f268c2a6d4ecd81d900feba83f048`. This handoff does not state that it is the live verified checkout; it only records the latest accepted evidence provided to the repository task.

A production task must independently verify exact checkout, config drift, migrations, static integrity, service state and browser acceptance. Do not touch production from an ordinary development or documentation task.

## Further Reading

- [README](../README.md)
- [Current Version Status](current-version-status.md)
- [Architecture](architecture.md)
- [Development Guide](development.md)
- [Provider Runtime Gates](provider-runtime-gates.md)
- [Provider Lab](provider-lab.md)
- [Repository Governance](repository-governance.md)
- [Production Deploy Checklist](production-deploy-checklist.md)
