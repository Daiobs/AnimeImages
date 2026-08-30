# videodl Whole-Repository Upstream Audit v1

## Status

| Field | Value |
| --- | --- |
| AnimeImages base | `53297de84ebed405a27fcc28e787c4013a19b316` |
| videodl pin | `6cd1de0fe1209380b183b654c65e98a629637873` |
| ParseHub control pin | `144260f4349c5bb57415f7555a572fcd48c3b8ba` |
| Scope | Read-only architecture audit |
| Runtime or route change | None |
| `CURRENT_VIDEODL_INTEGRATION_LEVEL` | Isolated whole-repository Staff Lab worker |
| `ASSET_1_COMPLETENESS` | `MINIMAL` |

`MINIMAL` does not mean the current integration is unsafe or useless. The
worker has strong process isolation and safely exposes final resources. It
means that the integration retains too little resolution evidence to explain
how videodl obtained a result, which backends were tried, or why fallback
occurred.

## Document Set

- This document: current integration, fixed-pin execution chain, sampled
  clients, and target research modes.
- [Architecture and code absorption](02_VIDEODL_ARCHITECTURE_AND_CODE_ABSORPTION_V1.md)
  covers component-level reuse decisions and AnimeImages gaps.
- [Stage, lineage, and attempt RFC](03_BACKEND_STAGE_LINEAGE_AND_ATTEMPT_RFC_V1.md)
  defines the backend taxonomy and evidence vocabulary.
- [Observation and delivery RFC](04_UPSTREAM_OBSERVATION_AND_DELIVERY_RFC_V1.md)
  defines the private cross-upstream contract and projections.
- [Foundation roadmap](05_FOUNDATION_IMPLEMENTATION_ROADMAP_V1.md) sequences
  small code PRs while formal routes remain frozen.
- [Reuse and provenance matrix](06_COMPONENT_REUSE_AND_PROVENANCE_MATRIX_V1.md)
  records the detailed component disposition.

## Sources And Method

The audit read the exact pinned videodl and ParseHub repositories in temporary
detached checkouts. It used static source inspection and small AST queries. It
did not invoke a provider downloader, submit a real platform URL, fetch media,
use a Cookie, or access Production.

Primary fixed-pin evidence includes:

- videodl `videodl/videodl.py`, `videodl/modules/sources/`,
  `videodl/modules/common/`, `videodl/modules/grabber.py`, and
  `videodl/modules/utils/`;
- AnimeImages `tools/provider_lab/runtime/workers/videodl_worker.py` and
  `xhs_download/services/provider_lab/`;
- ParseHub `src/parsehub/types/` and selected parser adapters.

The videodl pin registers 70 dedicated clients and 37 common clients. Those
counts describe registry entries, not supported-production guarantees.

## Current AnimeImages Integration

The current `videodl_repo` path has these useful safeguards:

1. The repository is installed at an exact commit in a separate virtual
   environment.
2. Every parse request runs in a separate subprocess.
3. The parent passes bounded JSON over stdin/stdout, supplies a temporary
   `HOME`, XDG directories and working directory, and filters inherited
   environment variables.
4. Parent timeouts terminate and reap the subprocess group.
5. The worker configures no proxy, no Cookie, no automatic proxy discovery,
   one request retry, and disables videodl's provider downloaders.
6. Private resource URLs leave the worker in a sidecar, become short-lived
   Staff-bound resource tokens, and are absent from initial HTML.
7. The normalized result is bounded and sanitized before it reaches the Lab.

The worker calls videodl `VideoClient.parsefromurl()` in automatic mode and
projects only the final `VideoInfo` list. It currently preserves:

- exact provider version;
- final `source` as `selected_backend`;
- title and bounded raw shape;
- video, independent audio, and cover assets;
- extension and basic media metadata when available;
- `headers_present`, `requires_cookie`, and `requires_merge` booleans.

### Current Information Loss Matrix

`CURRENT_INFORMATION_LOSS_MATRIX = DOCUMENTED`

