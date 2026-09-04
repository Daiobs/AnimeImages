# Current Version Status

Date: 2026-09-04

## Source Of Truth

| Concern | Source of truth |
| --- | --- |
| Development repository | Latest fetched `origin/main` and its code/tests |
| Merged capability | Merge commit and final PR evidence |
| Recorded production application | Last explicitly recorded deployed SHA |
| Live production application | Requires a separate authorized server verification |
| Production infrastructure | Exact host configuration from a separate infrastructure audit |
| Runtime configuration | Deployment environment and persisted Admin settings |
| Historical acceptance | Dated acceptance/checkpoint records in this file and `docs/research/` |

Never infer deployment from a merge to `main`. Repository code, recorded production code, active host configuration and Runtime Settings are separate facts.

The authorized Asset 3 activation on 2026-09-04 verified the live production
checkout and deployed the exact reviewed SHA below. See the
[activation record](research/production/ASSET_3_PRODUCTION_ACTIVATION_V1.md) for
the operational scope and incomplete email/browser gates. This is not a new
full-platform browser acceptance.

## Development Repository

```text
CURRENT_DEVELOPMENT_BASELINE = LATEST_FETCHED_ORIGIN_MAIN
DOCUMENTED_APPLICATION_BASELINE = 509ef74b76a4424f907bbf6152a824b5a9b2d633
```

This documentation was prepared against the application baseline above, which
includes the development changes merged through PR #82. Resolve the
current Repository SHA instead of treating this snapshot as a permanent main
pointer:

```bash
git fetch origin
git rev-parse origin/main
```

### PR #74

XHS ordinary-video Hybrid:

- Native exact note identity and metadata;
- optional TianTian high-quality ordinary video;
- Native ordinary-video fallback;
- no TianTian call for images or Live Photo.

### PR #75

XHS image fidelity and Staff Live Photo:

- ParseHub raw original image candidates;
- Native exact same-item fallback and source order;
- shared static preview path;
- Anonymous Single and Batch remain flat static images;
- Active Staff Single gets Live Photo badge and one-shot playback;
- Staff browser archives support still + motion pairs and mixed selections;
- ZIP entry timestamp repair.

### PR #76

Staff-only Upstream Provider Lab:

- fixed five-Provider Registry;
- whole-repository ParseHub Worker;
- whole-repository videodl Worker;
- isolated venv/process/result governance;
- existing AnimeImages, yt-dlp and TianTian wrappers;
- Bilibili three-sample comparison;
- no formal platform route change.

## Last Recorded Production Runtime

```text
last verified production runtime = 509ef74b76a4424f907bbf6152a824b5a9b2d633
previous verified production runtime = 38992e955f2f268c2a6d4ecd81d900feba83f048
source PR = #82
source title = Add daily real-sample validation reports and notifications
```

The activation verified a clean production `main`, created and verified a
PostgreSQL backup, deployed the reviewed target, applied migration 0025 and
rechecked local/public health. The deployment used the existing scripts and
retained the Web and Nginx topology.

PR #74, #75 and #76 code is included in the deployed target. Deployment does not
claim renewed browser acceptance or enable the Staff Provider Lab page.

## Repository Ahead Of Production

```text
REPOSITORY_MAIN_AHEAD_OF_RECORDED_PRODUCTION = NO_AT_ACTIVATION
AHEAD_TYPE = NONE_AT_REVIEWED_TARGET
PRODUCTION_VERIFICATION_SCOPE = EXACT_SHA_AND_ASSET_3_OPERATIONS
```

Repository main and the live checkout matched the reviewed target at activation.
The activation-record Draft PR is documentation only and is not deployed.

Any future deployment must compare exact SHAs, run the release gates and receive explicit production authorization. This file does not authorize deployment.

## Current Capability Boundaries

The following describes development main. The last column records whether this documentation task has evidence of production deployment.

