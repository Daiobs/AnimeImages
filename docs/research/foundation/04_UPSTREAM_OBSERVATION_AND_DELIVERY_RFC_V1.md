# Upstream Observation And Delivery RFC v1

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

## Status And Ownership

`UpstreamObservationV1` is a private, request-scoped evidence contract. It is
not a Public API, database model, `MediaAsset`, `ParseResult`, routing score,
or provider winner declaration.

`AcquisitionRequirementV1` and `AssemblyRequirementV1` describe observed
facts. `DeliveryDispositionV1` is a separate AnimeImages admission decision;
an upstream cannot select product transport or execute a downloader.

Related documents:

- [Whole-repository audit](01_VIDEODL_WHOLE_REPOSITORY_UPSTREAM_AUDIT_V1.md)
- [Architecture absorption](02_VIDEODL_ARCHITECTURE_AND_CODE_ABSORPTION_V1.md)
- [Stage, lineage, and attempt RFC](03_BACKEND_STAGE_LINEAGE_AND_ATTEMPT_RFC_V1.md)
- [Foundation roadmap](05_FOUNDATION_IMPLEMENTATION_ROADMAP_V1.md)
- [Reuse and provenance matrix](06_COMPONENT_REUSE_AND_PROVENANCE_MATRIX_V1.md)

## Contract Boundaries

The private exchange has two channels:

1. a URL-free, bounded observation that may be projected to Staff Lab;
2. a request-bound `PrivateResourceChannelV1` keyed by opaque resource
   references.

The private channel is not confined to Worker memory. The current protocol is:

```text
Worker
  -> bounded stdout JSON resources sidecar
  -> Django parent
  -> short-lived Cache
  -> opaque Staff token
```

This restricted private Worker Protocol may cross the subprocess boundary,
but it is request-bound and TTL-limited. Values never enter the normal
observation, initial HTML, sanitized result JSON, ordinary logs, or database.
Current Provider Lab output does not carry Cookie or header values.

## UpstreamObservationV1

### Top-Level Shape

```text
UpstreamObservationV1
  schema_version: "1.0"
  request_id: opaque AnimeImages request ID
  top_level_provider: fixed provider ID
  provider_version: bounded version/pin
  provider_scope: internal | staff_lab | formal_candidate
  resolution_mode: AUTO | DEDICATED_ONLY | COMMON_ONLY |
                   GRABBER_ONLY | SPECIFIC_BACKEND
  resolution_stage: ResolutionStageV1 or null
  selected_backend: fixed backend catalog ID or null
  backend_type: BackendCapabilityType or null
  backend_lineage: BackendLineageV1
  attempt_trace: AttemptTraceV1[]
  evidence_stage: EvidenceStageV1
  relationship_evidence: RelationshipEvidenceV1
  route_admission_state: RouteAdmissionStateV1
  deployment_state: DeploymentStateV1
  license_scope: fixed project-owned scope
  claimed_platform: bounded platform ID or unknown
  claimed_identity: ClaimedIdentityV1 or null
  metadata: ObservationMetadataV1
  media_candidates: MediaCandidateObservationV1[]
  format_variants: FormatVariantObservationV1[]
  video_tracks: TrackObservationV1[]
  audio_tracks: TrackObservationV1[]
  relationships: RelationshipObservationV1[]
  acquisition_requirements: AcquisitionRequirementV1[]
  assembly_requirements: AssemblyRequirementV1[]
  delivery_disposition: DeliveryDispositionV1 or null
  warnings: fixed warning code[]
  error_code: fixed safe code or null
  raw_shape: bounded URL-free structural summary
```

`GRABBER_ONLY` is represented for schema completeness but remains
`BLOCKED_UNTIL_SANDBOX_GATE`; Staff opt-in alone cannot activate it.

### Claimed Identity

```text
ClaimedIdentityV1
  identity_type: post | video | image_set | article | direct_resource | unknown
  identity_digest: request-scoped or stable safe digest
  identity_source: redirect | payload | page_state | api | backend_claim
  authority: authoritative | corroborated | claimed | unknown
```