| Evidence | Present now | Lost or ambiguous consequence |
| --- | --- | --- |
| Direct-media stage | No | A direct input and a later grabber result can look alike. |
| Dedicated stage | No | A platform adapter success is not distinguishable from other backend types. |
| Common stage | No | Third-party aggregation can be mistaken for videodl-native parsing. |
| Static grabber stage | No | HTML extraction cannot be separated from browser observation. |
| Browser observer stage | No | Browser launch and network observation are invisible. |
| Attempted backend list | No | Operators cannot tell whether a backend was skipped, failed, or never reached. |
| Failed backend list | No | Fallback success erases preceding failures. |
| Failure class | No | A timeout, rate limit, unsupported URL, and parser defect collapse together. |
| Per-attempt elapsed | No | Slow backend attribution is impossible. |
| Fallback reason | No | A selected backend has no causal explanation. |
| Selected backend type | No | A class name does not declare dedicated, aggregator, or observer semantics. |
| Nested lineage | No | A dedicated client may itself call a third-party aggregator. |
| Skipped stages | No | Policy decisions cannot be audited. |
| Format inventory | No | The worker emits the selected output, not all observed variants/tracks. |
| Detailed delivery requirements | Partial | Three booleans cannot express HLS, DASH, remux, transcode, or browser eligibility. |

## Fixed-Pin Resolution Chain

The exact top-level behavior in `VideoClient.parsefromurl()` is:

```text
DIRECT MEDIA DETECTION
  -> matching DEDICATED PLATFORM CLIENTS
  -> configured COMMON VIDEO CLIENTS
  -> WEB MEDIA GRABBER
```

This high-level label hides important control-flow details.

### Stage 1: Direct Media Detection

| Property | Fixed-pin behavior |
| --- | --- |
| Entry | Always first. |
| Backend | `WebMediaGrabber.isprobablydirectmedia`. |
| URL test | A recognized media extension is accepted immediately; `m3u8` is accepted as a playlist. |
| Network test | Otherwise HEAD, then a small Range GET, with the grabber request timeout. |
| Success | A media-like content type or recognized path extension. |
| Stop | Yes; returns grabber-built `VideoInfo` immediately. |
| Error evidence | Request failures are swallowed into a false result. |
| Output | `VideoInfo` with the input URL and generated filename. |

The path-extension shortcut does not prove host safety, content identity, or
bytes. AnimeImages must not equate this detection with product admission.

### Stage 2: Dedicated Platform Clients

| Property | Fixed-pin behavior |
| --- | --- |
| Entry | Unless `apply_common_video_clients_only` is true. |
| Selection | Registered clients whose `belongto(url)` returns true. |
| Order | Literal insertion order in `VideoClientBuilder.REGISTERED_MODULES`. |
| Instantiation | Lazy, on first attempted parse. |
| Success | Any returned `VideoInfo.with_valid_download_url`. |
| Stop | First successful client stops the stage. |
| Failure evidence | A bare `except` replaces the result with an empty list. |
| Timeout | No per-client default at this orchestrator level; request behavior varies by client. |

`with_valid_download_url` checks only that the value is a YouTube stream,
existing local path, or string beginning with `http`. It does not establish
media bytes, target identity, host admission, or complete-file availability.

### Stage 3: Common Clients

| Property | Fixed-pin behavior |
| --- | --- |
| Entry | No dedicated result contains a syntactically valid download URL. |
| Selection | Every configured common client; there is no `belongto` gate. |
| Order | Literal insertion order in `CommonVideoClientBuilder.REGISTERED_MODULES`. |
| Instantiation | Lazy, immediately before the attempt. |
| Success | Any returned `VideoInfo.with_valid_download_url`. |
| Stop | First successful common client stops the stage. |
| Failure evidence | A bare `except` erases the failure. |
| Privacy | Most common clients send the target URL to an external parsing service. |

A `videodl_repo` success at this stage is the external service's result carried
through videodl. It is not evidence that videodl's own platform parser worked.

### Stage 4: Web Media Grabber

The top-level orchestrator invokes the grabber only when the final
`video_infos` value is falsey. A non-empty list containing only invalid
`VideoInfo` objects can therefore suppress this fallback even though no valid
URL was found. This is a fixed-pin control-flow edge, not a proposed behavior.

Inside the grabber:

1. requests fetches the page HTML;
2. static extraction scans `video`, `audio`, `source`, links, broad `data-*`
   attributes, scripts, and full text;
3. candidates are deduplicated and probed;
4. if none survive, headless Chromium loads and scrolls the page, observes
   media-like network traffic, and repeats static extraction;
5. surviving candidates become `VideoInfo` objects.

