# videodl Architecture And Code Absorption v1

## Purpose

This document decides how AnimeImages should treat each major capability in
videodl at pin `6cd1de0fe1209380b183b654c65e98a629637873`.
It is an architecture decision, not permission to copy code or alter a formal
route.

Use these companion documents for non-duplicated detail:

- [Whole-repository audit](01_VIDEODL_WHOLE_REPOSITORY_UPSTREAM_AUDIT_V1.md)
  describes actual fixed-pin control flow and sampled clients.
- [Stage, lineage, and attempt RFC](03_BACKEND_STAGE_LINEAGE_AND_ATTEMPT_RFC_V1.md)
  owns backend semantics.
- [Observation and delivery RFC](04_UPSTREAM_OBSERVATION_AND_DELIVERY_RFC_V1.md)
  owns target fields and projections.
- [Foundation roadmap](05_FOUNDATION_IMPLEMENTATION_ROADMAP_V1.md) converts
  these decisions into small future PRs.
- [Reuse and provenance matrix](06_COMPONENT_REUSE_AND_PROVENANCE_MATRIX_V1.md)
  is the row-by-row decision record.

## Decision Vocabulary

| Decision | Meaning |
| --- | --- |
| `WHOLE_REPOSITORY_UPSTREAM` | Execute the exact repository in its isolated process boundary; do not import the component into AnimeImages. |
| `ISOLATED_FORMAL_UPSTREAM` | Keep one explicitly reviewed, fixed-pin backend behind an AnimeImages-owned isolated Worker in one fixed formal route. This never permits registry traversal or first-success routing. |
| `DIRECT_REUSE` | Copy or import with minimal semantic change, preserving license notices and provenance. No component currently earns this recommendation. |
| `ADAPTED_REUSE` | Reuse a bounded component with explicit security and contract changes. |
| `CLEAN_REIMPLEMENTATION` | Preserve proven concepts and tests while writing an AnimeImages-native implementation against its own contracts. |
| `REJECT` | Do not absorb the implementation into product runtime. It may remain historical or Lab evidence. |

The current AnimeImages use is recorded as noncommercial. videodl remains
PolyForm Noncommercial 1.0.0. Any future direct or adapted reuse must preserve
source provenance, provide the license/required notices, track modifications,
and maintain a replacement plan before commercialization. No source code is
copied by this research change.

Access and architecture are separate decisions:

```text
CURRENT_ACCESS_SCOPE = STAFF
CURRENT_LIFECYCLE_STAGE = EXPERIMENTAL
ARCHITECTURAL_ELIGIBILITY = MAY_BECOME_FIXED_FORMAL_UPSTREAM_AFTER_EXPLICIT_REVIEW
```

The current Staff Lab is the experimental access surface, not a permanent
architectural ceiling. A specific backend can become an
`ISOLATED_FORMAL_UPSTREAM` only after real-sample, identity, host, format,
delivery, security, stability, and license review. The whole registry,
automatic traversal, first-success product routing, and unreviewed common or
grabber backends remain ineligible.

## Registry And Builder Audit

### Fixed-Pin Design

`BaseModuleBuilder` owns a class-level mutable `REGISTERED_MODULES` mapping.
Subclasses populate literal dictionaries at import time and expose `register`,
`renew`, `delete`, `pop`, `build`, and related mutation methods. Assertions
enforce many invariants. `VideoClient` derives order from those mappings,
stores class/config pairs, and lazily instantiates a client when attempted.
`allowed_video_sources`, per-client config, and per-client request overrides
can narrow behavior.

### Value

- explicit client names;
- deterministic insertion order at a given pin;
- lazy heavy-client construction;
- a common build path for dedicated and common backends;
- useful per-backend configuration hooks.

### Risks

- global mutable class state can leak across tests and requests;
- import-time registration eagerly imports the entire client graph and its
  optional dependencies;
- mutation is not thread-safe;
- `assert` is not a runtime policy boundary under optimized Python;
- order changes silently alter fallback behavior;
- registry entries lack backend type, trust, data-sharing, browser, identity,
  timeout, and delivery metadata;
- an allowlist does not strictly disable implicit grabber fallback.

### Decision

`BaseModuleBuilder`, `VideoClientBuilder`, and `CommonVideoClientBuilder` are
`CLEAN_REIMPLEMENTATION` candidates. The target is an immutable explicit
catalog of frozen backend descriptors, separated from process-local backend
instances. Catalog validation must use normal exceptions, preserve order as
reviewed data, and make mode/stage eligibility explicit.

## VideoInfo Mapping

`VideoInfo` combines upstream observations, private runtime state, final asset
facts, and downloader instructions in one mutable object. That convenience is
valuable inside videodl, but the mixed trust domains are inappropriate for an
AnimeImages cross-upstream contract.