Raw note/video IDs and source URLs are not required in the generic contract.
A platform admission layer may compare private canonical identity values inside
the request, then emit only a safe digest and authority result.

### Metadata

`ObservationMetadataV1` contains bounded, untrusted claims:

- title;
- author display claim;
- content summary;
- published-time claim when safely normalized;
- declared post/result type;
- item count.

Metadata does not establish source identity or admission.

### Media Candidate Observation

```text
MediaCandidateObservationV1
  candidate_id: request-local stable ID
  private_resource_ref: opaque key into PrivateResourceChannelV1
  media_type: image | video | audio | animation | playlist | unknown
  role: primary | cover | gallery | still | motion | video_track |
        audio_track | attachment | unknown
  source_ordinal: positive integer
  container: mp4 | webm | jpeg | png | webp | avif | gif | hls |
             dash | unknown
  declared_extension: bounded lower-case extension or unknown
  width: non-negative integer or null
  height: non-negative integer or null
  duration_ms: non-negative integer or null
  codec: bounded normalized codec or unknown
  frame_rate: positive bounded rational/decimal or null
  bitrate_bps: non-negative integer or null
  size_bytes: non-negative integer or null
  size_provenance: content_length | exact_payload | manifest |
                   provider_exact | provider_approx | derived_sum | unknown
  merge_group: request-local ID or null
  resource_identity: ResourceIdentityObservationV1
```

The URL required by the task is represented safely: the candidate holds
`private_resource_ref`; `PrivateResourceSidecarV1` carries the actual private
value through the restricted Worker Protocol and short-lived parent cache.
This avoids making a URL-bearing DTO the default application contract.

### PrivateResourceChannelV1

```text
PrivateResourceSidecarV1
  resource_ref: opaque request-local ID
  url: private http(s) resource URL
  header_names: bounded allowlisted names
  header_values: private mapping
  cookie_context: private, optional
  referer: private, optional
  expires_at_hint: private, optional
```

The Worker emits this sidecar only through bounded stdout JSON. The Django
parent validates the resource under existing host/address policy, stores the
private envelope in short-lived Cache, and issues an opaque request/Staff
scoped token when inspection is allowed. It is not database persistence.
Normal observations, initial HTML, sanitized JSON, and ordinary logs receive
only `resource_ref` or the scoped token. Cookie and header values do not enter
the current Lab output.

### Format Variants And Tracks

`FormatVariantObservationV1` records the provider's inventory without choosing
a winner:

- variant ID and candidate/track refs;
- quality label and numeric dimensions;
- container, codecs, frame rate, bitrate, and size/provenance;
- protocol (`single_file`, `hls`, `dash`, `unknown`);
- video/audio presence;
- merge-group ID;
- declared rank from the upstream, if any, marked as untrusted.

`TrackObservationV1` records:

- track ID, candidate ref, and kind (`video` or `audio`);
- codec/container/language/channel data when known;
- dimensions/frame rate for video;
- bitrate/sample rate/channels for audio;
- merge-group ID and default flag.

Independent audio is explicit rather than inferred from a generic
`requires_merge` boolean.

### Relationships

`RelationshipObservationV1` is URL-free:

- relationship type (`gallery_order`, `livephoto_motion`, `cover_of`,
  `track_member`, `content_block_media`);
- source and target candidate/track IDs;
- group ID;
- ordinal;
- evidence type (`item_local`, `payload_explicit`, `provider_claim`);
- authority (`authoritative`, `corroborated`, `claimed`).

Array-position or URL-similarity guesses cannot claim `item_local` evidence.

## AcquisitionRequirementV1

This enum records what is required to acquire an observed resource. It carries
no private values and makes no product transport decision.

