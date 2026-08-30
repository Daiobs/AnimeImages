# Component Reuse And Provenance Matrix v1

## Scope And License Record

This matrix applies to CharlesPikachu/videodl at
`6cd1de0fe1209380b183b654c65e98a629637873`. The repository is licensed under
PolyForm Noncommercial 1.0.0. Current AnimeImages use is recorded as
noncommercial. This research PR copies no videodl source code.

For any future `DIRECT_REUSE` or `ADAPTED_REUSE` decision, the implementing PR
must retain source provenance, include applicable license/required notices,
record modifications, and document replacement cost before any future
commercialization. A clean reimplementation must be reviewed to ensure it is
actually independent rather than untracked derivative copying.

Companion documents:

- [Whole-repository audit](01_VIDEODL_WHOLE_REPOSITORY_UPSTREAM_AUDIT_V1.md)
- [Architecture absorption](02_VIDEODL_ARCHITECTURE_AND_CODE_ABSORPTION_V1.md)
- [Stage, lineage, and attempt RFC](03_BACKEND_STAGE_LINEAGE_AND_ATTEMPT_RFC_V1.md)
- [Observation and delivery RFC](04_UPSTREAM_OBSERVATION_AND_DELIVERY_RFC_V1.md)
- [Foundation roadmap](05_FOUNDATION_IMPLEMENTATION_ROADMAP_V1.md)

## Reuse Matrix