The static and browser sub-stages should be separate evidence stages in the
target model even though the current top-level API reports both as
`WebMediaGrabber`.

## Dedicated Client Sample

The dedicated registry shares a broad pattern: domain recognition, one or
more platform-specific requests, conversion into `VideoInfo`, and parser-local
error handling. The important variations are larger than that common shell.

| Client | Identity and requests | Result and delivery | Suitability conclusion |
| --- | --- | --- | --- |
| Bilibili | Recognizes Bilibili and short-link domains; uses Bilibili APIs. | Common video can be a complete DURL; bangumi/cheese can return separate video/audio with Referer headers. | Useful reference and Lab upstream. Current Native route remains authoritative; no route change. |
| Douyin | Recognizes `douyin.com`, follows redirects, extracts an item identity, and reads share-page state. | Produces one 1080-style video and cover, without independent audio. | Reference only; it does not replace the current Native/TianTian policy. |
| Rednote | Recognizes `xiaohongshu.com` and legacy `xhslink.com`; recursively searches state for master/backup URLs. | Video-oriented, weak gallery/LivePhoto semantics, and stale short-link coverage. | Not an identity-authoritative XHS source for formal admission. |
| Kuaishou | Recognizes Kuaishou; tries a browser parser before requests/Apollo-state fallback. | Can rank H.265/H.264 candidates. Browser cleanup is not consistently protected by a parser-local `finally`. | Lab-only whole-repository evidence until identity, process, and admission controls are reimplemented. |
| YouTube | Recognizes only YouTube host forms, but invokes named third-party aggregators before its own implementation. | Can return separate video/audio and headers. | Demonstrates why a dedicated class name is not sufficient lineage. Existing YouTube route remains unchanged. |

### Dedicated Client Reuse Strategy

- Keep the full registry available only through the isolated whole-repository
  Lab boundary for comparison.
- Treat a selected client as an observed backend, not an admitted product
  provider.
- Cleanly reimplement only a narrowly selected platform capability after
  independent identity, quality, delivery, security, and license review.
- Do not migrate XHS, Douyin, Bilibili, or YouTube routes from this audit.

```text
DEDICATED_CLIENT_COMMON_PATTERN = DOMAIN_GATE_THEN_PLATFORM_PARSE_TO_VIDEOINFO
DEDICATED_CLIENT_VARIATION_POINTS = IDENTITY_REQUEST_TRACK_HEADER_COOKIE_BROWSER_AND_DELIVERY
DEDICATED_CLIENT_REUSE_STRATEGY = ISOLATED_UPSTREAM_THEN_SELECTED_CLEAN_REIMPLEMENTATION
```

## Common Client Backend Matrix

| Client | Classification | External dependency and data flow | Output strengths | Main risk |
| --- | --- | --- | --- | --- |
| `KuKuToolVideoClient` | `AGGREGATOR_API` | Sends the target URL to the KuKuTool service after an auth/session handshake and encrypted request flow. | Final video, title, cover/pictures; platform-specific download headers. | Captcha/rate/protocol drift; opaque external selection; no attempt lineage. |
| `AnyFetcherVideoClient` | `AGGREGATOR_API` | Sends the target URL as JSON to AnyFetcher. | Format list, video/audio split, title and preview; locally selects a highest-looking format. | External availability, quality-label parsing, quota/rate policy, target URL disclosure. |
| `SnapAnyVideoClient` | `AGGREGATOR_API` | Sends the target URL to SnapAny with timestamped request signing. | Similar format and independent-audio data to AnyFetcher. | External protocol/signature drift, rate control, target URL disclosure. |
| `JXM3U8VideoClient` | `GENERIC_REMOTE_PARSER` | Sends an encoded target URL to an external parser endpoint and decrypts its response. | One URL, type, title and cover, including HLS. | Opaque service chain, hard-coded crypto protocol, no audio inventory, high stability and provenance risk. |

None of these samples is a pure local generic extraction algorithm. Account,
quota, anti-abuse, and service-policy behavior is not represented in
`VideoInfo`, so a future observation must declare the backend type and nested
service lineage explicitly.

`COMMON_CLIENT_BACKEND_MATRIX = DOCUMENTED`

## Web Media Grabber Boundary

### Capability

