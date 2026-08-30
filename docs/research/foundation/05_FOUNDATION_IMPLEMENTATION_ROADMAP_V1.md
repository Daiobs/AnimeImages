# Foundation Implementation Roadmap v1 (Superseded Contract-First Sequence)

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

## Current Implementation Decision

```text
ROADMAP_STATUS = SUPERSEDED
PR79_STATUS = CLOSED_WITHOUT_MERGE
PR79_MERGED = NO
CONTRACT_FIRST_SEQUENCE = SUPERSEDED
GLOBAL_UPSTREAM_OBSERVATION_KERNEL = NOT_ADOPTED
BACKEND_CATALOG = PAUSED
VIDEODL_WORKER_V2 = PAUSED
PROVIDER_LAB_PROJECTION = NOT_AUTHORIZED
NEXT_CODE_PR = NOT_SELECTED
PRODUCTION_CHANGE = NONE
```

## Scope

This roadmap preserves the implementation sequence proposed by the read-only
videodl audit as historical research. The sequence is no longer an authorized
set of foundation PRs. A future diagnostic contract requires a real producer
and a real consumer in the same reviewed vertical slice.

Reference set:

- [Whole-repository audit](01_VIDEODL_WHOLE_REPOSITORY_UPSTREAM_AUDIT_V1.md)
- [Architecture absorption](02_VIDEODL_ARCHITECTURE_AND_CODE_ABSORPTION_V1.md)
- [Stage, lineage, and attempt RFC](03_BACKEND_STAGE_LINEAGE_AND_ATTEMPT_RFC_V1.md)
- [Observation and delivery RFC](04_UPSTREAM_OBSERVATION_AND_DELIVERY_RFC_V1.md)
- [Reuse and provenance matrix](06_COMPONENT_REUSE_AND_PROVENANCE_MATRIX_V1.md)

## Former Sequencing Principles (Superseded)

The following principles and PR 1-PR 6 slices record the sequence proposed in
PR #78. They are retained for research context and do not select or authorize
future implementation work.

1. Contracts precede worker instrumentation.
2. Worker evidence precedes UI projection.
3. Lab evidence never auto-promotes a formal provider.
4. Private resources use a request-bound Worker sidecar, short-lived Django
   Cache, and opaque token; they never enter the normal observation.
5. Every PR is migration-free and reversible by code revert.
6. XHS, Douyin, Bilibili, YouTube, Batch, and download delivery stay frozen.
7. Continuous provider health is not a prerequisite for Assets 1 or 2.

## Historical Proposal PR 1: Minimal Upstream Observation Kernel

**Title:** Add minimal upstream observation kernel v1

**Goal**

Implement only the immutable, pure kernel contracts and validation for:

- `ResolutionStageV1`;
- `BackendCapabilityTypeV1`;
- `BackendLineageNodeV1`;
- `AttemptTraceV1`;
- `EvidenceStageV1`;
- `RelationshipEvidenceV1`;
- `AcquisitionRequirementV1`;
- `AssemblyRequirementV1`;
- minimal `UpstreamObservationV1` containing only those records and bounded
  metadata/counts needed by the URL-free fixtures.

Explicitly defer the full format-variant graph, full track graph,
`DeliveryDispositionV1` runtime, Private Resource Channel implementation,
Worker wiring, Provider Lab UI, persistence, database, settings, formal route,
and deployment-state logic.

**Allowed files**

- a new internal service module under `xhs_download/services/`;
- focused pure unit tests;
- minimal package exports if required.

**Forbidden scope**

- provider workers and Provider Lab views/templates;
- platform adapters and provider order;
- settings, models, migrations, dependencies, workflows;
- Private Resource Channel or resource-token behavior;
- download or Batch behavior.

**Runtime behavior**

None. No existing call site constructs or consumes the contracts.

**Migration / dependency**

None / none.

**Risk**

Low. Primary risks are an overbroad schema, accidental URL-bearing fields,
and accepting provider-owned admission state.

**Tests**

- fixed enum and bound validation;
- URL/header/Cookie/path rejection;
- lineage-node integrity;
- attempt ordering and selected-winner invariants;
- evidence-stage and relationship-evidence derivation;
- no route, deployment, license, or delivery-disposition authority;
- complete MP4, separate A/V, and LivePhoto URL-free fixtures;
- deterministic serialization and size bounds.

