# Foundation Implementation Roadmap v1

## Scope

This roadmap turns the read-only videodl audit into independently reviewable
foundation PRs. It does not authorize implementation in this research PR.

Reference set:

- [Whole-repository audit](01_VIDEODL_WHOLE_REPOSITORY_UPSTREAM_AUDIT_V1.md)
- [Architecture absorption](02_VIDEODL_ARCHITECTURE_AND_CODE_ABSORPTION_V1.md)
- [Stage, lineage, and attempt RFC](03_BACKEND_STAGE_LINEAGE_AND_ATTEMPT_RFC_V1.md)
- [Observation and delivery RFC](04_UPSTREAM_OBSERVATION_AND_DELIVERY_RFC_V1.md)
- [Reuse and provenance matrix](06_COMPONENT_REUSE_AND_PROVENANCE_MATRIX_V1.md)

## Sequencing Principles

1. Contracts precede worker instrumentation.
2. Worker evidence precedes UI projection.
3. Lab evidence never auto-promotes a formal provider.
4. Private resources remain sidecar-only and tokenized.
5. Every PR is migration-free and reversible by code revert.
6. XHS, Douyin, Bilibili, YouTube, Batch, and download delivery stay frozen.
7. Continuous provider health is not a prerequisite for Assets 1 or 2.

## PR 1: Internal Observation Contracts

**Title:** Add URL-free upstream observation foundation contracts

**Goal**

Implement immutable, pure contracts and validation for:

- `ResolutionStageV1`;
- backend capability taxonomy IDs;
- `BackendLineageV1`;
- `AttemptTraceV1`;
- `SupportStageV1`;
- `UpstreamObservationV1`;
- `DeliveryRequirementV1`;
- private resource references without resource values.

**Allowed files**

- a new internal service module under `xhs_download/services/`;
- focused pure unit tests;
- minimal package exports if required.

**Forbidden scope**

- provider workers and Provider Lab views/templates;
- platform adapters and provider order;
- settings, models, migrations, dependencies, workflows;
- resource-token behavior;
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
- lineage graph integrity;
- attempt ordering and selected-winner invariants;
- support-stage derivation;
- complete MP4, separate A/V, and LivePhoto URL-free fixtures;
- deterministic serialization and size bounds.

**Human gate**

Architecture review confirms the contracts are internal, URL-free, generic
across videodl and ParseHub, and do not alter runtime.

**Rollback**

Revert the isolated contract/test commit. No data or runtime cleanup.

**Exit criteria**

All contracts validate the three RFC examples; all negative security fixtures
fail closed; full existing tests remain unchanged.

## PR 2: Explicit Upstream Backend Catalog

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

## PR 3: videodl Worker V2 Evidence And Modes

**Title:** Emit bounded videodl resolution attempts in Provider Lab

**Goal**

Add explicit `AUTO`, `DEDICATED_ONLY`, `COMMON_ONLY`, `GRABBER_ONLY`, and
`SPECIFIC_BACKEND` worker modes and emit observation, lineage, and attempt
evidence from the exact-pin whole-repository subprocess.

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

**Runtime behavior**

Only an explicitly enabled Staff Lab request changes. Default-off behavior,
one-request/one-process isolation, output limits, no Cookie/proxy, and parent
process cleanup remain.

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
- static/browser grabber split;
- timeout/process-tree cleanup;
- no sensitive URL/header/Cookie/error/path leakage;
- current final assets remain byte-for-byte contract-compatible where
  applicable.

**Human gate**

Staff Lab runs synthetic/offline fixtures for each mode and visually verifies
attempt order without real media downloading.

**Rollback**

Disable V2 path and revert worker changes; V1 final-result worker remains.

## PR 4: Provider Lab Observation Projection

**Title:** Project upstream observation evidence into Staff Provider Lab

**Goal**

Render sanitized stage, backend type, support stage, lineage, attempt outcomes,
delivery requirements, and tokenized candidates while keeping independent
provider cards and failure isolation.

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

## PR 5: ParseHub Observation Adapter

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

## PR 6: Selected Safe Helper Reimplementation

**Title:** Add pure media observation helpers for admitted upstream resources

**Goal**

Only after contracts and evidence are stable, cleanly reimplement selected
pure concepts: MIME/signature normalization, HLS master parsing, and structured
delivery planning. Reuse existing AnimeImages security helpers first.

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

## Recommended Next Code PR

```text
NEXT_CODE_PR = Add URL-free upstream observation foundation contracts
```

Precise slice:

> Add immutable, pure `ResolutionStageV1`, `BackendLineageV1`,
> `AttemptTraceV1`, `SupportStageV1`, `UpstreamObservationV1`, and
> `DeliveryRequirementV1` contracts with bounded URL-free fixtures and
> validation tests. Do not wire videodl, ParseHub, Provider Lab views,
> platform routes, downloads, settings, persistence, or migrations.

Why this is first:

- it directly improves both whole-repository integration and architecture
  absorption;
- it has no runtime behavior;
- it does not depend on provider health monitoring;
- it gives Worker V2 and ParseHub one reviewed target;
- it can be reverted without state or operational cleanup.

Exit criteria:

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
admission, security, operational, and Human Gate after the foundation work.

## Asset 3 Deferred Interface

`CONTINUOUS_PROVIDER_HEALTH_MONITOR = DEFERRED`

A future health consumer may read only standardized provider/backend,
resolution stage, support stage, elapsed time, asset count, audio completeness,
delivery requirement, failure class, and lineage fields.

`CURRENT_PROVIDER_HEALTH_TEST = FUTURE_CONSUMER_OF_OBSERVATION_AND_ATTEMPT_EVIDENCE`

No schedule model, timer, email, report UI, or database change is designed in
this audit.

## Roadmap Decision

`ASSET_1_TARGET_MODEL = READY`

`VIDEODL_MODE_MODEL = READY`

The sequence is deliberately contract-first and Lab-first. It converts
videodl from an opaque final-result supplier into an explainable upstream
without turning Provider Lab into a production router.
