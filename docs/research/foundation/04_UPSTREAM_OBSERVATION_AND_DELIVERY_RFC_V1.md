# Upstream Observation And Delivery RFC v1

## Status And Ownership

`UpstreamObservationV1` is a private, request-scoped evidence contract. It is
not a Public API, database model, `MediaAsset`, `ParseResult`, routing score,
or provider winner declaration.

`DeliveryRequirementV1` describes what an observed resource requires. It does
not let the provider select the product transport or execute a downloader.

Related documents:

- [Whole-repository audit](01_VIDEODL_WHOLE_REPOSITORY_UPSTREAM_AUDIT_V1.md)
- [Architecture absorption](02_VIDEODL_ARCHITECTURE_AND_CODE_ABSORPTION_V1.md)
- [Stage, lineage, and attempt RFC](03_BACKEND_STAGE_LINEAGE_AND_ATTEMPT_RFC_V1.md)
- [Foundation roadmap](05_FOUNDATION_IMPLEMENTATION_ROADMAP_V1.md)
- [Reuse and provenance matrix](06_COMPONENT_REUSE_AND_PROVENANCE_MATRIX_V1.md)

## Contract Boundaries

The contract has two outputs:

1. a URL-free, bounded observation that may be projected to Staff Lab;
2. a process-private resource sidecar keyed by opaque resource references.

The sidecar may contain a private source/media URL and credential context only
inside the isolated request. It is never serialized into logs, initial HTML,
ordinary JSON, persistence, or public contracts. Existing Provider Lab
resource-token issuance is the pattern for exposing a bounded Staff inspection
capability without exposing the sidecar itself.

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
  support_stage: SupportStageV1
  claimed_platform: bounded platform ID or unknown
  claimed_identity: ClaimedIdentityV1 or null
  metadata: ObservationMetadataV1
  media_candidates: MediaCandidateObservationV1[]
  format_variants: FormatVariantObservationV1[]
  video_tracks: TrackObservationV1[]
  audio_tracks: TrackObservationV1[]
  relationships: RelationshipObservationV1[]
  request_requirements: RequestRequirementV1[]
  delivery_requirements: DeliveryRequirementV1[]
  warnings: fixed warning code[]
  error_code: fixed safe code or null
  raw_shape: bounded URL-free structural summary
```

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
  private_resource_ref: opaque key into process-private sidecar
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
  headers_required: boolean
  referer_required: boolean
  cookie_required: boolean
  merge_group: request-local ID or null
  resource_identity: ResourceIdentityObservationV1
```

The URL required by the task is represented safely: the candidate holds
`private_resource_ref`; `PrivateResourceEnvelopeV1` holds the actual private
URL in the isolated process. This avoids making a URL-bearing DTO the default
value passed through the application.

### Private Resource Sidecar

```text
PrivateResourceEnvelopeV1
  resource_ref: opaque request-local ID
  url: private http(s) resource URL
  header_names: bounded allowlisted names
  header_values: private mapping
  cookie_context: private, optional
  referer: private, optional
  expires_at_hint: private, optional
```

The sidecar is neither logged nor persisted. Before a candidate leaves the
isolated provider boundary, existing host/address policy validates it and a
scoped token replaces the private reference when Staff inspection is allowed.

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

### Request Requirements

`RequestRequirementV1` describes acquisition context without values:

- `SPECIAL_HEADERS_REQUIRED` plus allowlisted header names;
- `REFERER_REQUIRED`;
- `COOKIE_REQUIRED`;
- `BROWSER_CONTEXT_REQUIRED`;
- `RANGE_SUPPORTED` or `RANGE_REQUIRED` when observed;
- `EXPIRING_RESOURCE` as a non-authoritative hint.

Cookie/header/referer values stay in the private sidecar.

## DeliveryRequirementV1

### Enum