**Human gate**

Architecture review confirms the contracts are internal, URL-free, generic
across videodl and ParseHub, and do not alter runtime.

**Rollback**

Revert the isolated contract/test commit. No data or runtime cleanup.

**Exit criteria**

The minimal contracts validate URL-free complete MP4, separate video/audio,
and LivePhoto still/motion fixtures; all negative security fixtures fail
closed; full existing tests remain unchanged.

## Historical Proposal PR 2: Explicit Upstream Backend Catalog

**Title:** Add immutable Provider Lab upstream backend catalog v1

**Goal**

Define reviewed backend metadata for the current exact-pin videodl stages and
selected backends: stage, type, trust scope, external data sharing, browser
use, identity authority, timeout ceiling, and allowed research modes.

**Allowed files**

- internal catalog module;
- pure catalog validation tests;
- Provider Lab service-layer imports only if they remain unused at runtime.

**Forbidden scope**

- dynamic discovery or arbitrary Git repository loading;
- route/provider registration;
- changing current worker invocation;
- UI or settings flags.

**Runtime behavior**

None until Worker V2 consumes the catalog.

**Migration / dependency**

None / none.

**Risk**

Low to medium. Catalog drift could mislabel a backend, so exact pin and test
fixtures must be coupled.

**Tests**

- unique stable backend IDs;
- every stage/type is fixed and valid;
- nested aggregators declare external lineage/data sharing;
- browser backends cannot be enabled by non-Staff/default policy;
- catalog pin matches the worker pin.

**Human gate**

Review the complete initial catalog and confirm no entry is formal-route
eligible by default.

**Rollback**

Revert catalog and tests; no runtime state.

## Historical Proposal PR 3: videodl Worker V2 Evidence And Modes

**Title:** Emit bounded videodl resolution attempts in Provider Lab

**Goal**

Add explicit `AUTO`, `DEDICATED_ONLY`, `COMMON_ONLY`, and `SPECIFIC_BACKEND`
worker modes and emit observation, lineage, and attempt evidence from the
exact-pin whole-repository subprocess. `GRABBER_ONLY` remains a catalogued but
blocked mode until the separate sandbox gate passes.

The mechanism is an `ANIMEIMAGES_OWNED_VIDEODL_ORCHESTRATOR`. Strict modes
must not call `VideoClient.parsefromurl()`. The Worker uses the pinned
`VideoClientBuilder`, `CommonVideoClientBuilder`, and `WebMediaGrabber`
components directly while AnimeImages owns:

- mode gates and reviewed backend order;
- lazy backend construction;
- per-attempt timing and fixed failure classes;
- fallback/continuation decisions;
- attempt limits and total deadline;
- selected backend and lineage emission.

It must not modify the upstream checkout or use global Registry monkeypatching
as a long-term control mechanism. `WebMediaGrabber` remains unreachable in
every mode until the sandbox gate passes; its fixed-pin component is named here
to define future instrumentation ownership, not to authorize execution.

**Allowed files**

- videodl Provider Lab worker and worker-common protocol;
- internal instrumentation/adapter code owned by AnimeImages;
- focused subprocess lifecycle and sanitization tests.

**Forbidden scope**

- modifying the upstream checkout;
- provider downloads;
- formal provider routes or automatic selection;
- public UI;
- arbitrary backend IDs or command arguments.
- enabling `GRABBER_ONLY` before the sandbox gate.

**Runtime behavior**

Only an explicitly enabled Staff Lab request changes. Default-off behavior,
one-request/one-process bounded lifecycle, output limits, no Cookie/proxy, and
parent process cleanup remain. This is not a full filesystem/network sandbox
and does not provide private-network, loopback, cloud-metadata, or CPU/memory/
process-count rlimit enforcement.

```text
CURRENT_EXECUTION_ISOLATION = BOUNDED_SUBPROCESS_NOT_SANDBOX
```

**Migration / dependency**

None / none beyond the existing exact-pin isolated Lab environment.

**Risk**