| Requirement | Meaning |
| --- | --- |
| `SPECIAL_HEADERS_REQUIRED` | Acquisition requires one or more allowlisted request header names. |
| `REFERER_REQUIRED` | Acquisition requires a Referer context. |
| `COOKIE_REQUIRED` | Acquisition requires private Cookie context. |
| `BROWSER_CONTEXT_REQUIRED` | Acquisition requires browser-managed state or execution. |
| `RANGE_REQUIRED` | Successful or complete acquisition requires byte-range behavior. |
| `EXPIRING_RESOURCE` | Evidence indicates that the resource is time-limited. |

Cookie, header, and Referer values remain in `PrivateResourceChannelV1` and do
not enter current Lab output.

## AssemblyRequirementV1

This enum records media structure and assembly facts. It does not choose an
executor or user-facing route.

| Requirement | Meaning |
| --- | --- |
| `DIRECT_SINGLE_FILE` | One complete resource can represent the output. |
| `SEPARATE_VIDEO_AUDIO` | Distinct tracks must be combined or delivered as a multi-part product. |
| `HLS_PLAYLIST` | Candidate is an HLS playlist/segment graph. |
| `DASH_MANIFEST` | Candidate is a DASH manifest/track graph. |
| `REQUIRES_REMUX` | Container-only transformation is required for the intended output. |
| `REQUIRES_TRANSCODE` | Codec transformation is required. |

Both fact families use bounded records:

```text
RequirementRecordV1
  requirement: AcquisitionRequirementV1 | AssemblyRequirementV1
  candidate_ids: request-local ID[]
  merge_group: request-local ID or null
  header_names: bounded allowlisted name[] or empty
  evidence: provider_declared | structure_derived | inspection_verified
  confidence: exact | corroborated | claimed
  reason_code: fixed safe enum
```

## DeliveryDispositionV1

Delivery disposition is a product decision owned only by AnimeImages
admission:

| Disposition | Meaning |
| --- | --- |
| `BROWSER_DIRECT` | The admitted ordinary resource can be acquired directly by the browser. |
| `SERVICE_WORKER_DIRECT` | The admitted resource can use the approved zero-relay Service Worker path. |
| `BROWSER_ARCHIVE` | The browser can generate the admitted multi-resource archive. |
| `SERVER_GENERATED` | The final artifact must be generated by an approved server job, such as a merge/remux/transcode. |
| `REJECTED_UNDELIVERABLE` | Current product and policy cannot safely deliver the observation. |

No worker or provider may set this field. It is null until platform admission
evaluates identity, host, format, relationships, private request context, and
available delivery capabilities.

### Delivery Ownership

After admission, AnimeImages chooses among:

- browser direct or Service Worker zero-relay delivery;
- browser archive for client-generated multi-resource output;
- server merge/remux/transcode for genuinely server-generated output;
- rejection when request context or delivery cannot be safely supported.

Providers do not call downloaders and do not select the user-facing route.

## URL-Free Examples

The examples intentionally omit real/source URLs, credentials, hostnames,
note IDs, and provider response data.

### Complete MP4

```json
{
  "schema_version": "1.0",
  "top_level_provider": "example_repo",
  "resolution_mode": "SPECIFIC_BACKEND",
  "resolution_stage": "dedicated_source",
  "selected_backend": "example_video_adapter",
  "backend_type": "DEDICATED_SOURCE_ADAPTER",
  "evidence_stage": "MEDIA_CANDIDATES",
  "relationship_evidence": "NOT_APPLICABLE",
  "route_admission_state": "UNREVIEWED",
  "deployment_state": "NOT_DEPLOYED",
  "license_scope": "research_only",
  "media_candidates": [
    {
      "candidate_id": "candidate-1",
      "private_resource_ref": "resource-1",
      "media_type": "video",
      "role": "primary",
      "container": "mp4",
      "declared_extension": "mp4",
      "size_bytes": 125000000,
      "size_provenance": "content_length",
      "merge_group": null
    }
  ],
  "acquisition_requirements": [],
  "assembly_requirements": [
    {
      "requirement": "DIRECT_SINGLE_FILE",
      "candidate_ids": ["candidate-1"],
      "evidence": "structure_derived",
      "confidence": "exact"
    }
  ],
  "delivery_disposition": null
}
```