| `VideoInfo` field | Target category | Treatment |
| --- | --- | --- |
| `source` | `UPSTREAM_OBSERVATION` | Map to selected backend and lineage, not directly to a formal provider. |
| `raw_data` | `PRIVATE_RUNTIME_ONLY` | Never return or log; retain only bounded `raw_shape`. |
| `title` | `UPSTREAM_OBSERVATION` | Bounded metadata claim; admission decides whether to expose it. |
| `cover_url` | `UPSTREAM_OBSERVATION` | Private candidate reference until host/format admission. |
| `err_msg` | `REJECT` | Raw URL and exception text may be embedded; map to fixed failure class/code. |
| `download_url` | `UPSTREAM_OBSERVATION` | Transfer only through the bounded private Worker sidecar; the observation carries an opaque ref. |
| `audio_download_url` | `UPSTREAM_OBSERVATION` | Separate audio track and merge-group evidence. |
| video/audio headers | `PRIVATE_RUNTIME_ONLY` | Preserve names/required booleans; values stay in private delivery context. |
| video/audio cookies | `PRIVATE_RUNTIME_ONLY` | Values never enter observation, Lab JSON, logs, or public contracts. |
| `ext`, `audio_ext` | `UPSTREAM_OBSERVATION` | Declared format claim, later verified before admission. |
| `save_path`, `audio_save_path` | `REJECT` | Upstream filesystem decisions do not cross the process boundary. |
| guessed-extension results | `UPSTREAM_OBSERVATION` | Preserve method and claimed extension only after sanitization. |
| `download_with_ffmpeg` | `DELIVERY_REQUIREMENT` | Translate to HLS/DASH/remux/transcode facts; do not carry the boolean blindly. |
| `ffmpeg_settings` | `PRIVATE_RUNTIME_ONLY` | Arbitrary command settings are not accepted from a provider. |
| `enable_nm3u8dlre` | `DELIVERY_REQUIREMENT` | At most evidence of segmented-media handling; provider cannot select the executor. |
| `nm3u8dlre_settings` | `REJECT` | Do not accept arbitrary external-tool arguments. |
| `download_with_aria2c` | `DELIVERY_REQUIREMENT` | Indicates a performance preference, not a product requirement; usually omit. |
| `aria2c_settings` | `REJECT` | Do not accept arbitrary external-tool arguments. |
| `identifier` | `UPSTREAM_OBSERVATION` | Treat as an untrusted backend-local identity claim, never source authority. |
| extra fields | `PRIVATE_RUNTIME_ONLY` | Explicit allowlist only; unknown fields contribute to `raw_shape`, not output. |

Final admitted dimensions, duration, size provenance, codecs, role, safe header
requirements, and format can be projected into `MediaAsset`. Attempts,
lineage, failure history, raw payload, URLs, credential values, local paths,
and executor settings must not be added to `MediaAsset`.

`VIDEOINFO_TO_ANIMEIMAGES_MAPPING = DOCUMENTED`

`VIDEODL_RESULT_MODEL_ADOPTION = CLEAN_REIMPLEMENTATION`

## BaseVideoClient

`BaseVideoClient` combines HTTP sessions, retries, proxy selection, logging,
parsing support, downloader selection, filesystem naming, process execution,
progress, temporary files, and parallel download. It also has paths that retry
ordinary requests with TLS verification disabled, logs full media URLs in
several error/warning messages, and allows provider-owned command settings.

The class is a useful map of delivery cases, but it is too large and crosses
too many AnimeImages ownership boundaries. It must not become a giant base
provider in AnimeImages.

`BASE_VIDEO_CLIENT = REJECT`

Selected pure algorithms may be cleanly reimplemented behind explicit
contracts. Network, process, filesystem, and delivery policy must stay in
AnimeImages-owned services.

## Dedicated Clients

Dedicated clients are best retained initially as `WHOLE_REPOSITORY_UPSTREAM`
evidence in the Staff Lab. They do not share a strong enough identity,
transport, error, or delivery contract for direct registration in formal
routes as a group.

The sampled clients demonstrate key variation:

- platform API versus share-page HTML versus browser state;
- full file versus separate video/audio versus playlist;
- optional or required Referer/header/Cookie context;
- first-party platform endpoints versus nested third-party aggregators;
- short-link handling and canonical identity quality;
- browser process usage and cleanup quality;
- raw exception messages that may include source URLs.

If a future audit proves one client adds value, two reviewed paths exist:

- a platform-scoped `CLEAN_REIMPLEMENTATION` with AnimeImages identity
  authority, host admission, error classes, deadlines, and tests; or