| Component | Primary value | Recommendation | AnimeImages target | Required adaptation | Security/operational risk | External dependency | Provenance requirement | Future replacement cost |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| Whole repository | Broad real-world parser/backend catalog and delivery cases | `WHOLE_REPOSITORY_UPSTREAM` | Isolated Staff Provider Lab worker | Add modes and observation evidence in later PRs; keep exact pin and parse-only boundary | Large transitive surface; opaque nested services; process/network cost | Entire videodl environment | Record pin, license, worker isolation and no copied code | Medium: replace worker/provider evidence adapter, not formal routes |
| `VideoClient` orchestration | Ordered direct/dedicated/common/grabber fallback | `CLEAN_REIMPLEMENTATION` | Future Lab resolution runner | Strict modes, per-attempt deadlines, typed catalog, trace every failure/skip, no implicit fallback | Bare exceptions, erased attempts, non-empty invalid result can suppress grabber | Registered clients and grabber | Cite fixed-pin behavior as design evidence | Medium |
| `BaseModuleBuilder` | Simple registry/build interface | `CLEAN_REIMPLEMENTATION` | Immutable backend catalog | Frozen descriptors, explicit validation, no global mutation or assertions as policy | Test bleed, thread unsafety, import-time side effects | Python imports | Preserve audit provenance only | Low |
| `VideoClientBuilder` | Ordered dedicated registry | `CLEAN_REIMPLEMENTATION` | Dedicated backend catalog data | Add type/trust/identity/browser/data-sharing/timeouts and exact pin | Order drift changes behavior; eager imports | All dedicated client modules | Catalog generated/reviewed from fixed pin | Medium |
| `CommonVideoClientBuilder` | Ordered aggregator registry | `CLEAN_REIMPLEMENTATION` | Aggregator backend catalog data | Declare external services and source-reference sharing | Third-party privacy/quota/protocol drift hidden by class name | External parsing services | Record service lineage and fixed-pin class | Medium to high |
| `VideoInfo` | Compact final parser/downloader exchange object | `CLEAN_REIMPLEMENTATION` | `UpstreamObservationV1` plus private sidecar | Split facts, tracks, delivery requirements, private values and final assets | Mixes raw data, credentials, paths and arbitrary executor settings | Parser and downloader layers | Field mapping retained in audit | Medium |
| `BaseVideoClient` | Comprehensive HTTP, parse and delivery case library | `REJECT` | None as a whole | Extract only reviewed concepts into separate pure modules | Giant trust boundary; URL logging; TLS-disable fallback; provider command settings; server media transfer | requests, FFmpeg, external tools, filesystem | Do not copy class | High |
| Dedicated clients | Platform-specific identity/request knowledge | `WHOLE_REPOSITORY_UPSTREAM`; selected future `CLEAN_REIMPLEMENTATION` | Staff Lab evidence; later platform-scoped adapter only | Independent identity, host, format, deadline, error and delivery review | Highly variable quality; raw errors; browser use; nested aggregators | Platform APIs/pages and optional browsers | Per-client pin/path and behavior evidence | High per client |
| Common clients | External multi-platform parsing coverage | `WHOLE_REPOSITORY_UPSTREAM` | Staff Lab explicit aggregator attempts | Privacy/terms/quota review, typed lineage, strict limit and timeout | Target URL sent externally; opaque protocol/service; rate and availability drift | KuKuTool, AnyFetcher, SnapAny and other services | Name external service and source sharing | High |
| WebMediaGrabber direct probe | Fast media-like resource detection | `CLEAN_REIMPLEMENTATION` | Safe internal resource observation | AnimeImages SSRF/TLS/host/byte/time policy; separate detection from identity/admission | Extension false positives, redirect/address changes, unrestricted hosts | HTTP client | Cite fixed-pin concept, write independent tests | Low to medium |
| WebMediaGrabber static extraction | Broad media candidate discovery | `CLEAN_REIMPLEMENTATION` only for bounded cases | Platform-scoped static extractor | Exact container/state identity, candidate cap, safe parser, host admission | Ads/recommendations/preloads and full-page noise | HTML parser and HTTP | Fixed-pin extraction list as reference | Medium |
| WebMediaGrabber browser observer | Network/DOM observation after rendering | `REJECT` for formal route; `WHOLE_REPOSITORY_UPSTREAM` workbench only | Future isolated observer only after separate review | Dedicated sandbox, exact browser, network policy, TLS, CPU/memory/process limits, identity binding | Arbitrary page execution; certificate bypass; resource/process cost; unrelated media | Chromium/DrissionPage | Record fixed-pin behavior and workbench scope | Very high |
| `FileTypeSniffer` | Path/MIME/magic hints with method provenance | `CLEAN_REIMPLEMENTATION` | Existing/new pure media signature helper | Remove network I/O, narrow supported formats, distinguish declared versus detected, fix Range-byte defect | MIME trust, unbounded hosts/timeouts, broad formats | filetype/puremagic/requests | Retain concept provenance if materially derived | Low |
| Parse/download request decorators | Concise session-context switching | `CLEAN_REIMPLEMENTATION` | Immutable request context | No shared mutable client state; explicit header/Cookie capabilities | Session reinitialization and concurrency state bleed | HTTP session | Audit reference only | Low |
| Domain helpers | Normalized host and suffix matching | Use existing AnimeImages equivalent | `host_matches`/blocked-host policy | None from videodl; keep private-address and platform policy | Public suffix extraction alone is not SSRF defense | tldextract in videodl | No code reuse needed | Low |
| HLS master parsing | Variant and subtitle inventory | `CLEAN_REIMPLEMENTATION` | Pure format inventory parser | No fetch inside parser; input/variant bounds; safe URI references; tests | Fixed pin includes TLS-disabled fetch and raw URL diagnostics | HTTP/urllib in source implementation | Document conceptual source | Medium |
| HLS segment probing/writer | Candidate host latency selection and VOD construction | `REJECT` for initial foundation | None initially | Would need full SSRF/TLS/host/resource policy and delivery ownership | High concurrency, raw URL output, server media data plane | requests/filesystem | Do not copy | High |
| Structured command builder | Non-shell argv construction | `CLEAN_REIMPLEMENTATION` | Existing/future external-tool command schema | Executable/option allowlist, immutable typed args, output path ownership | Arbitrary provider modifiers could become command injection/control expansion | FFmpeg/N_m3u8DL-RE/aria2c | Design provenance and independent implementation review | Medium |
| FFmpeg planning | Separate download/remux/audio-transcode/full-transcode/copy plans | `CLEAN_REIMPLEMENTATION` | Server-generated delivery planner | Derive from admitted `DeliveryRequirementV1`; resource limits and output verification | CPU/disk exhaustion, unsafe inputs, provider-owned arguments | FFmpeg/ffprobe | Preserve design provenance if adapted | Medium to high |
| N_m3u8DL-RE integration | Segmented media acquisition | `REJECT` from automatic product route | None initially | Separate future server-generated-output review only | Binary supply, command surface, server bandwidth and temporary media | N_m3u8DL-RE | No reuse now | High |
| aria2c integration | Parallel server-side acquisition | `REJECT` from automatic product route | None initially | Separate future server-generated-output review only | Server data plane, command surface, resource amplification | aria2c | No reuse now | Medium to high |
| YouTube stream special case | Handles non-string upstream stream objects | `REJECT` as generic contract | Existing AnimeImages YouTube route | Express tracks and merge requirements through standard observation; no opaque object crossing process | Object-specific behavior and downloader coupling | videodl YouTube implementation | No reuse | High |
| File naming and temporary paths | Generates output paths per source/title | `REJECT` across provider boundary | AnimeImages delivery-owned naming | Provider supplies bounded metadata only; delivery owns safe filenames and temp roots | Traversal/collision/private path leakage | Filesystem | No reuse | Low |
| Progress and parallel download | User feedback and concurrent server downloads | `REJECT` for ordinary upstream parsing | Existing AnimeImages browser/server-generated progress | Keep transport-specific progress in delivery layer | Server bandwidth, misleading product progress, thread amplification | requests/thread pool/tools | Historical reference only | Medium |
| Chromium utilities | Browser discovery/startup/proxy/temp cleanup | `REJECT` for direct runtime reuse | Dedicated future observer process, if approved | Exact binary/runtime, no auto-download, sandbox and bounded process lifecycle | Environment drift, binary download, proxy credentials, process residue | DrissionPage/Chrome | Do not copy wholesale | Very high |
| Daily validation scripts | Large sample catalog and pass/fail reporting concepts | `DEFERRED_CONSUMER_REFERENCE` | Future provider health consumer | Consume standardized observation/attempt fields; remove real URLs from repository-facing evidence | Quota, sample expiry, secrets, false health signals, media downloads | Many live platforms and downloader | Cite as reference only | High |

