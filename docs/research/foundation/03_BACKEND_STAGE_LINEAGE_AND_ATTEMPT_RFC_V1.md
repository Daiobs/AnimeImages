# Backend Stage, Lineage, And Attempt RFC v1

> **Current status — historical research reference**
>
> This document records the research conclusions accepted in PR #78.
> Its former contract-first implementation sequence was superseded after
> PR #79 was closed without merge by Human Architecture Decision.
>
> Nothing in this document currently authorizes a global
> `UpstreamObservationV1`, Backend Catalog, videodl Worker V2,
> Provider Lab projection, formal provider route, or production deployment.
> Any future diagnostic contract must be introduced with a real producer
> and a real consumer in the same reviewed vertical slice.
>
> `HISTORICAL_RESEARCH = YES`; `CURRENT_IMPLEMENTATION_AUTHORITY = NONE`;
> `CONTRACT_FIRST_SEQUENCE = SUPERSEDED`; `PR79 = CLOSED_WITHOUT_MERGE`;
> `REAL_PRODUCER_AND_CONSUMER_REQUIRED = YES`.
>
> See:
> [PR #79 Human Architecture Decision](https://github.com/Daiobs/AnimeImages/pull/79#issuecomment-5468104417)

## RFC Status

| Field | Value |
| --- | --- |
| Status | Research-ready, not implemented |
| Intended first scope | Internal contracts and Staff Provider Lab |
| Public API | No |
| Persistence | No |
| Automatic scoring or routing | No |
| Formal route change | No |

Navigation:

- [Whole-repository audit](01_VIDEODL_WHOLE_REPOSITORY_UPSTREAM_AUDIT_V1.md)
  supplies the fixed-pin evidence for this model.
- [Architecture absorption](02_VIDEODL_ARCHITECTURE_AND_CODE_ABSORPTION_V1.md)
  explains why AnimeImages should own these contracts.
- [Observation and delivery RFC](04_UPSTREAM_OBSERVATION_AND_DELIVERY_RFC_V1.md)
  embeds these records in `UpstreamObservationV1`.
- [Roadmap](05_FOUNDATION_IMPLEMENTATION_ROADMAP_V1.md) defines the staged
  implementation order.
- [Reuse matrix](06_COMPONENT_REUSE_AND_PROVENANCE_MATRIX_V1.md) records source
  provenance and replacement cost.

## Design Goals

1. Explain every selected result without retaining sensitive URL or credential
   values.
2. Distinguish a backend container from the backend and external service that
   actually supplied a candidate.
3. Preserve failed, skipped, and timed-out attempts instead of reporting only
   the winner.
4. Keep evidence descriptive. It must not become an automatic score or route
   planner.
5. Work for video, images, galleries, LivePhoto relationships, and rich text.
6. Remain bounded by fixed enum values, field lengths, attempt counts, and
   total output size.

## Backend Capability Taxonomy

`videodl_repo` is a backend container. It can host several backend types in a
single process and must not itself be treated as one atomic backend.

| Backend type | Purpose and input | Output | Identity authority | Trust and sandbox | Formal-route eligibility | Examples |
| --- | --- | --- | --- | --- | --- | --- |
| `DEDICATED_SOURCE_ADAPTER` | Parse one declared platform/source shape. | Metadata, candidates, tracks, relationships. | Potentially high only when exact identity is proven. | Isolated external runtime or reviewed native adapter. | Candidate after platform-specific review. | videodl Bilibili/Douyin clients; AnimeImages native adapters. |
| `AGGREGATOR_API` | Send a source reference to an external multi-platform service. | Service-selected media and metadata. | Indirect; must disclose nested service lineage. | Sensitive external data-sharing boundary and strict timeout. | Explicit provider only, never silently treated as native. | KuKuTool, AnyFetcher, SnapAny. |
| `GENERIC_STATIC_EXTRACTOR` | Inspect fetched HTML or structured page state. | Candidate references and metadata claims. | Low unless bounded to an exact item container/state. | SSRF/TLS/byte bounds and parser sandbox. | Candidate only with platform identity admission. | Static part of WebMediaGrabber. |
| `BROWSER_NETWORK_OBSERVER` | Observe browser DOM/network behavior for one page. | Observed media-like requests and page facts. | Very low without current-item binding. | Requires a dedicated browser subprocess, network sandbox, CPU/memory/deadline limits. | Blocked until the sandbox gate passes. | WebMediaGrabber Chromium listener. |
| `DIRECT_MEDIA_PROBE` | Inspect an already supplied resource reference. | MIME/signature/range/length observations. | None for post identity; only resource evidence. | Strict host/address/TLS/byte/time policy. | Supports admission but cannot establish ownership. | HEAD/Range probe. |
| `FORMAT_INVENTORY_EXTRACTOR` | Read one upstream's available formats/tracks. | URL-free variant/track inventory plus private resource refs. | Depends on parent parser. | Bounded structures; no automatic quality winner. | Useful evidence, not route authority. | ParseHub format metadata; DASH inventory. |
| `RICH_MEDIA_RELATIONSHIP_PROVIDER` | Preserve item-local media relations. | Gallery order, still/motion, content blocks. | High only with item-local evidence. | Fail-closed relationship validation. | Candidate for RichMedia admission. | ParseHub `LivePhotoRef`; XHS relationship mapper. |
| `DELIVERY_TOOL` | Materialize an already admitted output. | File/stream/archive/merge result. | No source identity authority. | Executable allowlist and resource sandbox. | Chosen only by AnimeImages delivery policy. | FFmpeg, browser archive, Service Worker. |

A backend may have more than one capability, but each attempt declares one
primary type. Nested calls create additional lineage nodes instead of silently
changing the primary type.

## ResolutionStageV1

The stage enum describes where in the resolution chain an attempt occurred.
It does not claim success or trust.

| Stage | Meaning | Typical backend type |
| --- | --- | --- |
| `direct_media` | Inspect the input itself as a possible media resource. | `DIRECT_MEDIA_PROBE` |
| `dedicated_source` | Run a backend selected by platform/source identity. | `DEDICATED_SOURCE_ADAPTER` |
| `common_aggregator` | Run a source-agnostic external parsing service. | `AGGREGATOR_API` |
| `static_extractor` | Extract candidates from bounded static content. | `GENERIC_STATIC_EXTRACTOR` |
| `browser_observer` | Observe rendered DOM or browser network behavior. | `BROWSER_NETWORK_OBSERVER` |

The fixed videodl pin exposes the first three stages at the top level and
collapses the last two under `WebMediaGrabber`. A future Worker V2 should emit
the two grabber substages separately.

## BackendLineageV1

### Purpose

Lineage records who executed, who delegated, and which external system
ultimately supplied the observation. It prevents statements such as
"videodl succeeded" from hiding that a common aggregator did the work.

### Shape

```text
BackendLineageV1
  schema_version: "1.0"
  nodes[]:
    ordinal: positive integer
    parent_ordinal: positive integer or null
    actor_id: fixed catalog ID
    actor_type: container | adapter | external_service | observer | tool
    backend_type: BackendCapabilityType or null
    provider_version: bounded non-secret version or null
    trust_scope: internal | staff_lab | formal_candidate | external
    data_shared: none | source_reference | metadata | private_resource_context
  selected_leaf_ordinal: positive integer or null
```

The catalog, not provider output, owns `actor_id`, `actor_type`, and backend
type. Unknown values fail validation rather than becoming arbitrary strings.

### URL-Free Example

```text
AnimeImages
  -> videodl_repo
    -> KuKuToolVideoClient
      -> KuKuTool service
        -> selected media observation
```

The serialized form contains those catalog IDs and relationships, not a
source URL, endpoint URL, media URL, request body, header, or Cookie.

## AttemptTraceV1

### Record Shape

```text
AttemptTraceV1
  ordinal: positive integer
  stage: ResolutionStageV1
  backend_id: fixed catalog ID
  backend_type: BackendCapabilityType
  started_offset_ms: non-negative integer
  elapsed_ms: non-negative bounded integer
  outcome: success | failed | timeout | skipped | policy_rejected
  failure_class: fixed enum or null
  asset_count: non-negative bounded integer
  format_count: non-negative bounded integer
  relationship_count: non-negative bounded integer
  evidence_stage: EvidenceStageV1
  relationship_evidence: RelationshipEvidenceV1
  fallback_reason: fixed enum or null
  lineage_leaf_ordinal: positive integer or null
```

`started_offset_ms` is relative to the request's monotonic start. It is not a
wall-clock timestamp and cannot reveal local scheduling details.

### Failure Classes

| Failure class | Meaning |
| --- | --- |
| `NOT_APPLICABLE` | Backend identity gate did not match. Normally represented as skipped. |
| `UNSUPPORTED_IDENTITY` | Backend recognized the platform family but not the exact input form. |
| `TIMEOUT` | Per-attempt deadline expired. |
| `NETWORK_FAILURE` | Bounded network operation failed without a more specific safe class. |
| `HTTP_REJECTED` | Upstream returned a non-success response not classified below. |
| `AUTH_REQUIRED` | Required authentication was unavailable. |
| `RATE_LIMITED` | A fixed safe signal identified throttling. |
| `PARSE_EMPTY` | Backend completed without media or useful metadata. |
| `RESULT_INVALID` | Output failed schema or structural validation. |
| `IDENTITY_MISMATCH` | Returned content was not proven to be the requested item. |
| `POLICY_REJECTED` | Host, format, relationship, browser, or data-sharing policy rejected it. |
| `RESOURCE_UNAVAILABLE` | A claimed private resource failed bounded inspection. |
| `PROCESS_TERMINATED` | Parent terminated the isolated process. |
| `INTERNAL_ERROR` | Sanitized final class for unclassified backend defects. |

No raw exception text accompanies these classes. Detailed debugging remains
inside an explicitly controlled local research session and is never copied to
ordinary logs or result contracts.

### Fallback Reasons

Allowed values are descriptive and fixed:

- `NO_APPLICABLE_BACKEND`
- `NO_VALID_MEDIA`
- `BACKEND_FAILED`
- `BACKEND_TIMED_OUT`
- `RESULT_REJECTED`
- `MODE_CONTINUATION`
- `HIGHER_AUTHORITY_REQUIRED`
- `NONE`

An attempt that succeeds can still be followed by another only when the
selected research mode explicitly allows continuation, for example to obtain
missing relationship or format evidence. This is not automatic scoring.

### Bounds

A contract implementation should enforce at least:

- a mode-specific maximum attempt count;
- bounded ID and enum values;
- bounded non-negative timing and count fields;
- one trace entry per actual or policy-significant skipped attempt;
- monotonically increasing ordinal and start offset;
- no duplicate successful winner within one resolution branch;
- a total serialized observation limit enforced by the parent process.

## Independent Evidence And Admission Dimensions

Evidence, relationships, route admission, deployment, and license scope are
independent. They must not be compressed into a single monotonic support
ladder. A provider can have complete media evidence while remaining
route-rejected, or preserve a relationship while full-media verification is
not applicable.

### EvidenceStageV1

This dimension reports the strongest media evidence established for the
observation. It does not grant route or deployment authority.

| Stage | Evidence |
| --- | --- |
| `DETECTED` | Platform/backend family recognized; no useful metadata or media required. |
| `METADATA_ONLY` | Bounded metadata or format labels exist, but no structurally valid private candidate exists. |
| `MEDIA_CANDIDATES` | At least one structurally valid private resource reference has media type and role. |
| `BROWSER_INSPECTABLE` | A tokenized candidate passed bounded browser/request inspection; full file is not implied. |
| `FULL_MEDIA_VERIFIED` | Complete bytes or another platform-appropriate completeness proof matched expected evidence in an authorized test. |

### RelationshipEvidenceV1

| Value | Meaning |
| --- | --- |
| `NOT_APPLICABLE` | The result has no relationship claim that requires validation. |
| `CLAIMED` | A provider claims order/group/still-motion/content-block relationships, but direct evidence is insufficient. |
| `PRESERVED` | Item-local or explicit payload relationships validate without URL-similarity or array-position inference. |
| `FAILED` | A required relationship is malformed, mismatched, or cannot be preserved fail-closed. |

### RouteAdmissionStateV1

| Value | Meaning |
| --- | --- |
| `UNREVIEWED` | No platform-specific route decision has been made. This is the default for upstream evidence. |
| `REJECTED` | Platform identity, host, format, relationship, safety, license, delivery, or reliability review rejected formal use. |
| `ROUTE_CANDIDATE` | Evidence is sufficient to enter an explicit platform route review; no formal route exists yet. |
| `FORMAL_ROUTE_ADMITTED` | A separate reviewed route decision admits the fixed upstream/backend under explicit policy. |

Only AnimeImages admission can set this dimension. An upstream worker cannot
self-assert it.

### DeploymentStateV1 And License Scope

`DeploymentStateV1` is either `NOT_DEPLOYED` or `DEPLOYED`. It records release
state only and does not imply media quality or route admission. License scope
is a separate reviewed field, such as `noncommercial_lab`,
`formal_use_reviewed`, or another fixed project-owned value. Neither field is
derived from evidence stage.

### Existing Case Calibration

| Case | Evidence | Relationships | Route admission | Deployment / license note |
| --- | --- | --- | --- | --- |
| ParseHub Bilibili format metadata | `METADATA_ONLY` | `NOT_APPLICABLE` | `UNREVIEWED` for any new route | No route or deployment change. |
| videodl Bilibili complete media | `FULL_MEDIA_VERIFIED` for the specific Staff Lab evidence | `NOT_APPLICABLE` | `REJECTED` as a replacement because no quality gain was proven | Current use remains noncommercial Lab evidence and not deployed as a formal upstream. |
| TianTian XHS Ultra | Route-specific evidence, recorded separately from this Lab model | `NOT_APPLICABLE` | `FORMAL_ROUTE_ADMITTED` only through the existing XHS policy | Deployment is an independent release fact. |
| ParseHub XHS raw image | Verified-subset evidence under its route-specific tests | `NOT_APPLICABLE` | `FORMAL_ROUTE_ADMITTED` only for the reviewed subset | Exact host/format/download gates remain independent. |
| XHS Staff LivePhoto | At least `MEDIA_CANDIDATES` | `PRESERVED` | `UNREVIEWED` for download and Public LivePhoto route expansion | Staff preview admission does not grant download authority. |

`FULL_MEDIA_VERIFIED` does not imply `FORMAL_ROUTE_ADMITTED`, and
`FORMAL_ROUTE_ADMITTED` does not imply `DEPLOYED`.

## Attempt State In Each Mode

| Mode | Trace requirements |
| --- | --- |
| `AUTO` | Record direct probe, every entered dedicated/common backend, policy-significant skips, and the grabber stages as blocked until the sandbox gate. |
| `DEDICATED_ONLY` | Record direct probe and selected dedicated backends; common/static/browser stages are skipped with fixed policy reason. |
| `COMMON_ONLY` | Record each selected aggregator and its nested external-service lineage; dedicated and grabber stages are policy-skipped. |
| `GRABBER_ONLY` | Blocked until a separate sandbox gate proves filesystem, private-network, loopback, cloud-metadata, and resource-limit controls. Staff opt-in alone is insufficient. |
| `SPECIFIC_BACKEND` | Record exactly one permitted backend attempt plus its internal lineage; no implicit fallback, and browser observer remains sandbox-gated. |

The current videodl API cannot provide these strict semantics for every mode,
so the future worker must instrument or wrap stage entry rather than infer a
trace from the final `VideoInfo`.

## Security And Privacy Rules

Lineage and trace must never contain:

- source, expanded, API, or media URLs;
- signed query keys or values;
- Cookie or authorization material;
- header values;
- provider request/response bodies;
- raw exception text or stack traces;
- local filesystem paths;
- user input or titles used as identifiers.

Catalog IDs and fixed enum values are safe. Bounded elapsed/count fields are
safe. A request ID may be included only if it is already the AnimeImages
opaque request identifier.

## Validation Invariants

1. Every trace backend exists in the backend catalog.
2. Every trace lineage leaf resolves to a node with the same backend type.
3. The selected backend references a successful attempt.
4. A failed/timeout attempt cannot report admitted assets.
5. A skipped attempt has zero elapsed time unless policy evaluation itself is
   represented separately.
6. A nested external service must appear as a lineage node whenever the target
   source reference is shared externally.
7. Browser observations never claim identity authority by themselves.
8. Evidence stage and relationship evidence are derived from validated facts,
   not accepted from an untrusted worker field.
9. Route admission, deployment state, and license scope are independent
   project-owned decisions and are never inferred from evidence stage.
10. Traces are request-scoped and never stored in global state keyed by source
   URL.
11. Projection to public/runtime assets discards attempt trace and lineage.

## RFC Decision

`VIDEODL_RESOLUTION_STAGE_MODEL = READY`

`VIDEODL_BACKEND_TAXONOMY = READY`

`BACKEND_LINEAGE_RFC = READY`

`ATTEMPT_TRACE_RFC = READY`

`EVIDENCE_STAGE_MODEL = READY`

`RELATIONSHIP_EVIDENCE_MODEL = READY`

`ROUTE_ADMISSION_MODEL = READY`

`LINEAR_SUPPORT_LADDER = REMOVED`

Implementation must begin as internal URL-free dataclasses and validation
tests. Worker instrumentation and Provider Lab projection are later PRs.