- an `ISOLATED_FORMAL_UPSTREAM` that invokes only that fixed backend through
  an AnimeImages-owned Worker and preserves the same admission controls.

The second path does not directly import the client into the application and
does not expose the rest of `BaseVideoClient`, the registry, common clients,
or grabber fallback to the formal route.

## Common Clients

Common clients are external aggregator adapters, not a local generic parser
library. The four sampled clients all send the target URL to a third-party
service. Several locally decode private API responses or rank formats, but the
media authority, quotas, protocol stability, and data-sharing boundary remain
external.

Decision:

- current role: `WHOLE_REPOSITORY_UPSTREAM`, Staff Lab only;
- formal auto-routing: rejected;
- selected future integration: a separate explicit provider integration or
  `ISOLATED_FORMAL_UPSTREAM` only after privacy, terms, quota, host, identity,
  delivery, reliability, and license review;
- attribution: nested service lineage is mandatory.

## WebMediaGrabber

The grabber contains two separable ideas:

1. bounded direct/static inspection;
2. broad browser network observation.

The first is a `CLEAN_REIMPLEMENTATION` candidate using AnimeImages SSRF
guards, TLS verification, host/format policy, byte bounds, candidate limits,
and identity binding. The second is `REJECT` for formal routes in its current
form and remains `WHOLE_REPOSITORY_UPSTREAM` workbench evidence only.

Browser observation cannot distinguish requested media from ads,
recommendations, preload traffic, or other page cards. The fixed pin also
ignores certificate errors in Chromium and has no AnimeImages private-network
policy. Exact URL deduplication solves duplication, not authority.

## File And Domain Helpers

### FileTypeSniffer

Useful concepts:

- path extension as a low-confidence hint;
- MIME normalization;
- bounded header/byte signature detection;
- provenance of the detection method.

Unsafe or weak aspects:

- its network calls are not intrinsically host-admitted;
- timeout safety depends on caller overrides;
- declared MIME and broad extension lists are not enough for admission;
- the fixed-pin Range-byte branch contains an assignment-expression defect
  that can prevent the detected magic result from being applied;
- it mixes observation and network execution.

Decision: `CLEAN_REIMPLEMENTATION`, or use existing AnimeImages raster/media
signature helpers. Keep network probing outside the pure sniffer.

### Request Decorators

The parse/download decorators mutate shared client session state and
reinitialize sessions around a method call. They are concise but not suitable
for concurrent request isolation. Decision: `CLEAN_REIMPLEMENTATION` as
immutable request contexts if needed.

### Domain Helpers

Suffix matching and normalized hostname extraction are sound concepts, but
AnimeImages already has blocked-host and suffix helpers integrated with its
security boundary. Decision: reuse the AnimeImages implementation; videodl is
reference provenance only.

## HLS And Command Planning

### HLS Helpers

The fixed pin contains useful master-playlist parsing, variant ordering, and
bounded segment probing ideas. It also contains a helper that explicitly
disables TLS certificate and hostname verification, verbose paths that print
raw URLs, and high-concurrency probing without AnimeImages admission.

Decision: `CLEAN_REIMPLEMENTATION` of small pure parsing/scoring pieces only.
Do not absorb insecure network behavior.

### Command Builders And FFmpeg Planning

The structured argv builder and distinct plans for download, remux, audio
transcode, full transcode, and copy merge are valuable architecture
references. AnimeImages must own executable allowlists, argument schemas,
input/output paths, deadlines, resource limits, and output verification.

Decision: `CLEAN_REIMPLEMENTATION`. Provider-supplied `ffmpeg_settings` must
never become arbitrary command arguments.

### N_m3u8DL-RE And aria2c

These are optional execution engines, not parser facts. They add binaries,
command surfaces, temporary media, and server bandwidth. They conflict with
ordinary Browser Zero Relay and are unnecessary for the first foundation PRs.

Decision: `REJECT` from automatic product delivery. Reconsider only for a
separately reviewed server-generated output where the normal delivery
contract proves a need.

## Chromium Utilities

The utilities solve executable discovery, port selection, proxy conversion,
temporary-profile cleanup, and browser startup. They can also download a
browser executable and expose complex environment-dependent behavior.

Decision: `REJECT` for direct AnimeImages runtime reuse. If browser
observation is ever approved, isolate it in a dedicated external process with
an exact browser/runtime pin, network sandbox, TLS enforcement, CPU/memory
limits, process-tree cleanup, and observation-only output.

## Delivery Architecture Comparison

videodl embeds executor choice in `VideoInfo` and lets `BaseVideoClient`
dispatch the work:

- direct request streaming;
- separate video/audio fetch and FFmpeg merge;
- HLS/DASH through FFmpeg or N_m3u8DL-RE;
- aria2c;
- YouTube stream objects;
- special platform paths;
- local temporary files, naming, progress, and parallelism.