## Direct Reuse Finding

`DIRECT_REUSE` is intentionally empty. This is not a blanket license
prohibition. AnimeImages already has stronger security boundaries for many
small helpers, while the remaining components mix concerns or depend heavily
on videodl internals. Cleanly reimplementing narrow concepts produces a
smaller, testable ownership boundary and lowers future replacement cost.

## Provenance Record Template

Any future adapted or clean implementation materially informed by videodl
should record:

```text
Upstream repository: CharlesPikachu/videodl
Reviewed pin: 6cd1de0fe1209380b183b654c65e98a629637873
Source component/path: <bounded list>
Absorption decision: ADAPTED_REUSE | CLEAN_REIMPLEMENTATION
License/notice handling: <reviewed result>
AnimeImages modifications: <bounded summary>
Security differences: <bounded summary>
Replacement boundary: <module and tests>
```

Do not place source/media URLs, test credentials, private headers, or copied
upstream payloads in that record.

## Asset 3 Boundary

Daily validation scripts are recorded only as
`DEFERRED_CONSUMER_REFERENCE`. A future health system may consume provider,
backend, resolution/support stage, elapsed time, asset/audio completeness,
delivery requirement, failure class, and lineage. Scheduling, timers, email,
UI, and persistence are outside this audit.

## Matrix Decision

`ASSET_2_REUSE_MATRIX = COMPLETE`

The near-term reusable asset is the evidence taxonomy and test cases. The
whole repository remains an isolated upstream; safe components are absorbed
only through small, explicit, independently reviewable implementations.