### Separate Video And Audio

```json
{
  "schema_version": "1.0",
  "top_level_provider": "example_repo",
  "resolution_mode": "AUTO",
  "resolution_stage": "dedicated_source",
  "selected_backend": "example_track_adapter",
  "backend_type": "DEDICATED_SOURCE_ADAPTER",
  "evidence_stage": "MEDIA_CANDIDATES",
  "relationship_evidence": "PRESERVED",
  "route_admission_state": "UNREVIEWED",
  "deployment_state": "NOT_DEPLOYED",
  "license_scope": "research_only",
  "video_tracks": [
    {
      "track_id": "video-1",
      "candidate_id": "candidate-1",
      "codec": "h264",
      "merge_group": "merge-1"
    }
  ],
  "audio_tracks": [
    {
      "track_id": "audio-1",
      "candidate_id": "candidate-2",
      "codec": "aac",
      "merge_group": "merge-1"
    }
  ],
  "relationships": [
    {
      "relationship_type": "track_member",
      "source_id": "video-1",
      "target_id": "audio-1",
      "group_id": "merge-1",
      "evidence_type": "payload_explicit"
    }
  ],
  "acquisition_requirements": [],
  "assembly_requirements": [
    {
      "requirement": "SEPARATE_VIDEO_AUDIO",
      "candidate_ids": ["candidate-1", "candidate-2"],
      "merge_group": "merge-1",
      "evidence": "structure_derived",
      "confidence": "exact"
    }
  ],
  "delivery_disposition": null
}
```

### LivePhoto Still And Motion

```json
{
  "schema_version": "1.0",
  "top_level_provider": "example_rich_media_repo",
  "resolution_mode": "SPECIFIC_BACKEND",
  "resolution_stage": "dedicated_source",
  "selected_backend": "example_relationship_adapter",
  "backend_type": "RICH_MEDIA_RELATIONSHIP_PROVIDER",
  "evidence_stage": "MEDIA_CANDIDATES",
  "relationship_evidence": "PRESERVED",
  "route_admission_state": "UNREVIEWED",
  "deployment_state": "NOT_DEPLOYED",
  "license_scope": "research_only",
  "media_candidates": [
    {
      "candidate_id": "still-1",
      "private_resource_ref": "resource-1",
      "media_type": "image",
      "role": "still",
      "container": "jpeg",
      "source_ordinal": 1
    },
    {
      "candidate_id": "motion-1",
      "private_resource_ref": "resource-2",
      "media_type": "video",
      "role": "motion",
      "container": "mp4",
      "source_ordinal": 1
    }
  ],
  "relationships": [
    {
      "relationship_type": "livephoto_motion",
      "source_id": "still-1",
      "target_id": "motion-1",
      "group_id": "livephoto-1",
      "ordinal": 1,
      "evidence_type": "item_local",
      "authority": "authoritative"
    }
  ],
  "acquisition_requirements": [],
  "assembly_requirements": [],
  "delivery_disposition": null
}
```

## Projection 1: Provider Lab

`UpstreamObservationV1 -> LabProviderResultV1` should:

1. retain top-level provider/version/status and bounded metadata;
2. map selected backend to the existing field and add a sanitized stage/type
   section only in a later schema-compatible Lab extension;
3. issue existing opaque Staff-bound tokens for admitted inspection candidates;
4. project URL-free assets and relationships;
5. include bounded attempt counts, outcomes, timings, evidence stage,
   relationship evidence, and fixed warning/error codes;
6. never include private channel values, raw exception text, or source/media
   URLs;
7. keep result persistence disabled.

The Lab projection can show evidence. It must not choose a formal winner or
modify platform Registry state.

## Projection 2: Platform Admission

```text
UpstreamObservationV1
  -> platform-specific identity authority
  -> host/address/format/signature admission
  -> relationship and ordering validation
  -> delivery-policy decision
  -> MediaAsset / RichMediaContractV1 / ParseResult
```

