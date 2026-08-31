# videodl Asset 1 And Asset 2 Implementation Outcome v1

## Decision And Scope

This document records an implemented, Provider-Lab-local vertical slice. It is
not a roadmap and does not reactivate the superseded PR #78 contract-first
sequence.

```text
ASSET_1_WHOLE_REPOSITORY_UPSTREAM = COMPLETE
ASSET_2_ARCHITECTURE_AND_CODE_ABSORPTION = COMPLETE
ASSET_3_PRODUCTION_DAILY_VALIDATION = DEFERRED_NOT_CANCELLED
GLOBAL_OBSERVATION_KERNEL = NO
GLOBAL_BACKEND_CATALOG = NO
FORMAL_ROUTE_CHANGE = NO
PRODUCTION_DEPLOYMENT = NO
```

The producer-consumer check was used as an anti-overengineering review
heuristic, not as an absolute requirement. Every new runtime type and field has
both a current producer and a current Staff Lab consumer.

## Fixed Upstream Boundary

| Field | Value |
| --- | --- |
| Repository | `CharlesPikachu/videodl` |
| Exact pin | `6cd1de0fe1209380b183b654c65e98a629637873` |
| License | PolyForm Noncommercial 1.0.0 |
| Authorized use | Noncommercial local Staff Lab only |
| Runtime | Independent venv and bounded subprocess |
| Inventory | 70 dedicated and 37 common backends at the reviewed pin |

No videodl source was copied or modified. The isolated worker imports the
exact-pin registries and backend implementations. AnimeImages owns only the
request schema, execution policy, process boundary, attempt diagnostics and
Staff presentation.

## Controlled Modes

| Mode | Controlled behavior |
| --- | --- |
| `specific_backend` | Runs exactly one fixed-registry backend. No direct, dedicated, common or grabber fallback. |
| `dedicated_only` | Runs matching `allowed` dedicated backends in fixed registry order. Sharing-required and blocked matches are policy-skipped; no common or grabber stage. |
| `common_only` | Runs only explicitly selected common IDs after an explicit external-sharing confirmation. |
| `reviewed_auto` | Runs matching `allowed` dedicated backends, then only confirmed and selected common IDs. |
| `legacy_auto` | Preserves the old opaque `VideoClient.parsefromurl()` path for comparison; it is not the default and is not completion evidence. |

The backend category is registry metadata, not an execution or sharing policy.
The exact-pin local policy reviewed all 70 dedicated backends: 60 are
`allowed`, `C56VideoClient` requires explicit sharing confirmation because it
delegates to a nested platform parser, and 9 are blocked. The blocked set covers
DrissionPage/Chromium execution, embedded Cookie or Provider credential
material, TLS-verification bypass, nested third-party aggregation and an
unapproved child process.
`YouTubeVideoClient` is blocked because its first paths send the source to
external aggregators and its reachability probes disable TLS verification.
`KuaishouVideoClient`, `MGTVVideoClient` and `XinpianchangVideoClient` are
browser-capable and blocked before backend build/parse. Unknown dedicated IDs
default to blocked.

| Policy result | Dedicated backends |
| --- | --- |
| sharing confirmation | `C56VideoClient` |
| browser execution blocked | `KuaishouVideoClient`, `MGTVVideoClient`, `XinpianchangVideoClient` |
| embedded Cookie blocked | `HaokanVideoClient`, `KakaoVideoClient` |
| embedded Provider credential blocked | `DailyMotionVideoClient` |
| TLS verification disabled blocked | `YinyuetaiVideoClient` |
| nested aggregator plus TLS bypass blocked | `YouTubeVideoClient` |
| child-process execution blocked | `TencentVideoClient` |
| exact-pin reviewed allowed | the remaining 60 explicitly enumerated IDs in the local policy |

The same local policy also enumerates all 37 Common registry IDs at the exact
pin. No Common backend is directly `allowed`: 21 require explicit source-sharing
confirmation and 16 are blocked for additional execution risk. Unknown Common
IDs are blocked, and runtime inventory must match both complete policy ID sets.