| Requirement | Meaning |
| --- | --- |
| `DIRECT_SINGLE_FILE` | One complete resource can represent the output. |
| `SEPARATE_VIDEO_AUDIO` | Distinct tracks must be combined or delivered as a multi-part product. |
| `HLS_PLAYLIST` | Candidate is an HLS playlist/segment graph. |
| `DASH_MANIFEST` | Candidate is a DASH manifest/track graph. |
| `REQUIRES_REMUX` | Container-only transformation is required for the intended output. |
| `REQUIRES_TRANSCODE` | Codec transformation is required. |
| `SPECIAL_HEADERS_REQUIRED` | Acquisition requires allowlisted request headers. |
| `REFERER_REQUIRED` | Acquisition requires a Referer context. |
| `COOKIE_REQUIRED` | Acquisition requires private Cookie context. |
| `BROWSER_DIRECT_ELIGIBLE` | Admission evidence indicates the browser may acquire the ordinary resource without AnimeImages media relay. |
| `SERVER_GENERATED_REQUIRED` | The final artifact must be generated server-side, such as a merge, remux, or archive not available in-browser. |
| `UNDELIVERABLE` | Current product and policy cannot safely deliver the observation. |

### Requirement Record

```text
DeliveryRequirementRecordV1
  requirement: DeliveryRequirementV1
  candidate_ids: request-local ID[]
  merge_group: request-local ID or null
  evidence: provider_declared | structure_derived | inspection_verified |
            admission_decided
  confidence: exact | corroborated | claimed
  reason_code: fixed safe enum
```

The provider may declare or structurally imply a requirement. Only
AnimeImages admission may add `admission_decided`,
`BROWSER_DIRECT_ELIGIBLE`, `SERVER_GENERATED_REQUIRED`, or final
`UNDELIVERABLE` product semantics.

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
  "support_stage": "MEDIA_CANDIDATES",
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
      "headers_required": false,
      "referer_required": false,
      "cookie_required": false,
      "merge_group": null
    }
  ],
  "delivery_requirements": [
    {
      "requirement": "DIRECT_SINGLE_FILE",
      "candidate_ids": ["candidate-1"],
      "evidence": "structure_derived",
      "confidence": "exact"
    }
  ]
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
  "support_stage": "MEDIA_CANDIDATES",
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
  "delivery_requirements": [
    {
      "requirement": "SEPARATE_VIDEO_AUDIO",
      "candidate_ids": ["candidate-1", "candidate-2"],
      "merge_group": "merge-1",
      "evidence": "structure_derived",
      "confidence": "exact"
    },
    {
      "requirement": "SERVER_GENERATED_REQUIRED",
      "candidate_ids": ["candidate-1", "candidate-2"],
      "merge_group": "merge-1",
      "evidence": "admission_decided",
      "confidence": "exact"
    }
  ]
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
  "support_stage": "RELATIONSHIPS_PRESERVED",
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
  "delivery_requirements": []
}
```

## Projection 1: Provider Lab

`UpstreamObservationV1 -> LabProviderResultV1` should:

1. retain top-level provider/version/status and bounded metadata;
2. map selected backend to the existing field and add a sanitized stage/type
   section only in a later schema-compatible Lab extension;
3. issue existing opaque Staff-bound tokens for admitted inspection candidates;
4. project URL-free assets and relationships;
5. include bounded attempt counts, outcomes, timings, support stage, and fixed
   warning/error codes;
6. never include private sidecar values, raw exception text, or source/media
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
- boolean request requirements that the delivery path supports;
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
| Provider Lab private resources | Existing pattern for the resource sidecar and scoped token exchange. |

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

1. Observation and sidecar resource references are one-to-one.
2. Every candidate/track/relationship ID is request-local and unique.
3. Unknown enums, negative values, excessive arrays, and oversized strings are
   rejected.
4. A relationship cannot reference a missing candidate/track.
5. A still/motion relation requires explicit item-local or payload evidence;
   URL similarity and array position are insufficient.
6. `cookie_required=true` never serializes Cookie values.
7. `headers_required=true` exposes at most allowlisted header names.
8. A provider cannot self-assert `PRODUCT_ADMISSIBLE` or
   `PRODUCTION_ELIGIBLE`; AnimeImages derives support stage.
9. `BROWSER_DIRECT_ELIGIBLE`, `SERVER_GENERATED_REQUIRED`, and
   `UNDELIVERABLE` require AnimeImages admission evidence.
10. Projection to `MediaAsset` is fail-closed if identity, host, format, or
    delivery requirements are unsupported.

## RFC Decision

`UPSTREAM_OBSERVATION_RFC = READY`

`DELIVERY_REQUIREMENT_RFC = READY`

The first implementation should define URL-free immutable contracts and pure
validation only. It should not wire videodl, ParseHub, Provider Lab views,
formal routes, downloads, or persistence.