- direct-media path/MIME probing;
- requests-based static HTML extraction;
- common media tags and broad data-attribute discovery;
- script/text URL discovery;
- HEAD/Range candidate probing;
- Chromium page loading, auto-scroll, and network observation;
- exact-URL deduplication and Referer propagation.

### Identity And Resource Risks

- page-wide scans can capture ads, recommendations, preload media, hidden
  cards, or unrelated embeds;
- network observation lacks current-item identity binding;
- a media-looking response is not proof that it belongs to the requested post;
- `blob:` resources are rejected, so browser-only assembly is not represented;
- login state can alter observed content without being expressed in lineage;
- no AnimeImages host allowlist or private/loopback address policy is applied
  inside the upstream grabber;
- browser options ignore certificate errors;
- broad HTML and network candidates can amplify CPU, memory, subprocess, and
  network cost;
- parent process-group cleanup is helpful, but not a substitute for admission
  controls and per-substage evidence.

`WEB_MEDIA_GRABBER_RECOMMENDED_SCOPE = UPSTREAM_WORKBENCH_ONLY`

```text
WEB_MEDIA_GRABBER_CAPABILITY = DOCUMENTED
WEB_MEDIA_GRABBER_IDENTITY_RISK = HIGH_WITHOUT_ITEM_BINDING
WEB_MEDIA_GRABBER_RESOURCE_RISK = HIGH_WITHOUT_NETWORK_AND_PROCESS_SANDBOX
```

Its direct probe and extraction ideas can inform a clean implementation. Raw
browser observations must not enter a formal or public route without exact
identity binding, SSRF controls, TLS verification, bounded candidate counts,
host/format admission, and explicit policy review.

## Target Whole-Repository Modes

These are proposed Lab research modes, not implemented settings. The timeout
values are outer policy ceilings; a later code PR must make each attempt
strictly smaller than the total envelope.

| Mode | Allowed stages | Proposed max backend attempts | Browser | External aggregator | Security level | Current API support |
| --- | --- | ---: | --- | --- | --- | --- |
| `AUTO` | direct, dedicated, common, static, optional browser | 8 | Off by default; explicit Staff opt-in | Explicit Staff opt-in | Restricted Lab | Current worker uses effectively unbounded registry order inside a 25-second parent timeout. |
| `DEDICATED_ONLY` | direct plus dedicated | 4 | No | No, unless a dedicated client has hidden nested lineage | Preferred Lab comparison | Not strict: a selected dedicated set can still fall through to grabber. |
| `COMMON_ONLY` | selected common aggregators | 4 | No | Yes | Sensitive Lab | Partially exposed by `apply_common_video_clients_only`, but grabber fallback remains. |
| `GRABBER_ONLY` | direct, static, optional browser | 2 substages | Explicit | No | Restricted workbench | Indirectly possible by allowing only the grabber sentinel; not a first-class contract. |
| `SPECIFIC_BACKEND` | one declared backend | 1 | Only when that backend is the browser observer | Only when selected | Most deterministic | Backend allowlisting exists, but implicit grabber fallback prevents strict isolation. |

Every mode should accept a source reference plus fixed policy, return
`UpstreamObservationV1` and a private resource sidecar, and emit no media body.
The mode must declare its stage allowlist, browser/aggregator permission,
attempt ceiling, per-attempt timeout, and total deadline.

## ParseHub Control Conclusion

The proposed model is not video-specific. ParseHub supplies typed image,
video, multimedia, rich-text, and item-local LivePhoto references. Those map
cleanly into observation candidates and relationships. ParseHub is weaker in
independent audio, delivery flags, and format inventory; videodl is weaker in
gallery, rich text, and relationship preservation. The shared contract is
therefore useful precisely because neither upstream model is sufficient by
itself. The full mapping is in the
[Observation and delivery RFC](04_UPSTREAM_OBSERVATION_AND_DELIVERY_RFC_V1.md).

`PARSEHUB_CONTROL_COMPATIBILITY = PASS`

## Formal Route Freeze

This audit does not change or recommend immediate migration of:

- XHS video, image, or LivePhoto behavior;
- Douyin parsing;
- Bilibili Native;
- YouTube parsing or merge;
- Batch behavior;
- browser, Service Worker, archive, manifest, or server-generated delivery.

The first foundation work belongs in internal contracts and Provider Lab.
Formal admission remains platform-specific and fail-closed.