Medium. Instrumentation can accidentally misstate attempts or exceed the
deadline. The implementation must observe stage entry directly rather than
infer it from final `VideoInfo`.

**Tests**

- each strict mode blocks disallowed stages;
- per-attempt and total deadlines;
- first-success and continuation semantics;
- common backend external-service lineage;
- deterministic blocking of `GRABBER_ONLY` until the sandbox gate;
- timeout/process-tree cleanup;
- no sensitive URL/header/Cookie/error/path leakage;
- current final assets remain byte-for-byte contract-compatible where
  applicable.

**Human gate**

Staff Lab runs synthetic/offline fixtures for each mode and visually verifies
attempt order without real media downloading.

**Rollback**

Disable V2 path and revert worker changes; V1 final-result worker remains.

## Historical Proposal PR 4: Provider Lab Observation Projection

**Title:** Project upstream observation evidence into Staff Provider Lab

**Goal**

Render sanitized stage, backend type, evidence stage, relationship evidence,
lineage, attempt outcomes, acquisition/assembly facts, and tokenized
candidates while keeping independent provider cards and failure isolation.

**Allowed files**

- Provider Lab projection/service layer;
- Staff-only template/JavaScript/CSS if necessary;
- focused Django/Node tests and docs.

**Forbidden scope**

- public output;
- provider winner/ranking UI;
- persistence;
- formal route or download authorization;
- raw resource URLs or credential context.

**Runtime behavior**

Only Staff Lab presentation changes when the existing flags are enabled.

**Migration / dependency**

None / none.

**Risk**

Medium. Main risk is leaking private fields through JSON/DOM or making trace
evidence look like automatic ranking.

**Tests**

- anonymous/non-Staff remains 404;
- initial HTML and JSON contain no raw resource URL;
- bounded trace rendering;
- opaque resource token remains Staff/request/asset scoped;
- independent provider failure remains isolated;
- no route selection or persistence.

**Human gate**

Staff browser review checks readability, no raw values in DOM/network payloads,
and no product-route controls.

**Rollback**

Revert projection; worker evidence can remain unused.

## Historical Proposal PR 5: ParseHub Observation Adapter

**Title:** Normalize ParseHub Lab results through UpstreamObservationV1

**Goal**

Use the same observation contract for ParseHub images, video, multimedia,
rich text, and LivePhoto relationships. Preserve the existing exact pin,
Cookie/proxy `None`, parse-only behavior, resource tokens, and Provider Lab
scope.

**Allowed files**

- ParseHub Provider Lab worker/normalizer;
- observation projection code;
- URL-free typed fixtures and focused tests.

**Forbidden scope**

- ParseHub downloader;
- formal XHS ParseHub Runtime changes;
- XHS route, LivePhoto product behavior, or download authority;
- new platform support.

**Runtime behavior**

Only Staff Provider Lab normalized evidence changes. Existing formal ParseHub
XHS integration is a separate capability and remains unchanged.

**Migration / dependency**

None / none.

**Risk**

Medium. Rich relationships must remain item-local and fail closed; the Lab
worker must not be confused with the formal XHS runtime.

**Tests**

- every ParseHub result/ref type maps;
- gallery order and LivePhoto relation preservation;
- no array-position or URL-similarity pairing;
- missing independent audio/format fields remain explicitly unknown;
- no private value leakage.

**Human gate**

Staff Lab control samples demonstrate the same observation vocabulary for
video, galleries, and LivePhoto without route changes.

**Rollback**

Restore the existing direct `LabProviderResultV1` normalizer.

## Historical Proposal PR 6: Selected Safe Helper Reimplementation

**Title:** Add pure media observation helpers for admitted upstream resources

**Goal**

Only after contracts and evidence are stable, cleanly reimplement selected
pure concepts: MIME/signature normalization, HLS master parsing, and structured
assembly-fact handling. Reuse existing AnimeImages security helpers first.

**Allowed files**

- pure helper modules and tests;
- explicit provenance documentation.

**Forbidden scope**

- browser observer;
- disabled TLS;
- downloader/external command execution;
- arbitrary FFmpeg/aria2c/N_m3u8DL-RE arguments;
- formal route wiring.

**Runtime behavior**