| Capability | Anonymous | Active Staff | Batch | Development main | Recorded production evidence |
| --- | --- | --- | --- | --- | --- |
| XHS ordinary video | Native; optional Ultra by existing product setting | Same route with Staff diagnostics | Flat ordinary result | Native identity + TianTian Ultra + Native fallback | Code deployed; no renewed browser acceptance |
| XHS image/gallery | Ordered flat originals | Same originals | Ordered flat originals | Native identity/order + ParseHub raw original + Native same-item fallback | Code deployed; runtime gates not changed |
| XHS Live Photo | Flat stills only | Badge, one-shot playback, still + motion archive | Flat stills only | Staff-only vertical slice | Code deployed; no renewed browser acceptance |
| Douyin | Native baseline; Ultra by Runtime Setting | Additional diagnostics | Separate TianTian -> Native order | Stable fixed strategy | Native parse-only canary and scheduled check passed |
| Bilibili | Native video-only | Native plus Lab comparison when Lab enabled | Existing video boundary | Native remains formal route | Code deployed; route configuration not changed |
| Provider Lab | No | Staff-only when flags enabled | No | Present, default off | Code deployed; Lab page remains disabled |

## XHS Fixed Routes

### Ordinary Video

```text
Native exact identity and metadata
  -> TianTian high-quality ordinary video when Ultra is enabled
  -> Native fallback when TianTian is unavailable or rejected
```

TianTian is never called by the XHS image or Live Photo branches.

### Images

```text
Native exact note identity and source order
  -> ParseHub raw original candidate
  -> Native exact same-item fallback
```

Preview and original download resources are separate. Admitted original formats are JPEG, PNG, GIF, WebP, AVIF and HEIC.

If the ParseHub XHS Runtime is disabled, unavailable or fails correspondence/admission checks, the same Native item remains authoritative. There is no cross-item replacement or recommendation scan.

### Live Photo

- Anonymous Single: flat images; no badge, motion, playback or pair archive.
- Active Staff Single: exact same-item still/motion relationship, one-shot playback, ended restore, replay and browser archive.
- Batch: flat static images; no motion, pair token or Live Photo archive.
- Archive semantics: original still + motion MP4, not an automatic iOS Photos import.

## Douyin Fixed Routes

Normal parsing uses Native baseline.

Single Ultra:

```text
TianTian preferred
  -> KuKuTool fallback / race reserve
  -> Native fallback
```

Batch Ultra:

```text
TianTian
  -> Native fallback
```

The PR #65 recorded production line admits only reviewed source-bound final redirect families and converts terminal browser download failures into explicit retryable UI state. New XHS development work does not imply a Douyin production change.

## Bilibili Fixed Route

```text
BILIBILI_CURRENT_SCOPE = VIDEO_ONLY
BILIBILI_VIDEO_ROUTE = NATIVE
```

Current development support includes BV, AV, `b23.tv`, `p=` part selection and official DURL media. The adapter reads the title, UP owner name and cover from official View data. The title and owner name feed the current download filename semantics, while the cover enters the media result; an independent structured `ParseResult.metadata.author` value is not yet a stable contract.

Three real Provider Lab samples established:

- Native, TianTian, videodl and official DURL media bytes were identical;
- no TianTian Bilibili quality delta was proven;
- no videodl Bilibili quality delta was proven;
- ParseHub exposed additional format metadata but no deliverable media improvement;
- yt-dlp returned `PROVIDER_PROCESS_FAILED`, a known Provider-specific nonblocking limitation.

Dynamic, Opus, Live Photo, Cookie/member-only quality and DASH audio/video merge remain deferred. Provider Lab does not change this route.

## Provider Lab State

Provider Lab is present in development main and defaults off:

```text
UPSTREAM_PROVIDER_LAB_ENABLED=False
VIDEODL_PROVIDER_LAB_ENABLED=False
PROVIDER_LAB_PRODUCTION_STATUS=CODE_DEPLOYED_UI_DISABLED
```

It is Staff-only, sends one POST per Provider, limits browser concurrency to three, does not let Django fan out automatically, does not choose a winner and does not persist results.

Whole-repository pins:

```text
ParseHub=144260f4349c5bb57415f7555a572fcd48c3b8ba
videodl=6cd1de0fe1209380b183b654c65e98a629637873
```

videodl is PolyForm Noncommercial 1.0.0. The 2026-09-04 task separately authorized
internal noncommercial production validation: its persistent isolated runtime
is enabled for the daily policy inventory while `UPSTREAM_PROVIDER_LAB_ENABLED`
remains false. This does not authorize a public or formal production Provider
route, Common execution, provider downloads or browser execution.

See [Provider Lab](provider-lab.md) for stable operations and [Provider Lab research](research/provider_lab/01_UPSTREAM_PROVIDER_LAB_V1.md) for historical design evidence.