| Policy result | Common backends |
| --- | --- |
| sharing confirmation | the 21 exact-pin reviewed Common IDs without an additional blocked behavior |
| TLS verification disabled blocked | `BugPkVideoClient`, `LongZhuVideoClient` |
| browser execution blocked | `GVVIPVideoClient` |
| interactive execution blocked | `IM1907VideoClient` |
| provider-owned downloader execution blocked | `VThreadsVideoClient`, `VeedMateVideoClient` |
| embedded Cookie blocked | `VideoFKVideoClient`, `ZanqianbaVideoClient` |
| embedded Provider credential or signing material blocked | `APICXVideoClient`, `IIILabVideoClient`, `QingtingVideoClient`, `SnapAnyVideoClient`, `SpapiVideoClient`, `WzjunVideoClient`, `XCVTSVideoClient`, `XiazaitoolVideoClient` |

External-sharing confirmation authorizes only sending the source reference to
the selected reviewed service. It never authorizes TLS bypass, browser or
interactive execution, provider-owned download jobs, embedded credentials or
embedded Cookies.

The bounded Common audit checked every registry implementation for disabled
TLS verification, browser/Chromium use, child-process or shell execution,
embedded authorization/credential/Cookie material, provider-owned downloader
jobs and uncontrolled command or file behavior. No Common implementation added
a separate child-process or local-file execution finding at this pin; every
positive additional-risk finding is represented by one of the 16 blocked IDs
above.

`WebMediaGrabber`, static grabber fallback, DrissionPage/Chromium and browser
observation are not available in controlled modes.

AnimeImages parent orchestration creates one bounded child process per actual
backend attempt. Each attempt has an eight-second ceiling, the reviewed run has
a 25-second total ceiling, and at most 12 attempts can be recorded. The exact
backend inventory and dedicated match operations are separately bounded.
Inventory output distinguishes callable registry presence from controlled
eligibility. It is cached for 10 minutes on success and 30 seconds on safe
failure with a key bound to the pin, local policy revision/file identity and
runtime identity; concurrent cold loads are serialized. The cache contains no
source or media URL.

## Attempt Contract

`LabProviderAttemptV1` is optional and videodl-specific in practice. Other Lab
providers emit an empty attempt list. Its closed fields are:

```text
ordinal
backend_id
backend_category
outcome
elapsed_ms
candidate_count
selected
safe_failure_code
external_sharing
```

Allowed outcomes are `success`, `failed`, `timeout`, `unsupported`, `empty`,
`policy_rejected` and `skipped`. At most one successful attempt is selected.
An attempt is created only after its backend child process is started; a total
deadline exhausted before launch does not invent an attempt.
If an earlier attempt completed and the total deadline expires before the next
backend starts, the timeout retains every completed attempt and still does not
invent the next one. `external_sharing` comes from the exact backend policy,
never from the registry category.

Safe backend failures are `BACKEND_UNSUPPORTED`, `BACKEND_EMPTY`,
`BACKEND_TIMEOUT`, `BACKEND_PROCESS_FAILED`, `BACKEND_SCHEMA_INVALID` and
`BACKEND_POLICY_REJECTED`. Diagnostics contain no source/media URL, hostname,
Header, Cookie, API body, local path, command, raw exception or provider
payload.

## Real Non-Production Evidence

The real checks used public samples without accounts, Cookie, Authorization,
private proxy or Production access. Source and media URLs were held only in
process memory and are not stored in this repository or report. No complete
media body was downloaded.

| Check | Result |
| --- | --- |
| exact-pin check-only and inventory | PASS; 70 dedicated, 37 common |
| exact dedicated backend | PASS; one selected attempt, cover plus video candidates |
| dedicated-only success | PASS; one matching dedicated backend selected |
| reviewed-auto dedicated success | PASS; common stage not entered |
| dedicated-only unsupported | PASS; fixed unsupported result and zero invented attempts |
| dedicated-only empty | PASS; one real backend attempt recorded as empty |
| selected common backend | LIVE `BACKEND_TIMEOUT` within the per-attempt bound |
| common without confirmation | OFFLINE PASS; policy rejected before worker execution |
| reviewed-auto dedicated failure then selected common | LIVE common service unavailable; order and isolation proven by deterministic tests |
| unknown backend and category mismatch | OFFLINE PASS; fail closed before backend execution |
| grabber request | OFFLINE PASS; policy rejected |