None until a separately reviewed consumer adopts the helper.

**Migration / dependency**

None / none.

**Risk**

Low if pure and unused; higher if network or execution sneaks into scope.

**Tests**

- signature/MIME mismatch;
- bounded HLS variant parsing;
- no URL fetch in pure helpers;
- no command execution;
- provenance and notice checks.

**Human gate**

Security review verifies no network, process, or filesystem side effects.

**Rollback**

Revert unused helpers.

## Former Recommended Next Code PR (Superseded)

```text
NEXT_CODE_PR = NOT_SELECTED
FORMER_NEXT_CODE_PR = ADD_MINIMAL_UPSTREAM_OBSERVATION_KERNEL_V1
FORMER_RECOMMENDATION_STATUS = SUPERSEDED
```

The following precise slice is retained only as the former PR #78 proposal. It
is not current implementation authorization:

> Add immutable, pure `ResolutionStageV1`, `BackendCapabilityTypeV1`,
> `BackendLineageNodeV1`, `AttemptTraceV1`, `EvidenceStageV1`,
> `RelationshipEvidenceV1`, `AcquisitionRequirementV1`,
> `AssemblyRequirementV1`, and minimal `UpstreamObservationV1` contracts with
> bounded URL-free complete-MP4, separate-A/V, and LivePhoto fixtures. Do not
> add full format/track graphs, delivery-disposition runtime, private resource
> transport, Worker wiring, Provider Lab UI, routes, downloads, settings,
> persistence, database logic, deployment-state logic, or migrations.

Historical rationale for placing this first:

- it directly improves both whole-repository integration and architecture
  absorption;
- it has no runtime behavior;
- it does not depend on provider health monitoring;
- it gives Worker V2 and ParseHub one reviewed target;
- it can be reverted without state or operational cleanup.

Historical exit criteria:

- the three examples in the
  [Observation RFC](04_UPSTREAM_OBSERVATION_AND_DELIVERY_RFC_V1.md) validate;
- sensitive strings and URL-bearing fields fail closed;
- lineage/attempt invariants pass;
- full existing test counts do not regress;
- code review confirms zero route/runtime wiring.

## Formal Route Freeze

The roadmap's first foundation PRs must not alter:

- XHS video, image, or LivePhoto routes;
- Douyin route or Provider order;
- Bilibili Native;
- YouTube/merge;
- Batch parsing;
- browser direct, Service Worker, archive, manifest, merge, or recovery
  delivery behavior.

Any eventual formal route use requires a separate platform-specific evidence,
admission, security, operational, license, and Human Gate after the foundation
work. A reviewed specific backend may remain behind an
`ISOLATED_FORMAL_UPSTREAM`; a clean reimplementation is not mandatory, but
automatic backend traversal and first-success product routing remain banned.

## Historical Asset 3 Deferred Interface

`CONTINUOUS_PROVIDER_HEALTH_MONITOR = DEFERRED`

A future health consumer may read only standardized provider/backend,
resolution stage, evidence stage, relationship evidence, elapsed time, asset
count, audio completeness, acquisition/assembly facts, failure class, and
lineage fields. Route admission, deployment, and license remain independent
project-owned dimensions.

`CURRENT_PROVIDER_HEALTH_TEST = FUTURE_CONSUMER_OF_OBSERVATION_AND_ATTEMPT_EVIDENCE`

No schedule model, timer, email, report UI, or database change is designed in
this audit.

## Historical Roadmap Decision (Superseded)

`ASSET_1_TARGET_MODEL = RESEARCH_REFERENCE_ONLY`

`VIDEODL_MODE_MODEL = RESEARCH_REFERENCE_ONLY`

The former sequence was deliberately contract-first and Lab-first. It is
preserved to explain the PR #78 research conclusions, but PR #79 closed
without merge and the sequence no longer selects a next code PR. Lineage,
attempt, and evidence concepts may be reconsidered only when a reviewed
vertical slice contains both a real producer and a real consumer.

```text
WORKER_V2_INSTRUMENTATION = ANIMEIMAGES_OWNED_VIDEODL_ORCHESTRATOR
GRABBER_ONLY = BLOCKED_UNTIL_SANDBOX_GATE
```