AnimeImages already separates product delivery paths:

- Browser Zero Relay and Service Worker for eligible ordinary media;
- browser archive for client-generated multi-resource output;
- server merge for genuinely server-generated output;
- signed/opaque manifest and recovery controls;
- platform admission before a resource becomes downloadable.

The durable principle is:

> A provider reports acquisition and assembly facts. AnimeImages admission
> alone selects the product delivery disposition.

The mapping is defined in the
[Observation and delivery RFC](04_UPSTREAM_OBSERVATION_AND_DELIVERY_RFC_V1.md).
Do not import videodl's downloader dispatcher or let a provider select a
formal user route.

```text
DELIVERY_ARCHITECTURE_COMPARISON = DOCUMENTED
DELIVERY_COMPONENT_REUSE_MATRIX = DOCUMENTED
```

## AnimeImages Gap Matrix

| Current area | Existing strength | Missing foundation | Decision |
| --- | --- | --- | --- |
| Platform Adapter | Explicit platform identity and capabilities | Cross-upstream attempt evidence | Keep; do not generalize into a planner. |
| Formal Provider routes | Platform-specific policy and ordering | No gap required for this audit | Freeze. |
| `GenericThirdPartyAdapter` | Host/risk/provider admission | Nested backend lineage and observation stages | Do not route videodl through it automatically. |
| Provider Lab | Bounded subprocess lifecycle, tokens, independent cards | Worker collapses all attempts to one final result; current execution is not a sandbox | Add observation contracts in Lab first. |
| `MediaCandidate` | Compact selection candidate | Track/relationship/delivery/attempt model | Keep compact; observation precedes it. |
| `MediaAsset` | Final runtime/download asset | Not intended for attempts or lineage | Do not expand with trace data. |
| `ParseResult` | Final platform result | Not a multi-backend evidence envelope | Project admitted observation into it. |
| `RichMediaContractV1` | URL-free item-local relationships | Upstream attempt context | Keep relationships separate from trace. |
| `LabProviderResultV1` | Bounded Staff result | Stage, lineage, attempts, format inventory | Projection target, not source of truth. |
| External process lifecycle | Bounded env/output/time/process group | Backend-internal attempt deadlines | Reuse parent boundary; add worker evidence later. |

Answers to the required design questions:

1. `UpstreamObservationV1`: **yes**, as an internal private evidence envelope.
2. `AcquisitionRequirementV1` and `AssemblyRequirementV1`: **yes**, as
   observed facts; `DeliveryDispositionV1` remains an AnimeImages admission
   decision.
3. `BackendLineageV1`: **yes**, because class names hide external services.
4. `AttemptTraceV1`: **yes**, because fallback currently erases failures.
5. Expand `MediaAsset`: **no** for attempts, lineage, raw formats, or private
   requirements. Only admitted final facts may be added through a later
   platform-specific change.
6. Always private: source/media URLs, signed queries, header/Cookie values,
   raw payloads, exception strings, local paths, third-party request bodies,
   and provider executor arguments.
7. Lab-only today: sanitized backend/stage/lineage, attempt outcomes and
   timings, evidence and relationship dimensions, format inventory,
   warning/error codes, and tokenized resource inspection. A future fixed
   formal upstream requires a separate explicit review and route PR.
8. Routes not to migrate: XHS, Douyin, Bilibili, YouTube, Batch, and all
   current download delivery behavior.

## Do-Not-Absorb List

- first syntactically valid `http` value automatically becoming the formal
  winner;
- broad `except` blocks that silently erase backend failure evidence;
- a giant base provider combining parse, network, filesystem, and delivery;
- routine `verify=False` or disabled certificate checks as fallback;
- arbitrary-page browser traffic entering Public results;
- loading or executing an arbitrary Git repository;
- provider-owned selection of formal download transport;
- Cookie values entering a result contract;
- full source/media URLs or raw exception text entering ordinary logs;
- a common external aggregator being labeled as videodl-native capability;
- Provider Lab becoming an automatic formal router;
- mutable global registries as request policy;
- provider-controlled FFmpeg, aria2c, or other command arguments;
- upstream local paths becoming AnimeImages delivery paths.

## Asset 2 Conclusion

`ASSET_2_REUSE_MATRIX = COMPLETE`

The strongest near-term asset is not a directly reusable downloader. It is a
catalog of real resolution and delivery cases that can drive AnimeImages-owned
contracts and tests. Keep whole-repository execution isolated, preserve its
backend evidence, and either reimplement a small safe concept or explicitly
review one fixed backend for `ISOLATED_FORMAL_UPSTREAM`; neither path permits
automatic registry traversal.