The videodl Asset 1/2 development branch adds controlled, observable execution
inside this same default-off Staff Lab: exact backend, dedicated-only,
explicitly confirmed common-only, reviewed-auto, and an opaque legacy
comparison. AnimeImages owns bounded attempt ordering and safe diagnostics;
videodl remains the exact-pin isolated noncommercial upstream. This changes no
formal platform route and authorizes no Production deployment. See the
[implementation outcome](research/videodl/ASSET_1_2_IMPLEMENTATION_OUTCOME_V1.md)
for the 26 component dispositions.

## Download Data Plane

Ordinary Provider video:

```text
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk
```

Ordinary image/archive:

```text
Image CDN -> Browser image/archive implementation -> Disk
```

Staff Live Photo archive:

```text
Still CDN + Motion CDN -> Browser archive -> Disk
```

AnimeImages remains the control, validation and descriptor authority. Ordinary media does not use automatic Server Relay. Server-generated outputs such as an explicit merge remain a distinct resource class and may use a server path.

## Open And Deferred Work

PR #62 remains outside current main:

```text
branch = feat/external-tool-progress-materials-v1
head = bb5f63f348ad5a1ae2f40332077610af51eb8940
state = OPEN / Draft / DEFERRED / not merged
```

It was not modified by the documentation refresh and must not be described as a current capability.

Other explicit boundaries:

- Enabling the Provider Lab production page remains a separate future task.
- XHS Anonymous and Batch Live Photo motion remain closed.
- Bilibili Dynamic/Opus and merge upgrades remain research/product work.
- videodl cannot enter public or production routes under the current license boundary.
- Provider Lab resource inspection is not full-file equivalence proof.
- Asset 3 has operational acceptance with email and Admin browser gaps; no
  renewed full-platform browser/download acceptance is claimed.

## Previously Accepted Milestones

These records preserve historical acceptance evidence. They are not claims about the active production checkout on 2026-08-29 unless a separate deployment record says so.

### Production Promotion Checkpoint

The immutable historical checkpoint `checkpoint/production-promotion-ready-v1` points to `766cecfd239f594e791c556b4caa69aebf2d1f9f`. Its acceptance established Douyin canonical identity, reviewed media host admission, restored size metadata, Browser Streaming zero ordinary-media server bytes, and real-device download completion.

Observed mobile destinations were Android Gallery and iOS Files. Automatic iOS Photos import was not implemented and was accepted as nonblocking.

This checkpoint was not moved by PR #74, #75 or #76. It is historical evidence, not a statement that the recorded runtime still equals the checkpoint SHA.

### Production Polish

The historical Production Polish line accepted PR #47 through #52 at application SHA `a87ec475...`:

- retired public legacy submissions while retaining historical model state;
- hid internal Provider diagnostics from public results;
- consolidated project settings;
- added protected user feedback with migration `0024_userfeedback`;
- added browser identity assets;
- tightened XHS source identity, Gallery layout and Douyin display metadata.

The corresponding browser gate recorded no ordinary-video Server Relay and no unexpected application errors in its observation window.

### Browser Photo Archive

The browser-first photo archive was historically accepted at `e70848c8b89225beba0838e9b681aaf5b7fdf789`, with checkpoint `checkpoint/production-browser-photo-archive-v1`.

Recorded evidence included:

- production-origin XHS CORS;
- correct filesystem extension and JPEG magic;
- source/download byte equality;
- valid ordered STORE ZIP entries and CRC;
- real progress/speed/ETA;
- zero AnimeImages media endpoint requests on successful browser photo paths.

See [Browser Photo Archive research](research/browser-photo-archive-format-reliability-v1.md).

### Download UI And Batch Actions

PR #42 historically removed the redundant global download panel while retaining Service Worker state and card-local progress. Public Batch download/copy boundaries and Staff-only raw exports were accepted without changing ordinary-media zero-relay behavior.

See [Download UI acceptance](research/download-ui-batch-actions-production-acceptance-v1.md).

### Provider Stabilization

The Provider Stabilization line first exposed a production formatter mismatch and rolled back, then accepted its corrected formatter and Provider terminal diagnostics at the next reviewed candidate. Evidence recorded exact browser Streaming, zero ordinary-media server bytes, bounded Batch behavior and sanitized logs.