The successful sample exposed one image candidate and one video candidate with
no Cookie or Header requirement. Complete-file byte equivalence, independent
audio and HLS were not claimed because this task is parse-only and did not fetch
full media. The common service timeout is external availability evidence, not a
fallback success.

## Asset 2 Final Dispositions

Each reviewed component has exactly one current disposition. `DEFER` rows have
an explicit trigger; they are not an active backlog.

| Component | Final disposition | Current implementation or explicit reason/trigger |
| --- | --- | --- |
| Whole repository runtime | `KEEP_AS_ISOLATED_UPSTREAM` | Exact-pin independent venv and child process remain the license and dependency boundary. |
| `VideoClient` orchestration | `ABSORB_AS_ANIMEIMAGES_MODULE` | `services/provider_lab/videodl_control.py` owns reviewed order, deadlines, selection and fallback. |
| `BaseModuleBuilder` | `KEEP_AS_ISOLATED_UPSTREAM` | Worker reads the fixed registry; AnimeImages does not copy its mutable builder. |
| `VideoClientBuilder` | `KEEP_AS_ISOLATED_UPSTREAM` | Used only to describe and instantiate exact dedicated IDs in the child. |
| `CommonVideoClientBuilder` | `KEEP_AS_ISOLATED_UPSTREAM` | Used only for explicitly selected common IDs after the sharing gate. |
| `VideoInfo` | `KEEP_AS_ISOLATED_UPSTREAM` | Worker converts it to existing `LabProviderResultV1`; it never crosses as an object. |
| `BaseVideoClient` | `REJECT` | Giant HTTP/download/filesystem/command boundary, TLS fallback and unsafe diagnostic surface. |
| Dedicated clients | `KEEP_AS_ISOLATED_UPSTREAM` | Exact execution is allowed only when the pin-bound local policy says `allowed`, or after explicit confirmation for the one sharing-required backend; blocked/browser-capable clients never reach build/parse. |
| Common clients | `KEEP_AS_ISOLATED_UPSTREAM` | All 37 exact-pin IDs have local policy; only 21 explicitly selected sharing-confirmed IDs may run, while 16 higher-risk IDs and every unknown ID remain blocked. |
| WebMediaGrabber direct probe | `DEFER_WITH_EXPLICIT_TRIGGER` | Reconsider only for a platform-scoped need with SSRF, redirect, TLS, host, byte and identity gates. |
| WebMediaGrabber static extraction | `DEFER_WITH_EXPLICIT_TRIGGER` | Reconsider only with exact item binding and bounded candidate evidence. |
| WebMediaGrabber browser observer | `REJECT` | Arbitrary page execution and unrelated-media risk; controlled modes block it. |
| `FileTypeSniffer` | `USE_EXISTING_ANIMEIMAGES_EQUIVALENT` | Existing raster signature and platform admission helpers are narrower and fail closed. |
| Request decorators | `USE_EXISTING_ANIMEIMAGES_EQUIVALENT` | Parent-owned clean environment, request context and one-process-per-attempt isolation. |
| Domain helpers | `USE_EXISTING_ANIMEIMAGES_EQUIVALENT` | `services/common.py::host_matches` plus existing blocked-host policy. |
| HLS master parser | `DEFER_WITH_EXPLICIT_TRIGGER` | Only when an admitted product route needs bounded variant inventory and supplies fixtures. |
| HLS segment probing/writer | `REJECT` | Unsafe network concurrency and server media data-plane ownership. |
| Structured command builder | `USE_EXISTING_ANIMEIMAGES_EQUIVALENT` | Existing external-tool lifecycle uses fixed argv, clean env, bounded output and process-group cleanup. |
| FFmpeg planning | `USE_EXISTING_ANIMEIMAGES_EQUIVALENT` | Existing `services/youtube_merge.py` owns admitted YouTube server-generated output. |
| N_m3u8DL-RE | `REJECT` | New binary/command/server-bandwidth surface with no current consumer. |
| aria2c | `REJECT` | Ordinary media stays browser-side; no current server-generated-output need. |
| YouTube special objects | `USE_EXISTING_ANIMEIMAGES_EQUIVALENT` | Existing YouTube route and merge planner use explicit tracks rather than opaque upstream objects. |
| File naming and temporary paths | `USE_EXISTING_ANIMEIMAGES_EQUIVALENT` | AnimeImages delivery owns sanitized filenames and temporary roots. |
| Progress and parallel download | `USE_EXISTING_ANIMEIMAGES_EQUIVALENT` | Existing browser/archive/server-generated progress remains transport-owned. |
| Chromium utilities | `REJECT` | No browser observer, auto-download, proxy inheritance or certificate bypass in this Lab path. |
| Daily validation scripts | `DEFER_WITH_EXPLICIT_TRIGGER` | Asset 3: a separately authorized production scheduler, Staff report and notification design. |