Only admitted final facts cross this projection:

- final provider/source IDs;
- platform and post type;
- media type, role, order, and relationship IDs;
- safe host suffix policy outcome;
- verified extension/container and size provenance;
- dimensions, duration, codec facts when reliable;
- admitted acquisition and assembly facts that the selected delivery path
  supports;
- an approved private resource capability/token.

Attempt trace, backend lineage, format alternatives, source URLs, private
resource values, failure history, and executor suggestions remain internal.

## Current Contract Relationships

| AnimeImages contract | Relationship to observation |
| --- | --- |
| `MediaCandidate` | A platform policy may derive candidates from one or more observations for local selection. It remains compact. |
| `MediaAsset` | Final admitted runtime/download asset. It must not become the observation envelope. |
| `ParseResult` | Final product parse result assembled after admission. |
| `RichMediaContractV1` | URL-free item-local relationship projection. It complements, rather than duplicates, observation. |
| `LabProviderResultV1` | Staff-safe projection of observation and tokenized resources. |
| Provider Lab private resources | Existing bounded Worker sidecar, Django short-lived Cache, and scoped Staff token exchange. |

## ParseHub Control Mapping

At pin `144260f4349c5bb57415f7555a572fcd48c3b8ba`:

| ParseHub type | Observation mapping |
| --- | --- |
| `ImageParseResult` | One or ordered image candidates; LivePhoto items retain relationships. |
| `VideoParseResult` | Primary video candidate and metadata. |
| `MultimediaParseResult` | Mixed typed candidates plus ordered relationships. |
| `RichTextParseResult` | Metadata/content summary plus content-block media relationships. |
| `ImageRef` | Image candidate with dimensions/declared format. |
| `VideoRef` | Video candidate with dimensions/duration/declared format. |
| `LivePhotoRef` | Still and motion candidates joined by explicit item-local relationship. |

ParseHub gaps relative to videodl:

- independent audio tracks are not a general result primitive;
- delivery flags are sparse;
- comprehensive format inventory is not consistently retained.

videodl gaps relative to ParseHub:

- gallery and mixed-media typing are weak;
- rich text/content-block structure is absent;
- LivePhoto still/motion relationships are absent;
- one mutable `VideoInfo` cannot express a general rich-media graph.

`PARSEHUB_CONTROL_COMPATIBILITY = PASS`

## Validation Rules

1. Observation and request-bound private channel resource references are
   one-to-one.
2. Every candidate/track/relationship ID is request-local and unique.
3. Unknown enums, negative values, excessive arrays, and oversized strings are
   rejected.
4. A relationship cannot reference a missing candidate/track.
5. A still/motion relation requires explicit item-local or payload evidence;
   URL similarity and array position are insufficient.
6. `COOKIE_REQUIRED` never serializes Cookie values.
7. `SPECIAL_HEADERS_REQUIRED` exposes at most allowlisted header names and
   never header values.
8. A provider cannot self-assert route admission, deployment state, license
   scope, or delivery disposition; AnimeImages owns those decisions.
9. Every `DeliveryDispositionV1` value requires AnimeImages admission evidence.
10. Projection to `MediaAsset` is fail-closed if identity, host, format, or
    acquisition/assembly facts are unsupported.

## RFC Decision

`UPSTREAM_OBSERVATION_RFC = READY`

`ACQUISITION_REQUIREMENT_MODEL = READY`

`ASSEMBLY_REQUIREMENT_MODEL = READY`

`DELIVERY_DISPOSITION_MODEL = ANIMEIMAGES_ADMISSION_OWNED`

`DELIVERY_FACTS_AND_DECISIONS = SEPARATED`

`PRIVATE_RESOURCE_BOUNDARY = ALIGNED_WITH_CURRENT_PROTOCOL`

The first implementation should define URL-free immutable contracts and pure
validation only. It should not wire videodl, ParseHub, Provider Lab views,
formal routes, downloads, or persistence.