PR #65 later superseded those historical candidates; the 2026-09-04 Asset 3
activation now records the newer exact production application SHA above.

## Historical Infrastructure Evidence

Earlier audits recorded a canonical Nginx and systemd baseline, PostgreSQL,
Redis-backed shared controls and a Browser Download Telemetry route. The Asset 3
activation compared pre/post file hashes: Nginx configuration and the Web unit
were unchanged. Only the two approved validation units were installed.

Canonical infrastructure files remain under `ops/`; merging repository files does not install, reload or restart production configuration.

## Governance

- Start work from the latest fetched `origin/main`, not a production checkout.
- Use one task, one branch and one PR.
- Keep exact-SHA test and browser evidence with the PR that changes behavior.
- Treat deployment as a separate authorized task.
- Do not move immutable checkpoints.
- Keep research evidence under `docs/research/` and stable usage under top-level `docs/`.
- Update this file when development main, recorded production runtime or a major product boundary changes.

## Current Summary

```text
CURRENT_DEVELOPMENT_BASELINE = LATEST_FETCHED_ORIGIN_MAIN
DOCUMENTED_APPLICATION_BASELINE = 509ef74b76a4424f907bbf6152a824b5a9b2d633
LAST_RECORDED_PRODUCTION_RUNTIME = 509ef74b76a4424f907bbf6152a824b5a9b2d633
REPOSITORY_MAIN_AHEAD_OF_RECORDED_PRODUCTION = NO_AT_ACTIVATION
AHEAD_TYPE = NONE_AT_REVIEWED_TARGET
PRODUCTION_REVERIFIED_BY_THIS_TASK = EXACT_SHA_AND_ASSET_3_OPERATIONS
PRODUCTION_DEPLOYMENT_BY_THIS_TASK = YES
```

## Asset 3 Daily Validation Activation

Asset 3 extends the existing Provider Health Tests rather than
introducing a global health platform. It preserves the original nine checks,
adds five XHS/Bilibili/videodl checks, and supplies one bounded daily Suite,
change-based email logic, an Admin report, retention, and inactive systemd
candidates. Code defaults keep daily execution and email delivery off; the
authorized production activation now enables daily execution with the three
validated keys below. Missing approved SMTP and recipients keep both email
gates closed. Admin ORM/report evidence passed; the browser tool timed out.

The authorized reconnection closeout on the same date confirmed the unchanged
production target, environment and dependency checks, healthy Web/timer state,
evidence-preserving temporary cleanup and sudo credential-cache revocation.
The next automatic run had not occurred; the existing scheduled 3/3 result was
retained without another canary. No redeploy or production configuration change
was performed during closeout. Email and browser acceptance gaps remain open.

```text
ASSET_3_CODE = DEPLOYED
ASSET_3_PRODUCTION_ACTIVATION = PARTIAL
DAILY_ENABLED = YES
DAILY_TEST_KEYS = health_ready, douyin_native_parse, videodl_policy_inventory
APPROVED_SAMPLE_HOST = www.douyin.com
FIRST_SCHEDULED_SUITE = SUCCESS_3_OF_3
SMTP_CONFIGURED = NO
EMAIL_DB_GATE = DISABLED
EMAIL_ENV_GATE = DISABLED
ADMIN_BROWSER_SMOKE = NOT_AVAILABLE_TOOLING_TIMEOUT
SYSTEMD_INSTALLED = YES
TIMER_ENABLED = YES
ACTIVATION_SPECIFIC_ROUTE_OVERRIDE = NO
ACTIVATION_SPECIFIC_DOWNLOAD_CHANGE = NO
SSH_CLOSEOUT = PASS
SUPPLEMENTAL_ENVIRONMENT_CHECK = PASS
DEPENDENCY_CHECK = PASS
OPERATIONAL_TEMP_CLEANUP = PASS
SUDO_CREDENTIAL_CACHE_REVOCATION = PASS
POST_CLOSEOUT_WEB_HEALTH = PASS
POST_CLOSEOUT_TIMER = ACTIVE_AND_ENABLED
FINAL_ACTIVATION_STATUS = PARTIAL_EMAIL_NOT_CONFIGURED
PRODUCTION_REDEPLOY_DURING_CLOSEOUT = NO
```