No component is classified `CLEAN_REIMPLEMENT_NOW`; all required new local
behavior is the implemented orchestration/diagnostic module. No speculative
global contract or catalog was introduced.

## Runtime Consumers And Rollback

Current producers are the exact-pin worker and AnimeImages parent
orchestrator. Current consumers are the Staff Provider Lab result card,
attempt table, sanitized JSON export and existing resource-token inspection.

Rollback is local and reversible:

1. keep `VIDEODL_PROVIDER_LAB_ENABLED=False` (the default) to remove the
   provider and controls;
2. revert this vertical-slice PR to restore the legacy request-only behavior;
3. delete the task-owned videodl venv after stopping its local Lab process.

Rollback does not require a migration, dependency change, formal route change,
download change or Production action.

## Completion

```text
ASSET_1_FIXED_PIN = PASS
ASSET_1_ISOLATED_EXECUTION = PASS
ASSET_1_EXACT_BACKEND_CONTROL = PASS
ASSET_1_DEDICATED_ONLY_CONTROL = PASS
ASSET_1_COMMON_ONLY_CONTROL = PASS
ASSET_1_REVIEWED_AUTO_CONTROL = PASS
ASSET_1_HIDDEN_COMMON_FALLBACK = NO
ASSET_1_HIDDEN_GRABBER_FALLBACK = NO
ASSET_1_ATTEMPT_OBSERVABILITY = PASS
ASSET_1_DEDICATED_POLICY_REVIEWED = 70
ASSET_1_CONTROLLED_ALLOWED = 60
ASSET_1_CONTROLLED_REQUIRES_SHARING = 1
ASSET_1_CONTROLLED_BLOCKED = 9
ASSET_1_COMMON_POLICY_REVIEWED = 37
ASSET_1_COMMON_REQUIRES_SHARING = 21
ASSET_1_COMMON_BLOCKED = 16
ASSET_1_COMMON_POLICY_REGISTRY_MATCH = PASS
ASSET_1_BUGPK_TLS_BYPASS = BLOCKED
ASSET_1_UNKNOWN_COMMON_DEFAULT = BLOCKED
ASSET_1_CONTROLLED_BROWSER_EXECUTION = NO
ASSET_1_CONTROLLED_TLS_BYPASS = NO
ASSET_1_ATTEMPT_RETENTION_AT_TOTAL_DEADLINE = PASS
ASSET_1_INVENTORY_PAGE_LOAD = CACHED_AND_BOUNDED
ASSET_1_STAFF_LAB_CONTROLS = PASS
ASSET_1_STAFF_LAB_DIAGNOSTICS = PASS
ASSET_1_RESOURCE_TOKEN_FLOW = PRESERVED
ASSET_2_COMPONENTS_REVIEWED = 26
ASSET_2_REQUIRED_ABSORPTION = COMPLETE
ASSET_3 = DEFERRED_NOT_CANCELLED
```
