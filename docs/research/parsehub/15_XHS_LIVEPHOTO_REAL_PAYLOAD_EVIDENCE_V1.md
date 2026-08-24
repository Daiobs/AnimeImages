# XHS LivePhoto Real Payload Evidence v1

## Status

```text
XHS_LIVEPHOTO_REAL_PAYLOAD_EVIDENCE = COMPLETE
XHS_LIVEPHOTO_RELATIONSHIP_MAPPER_READY = YES
XHS_LIVEPHOTO_ASSET_SPEC_MAPPER_READY = PARTIAL_BLOCKED_BY_FORMAT_EVIDENCE
XHS_LIVEPHOTO_PROVIDER_INTEGRATION_READY = NO
XHS_STILL_FORMAT_PAYLOAD_FIELD = NOT_PROVEN
XHS_PROVIDER_WIRING = NOT_STARTED
XHS_PUBLIC_SUPPORT = NO
SUPERSEDES_PR_66_EVIDENCE_GAP_STATE = YES
DEPLOYED = NO
```

This research closes the real-payload evidence gap for beginning a separate,
offline XHS LivePhoto mapper implementation. It does not implement that mapper
and does not connect rich-media relationships to a Provider, registry,
manifest, download path, or UI.

## Collection Boundary

Five user-supplied public short links were inspected on 2026-08-24 without an
account credential. Complete source links, redirect queries, page payloads,
media URLs, request tokens, headers, titles, authors, and user identities are
not retained. Source links and note identities appear only as SHA-256
fingerprints in the structured evidence file.

The evidence schema names these repository-safe correlation values explicitly:

```text
source_fingerprint
target_identity_fingerprint
payload_identity_fingerprint
```

A SHA-256 fingerprint is not a secret and is not an anonymized identity
guarantee. Anyone who already knows a candidate public link or note identity
can recompute its deterministic hash and confirm a match. These fingerprints
are retained only for research-evidence correlation. They must not be used as
a runtime source identity, cache key, asset ID, Provider resource ID, or public
source identifier.

Each short link was resolved first. The intended note identity was taken from
the identity-bearing target path, then the current AnimeImages exact-identity
selector was applied to the page payload. A payload candidate was admitted
only when its own note identity exactly matched the intended identity.

`firstNoteId` was not read as target authority. Recommendation, feed, generic
page, and error-page candidates had no authority.

## Sample Inventory

| Sample | Declared type | Source fingerprint | Payload identity | Items | LivePhoto pairs | Result |
| --- | --- | --- | --- | ---: | ---: | --- |
| `xhs-livephoto-01` | LivePhoto | `sha256:1c0465f56850...` | exact match | 14 | 14 | direct item-local evidence |
| `xhs-livephoto-02` | mixed LivePhoto/gallery | `sha256:829fed6e2655...` | exact match | 17 | 12 | direct item-local evidence; 5 regular images preserved |
| `xhs-gallery-01` | image gallery | `sha256:e075ddb6fe2c...` | exact match | 4 | 0 | no false positive |
| `xhs-video-01` | video | `sha256:d43cc6aeb9a1...` | exact match | 1 cover item | 0 | no false positive |
| `xhs-invalid-01` | invalid/deleted | `sha256:5b09d8d82317...` | no matching payload | 0 | 0 | fail-closed |

The invalid sample's intended note identity was resolved from the first target,
but the destination became a generic error page. No exact payload note, image,
video, or LivePhoto relationship was admitted.

## Observed Real Shape

Both positive samples expose an ordered current-note collection at:

```text
note.imageList[]
```

Within one qualifying item, the following fields coexist:

```text
item.livePhoto
item.urlDefault or item.infoList[]
item.stream.<codec-family>[].masterUrl
item.stream.<codec-family>[].backupUrls[]
```

The LivePhoto marker, still candidate, and motion stream are properties of the
same image-list item. This is direct ownership evidence. The relationship is
not reconstructed from separate arrays.

The first positive sample contained 14 LivePhoto items and no regular image
items. The second sample is the stronger mixed-content control: 12 items carry
complete LivePhoto evidence while 5 interleaved items remain regular images.
The direct item structure preserves that distinction without compacting the
motion members or aligning parallel indexes.

## Rejected Pairing Methods

The evidence contract explicitly rejects:

- equal or adjacent array positions;
- similar URL paths or filenames;
- matching dimensions;
- coincidental media ordering; and
- unconditional `firstNoteId` selection.

A future mapper may emit one relationship only when the selected exact target
note contains one item with all three direct fields: marker, still, and motion.
Missing any one of them must fail closed for that relationship.

The item ordinal may preserve source order and locate an already-proven
relationship. It is not pairing evidence and must never be used to align a
compacted still list with a compacted motion list.

## Superseded Research And Retained Protocol

PR #66 documented the earlier evidence-gap state before these five qualifying
samples were available. Its `qualified samples = 0` and `pair evidence blocked`
conclusions are superseded by this evidence. The following safety lessons and
bounded acquisition protocol remain applicable:

- select the exact requested note by requested/payload identity equality;
- never treat `firstNoteId`, feed order, or a recommendation card as target
  authority;
- never infer relationships from array position, filenames, URL similarity,
  dimensions, or codec-family names;
- collect only the minimum public samples and fields required by a stated gate;
- redact complete links, redirect queries, tokens, headers, and payload bodies
  before durable output;
- record redirects, CORS behavior, and failed routes as fixed outcomes rather
  than admitting content from a default, login, recommendation, or error page;
- construct fictional sanitized fixtures only after the real field shape has
  been independently confirmed; and
- keep host admission and per-member format proof as separate later reviews.

The failed routes were useful negative evidence: login-gated search results and
a historical short link that no longer resolved to an identity-bearing note
could not pass exact identity admission. They yielded no relationship evidence
and did not justify a guessed fixture.

The pinned ParseHub field paths were also only a hypothesis until current real
payloads confirmed them. Its unconditional `firstNoteId` selection, per-image
network extension probes, and unknown-to-JPEG fallback remain explicitly
rejected.

## Negative Controls

### Ordinary gallery

The gallery payload contained four regular image items. Each had a still
candidate, no qualifying LivePhoto marker/motion combination, and produced
zero LivePhoto pairs.

```text
GALLERY_FALSE_POSITIVE = 0
```

### Video note

The video payload exposed one regular image cover plus a separate note-level
video structure. The cover item did not own a LivePhoto motion member, so the
note produced zero LivePhoto pairs.

```text
VIDEO_FALSE_POSITIVE = 0
```

### Invalid or deleted note

The target identity did not appear as an admissible payload note after the
error redirect. The current exact-identity selector returned no current note
and no media.

```text
INVALID_FAIL_CLOSED = YES
```

## Host And Format Evidence

Only hostname families are retained; paths and queries are discarded.

| Role | Observed family | Evidence |
| --- | --- | --- |
| still | `sns-webpic-qc.xhscdn.com` | positive and negative image controls |
| LivePhoto motion | `sns-video-v*.xhscdn.com` | positive payload streams and bounded probe |
| LivePhoto motion backup | `sns-bak-v*.xhscdn.com` | positive payload backup streams |
| ordinary video | `sns-video-v*.xhscdn.com` | video control bounded probe |

A bounded 128-byte representative probe was used only to establish format
families. No media was saved:

- still members: JPEG signature and `image/jpeg`;
- motion members: ISO BMFF/MP4 signature and `video/mp4`;
- ordinary video control: ISO BMFF/MP4 signature and `video/mp4`.

This is evidence, not host admission. The Provider allowlist and download
authority remain unchanged. A mapper implementation must still produce
URL-free structural output before any later Provider-builder host review.

The observed still URLs did not expose a stable filename extension, and no
trusted item-local payload format field was proven. The representative JPEG
signature establishes the observed format family only; it does not authorize
an offline mapper to label every still as JPEG. A later mapper must leave
extension population fail-closed or require separately verified format
evidence before creating `XhsRichMediaAssetSpec`.

## Capability Entry Decision

All required gates passed:

1. two independent LivePhoto samples were identity-confirmed;
2. both contain same-item marker, still, and motion evidence;
3. the ordinary gallery produced zero false positives;
4. the video note produced zero false positives;
5. the invalid note produced no false media or success; and
6. no positional, URL-similarity, or `firstNoteId` inference was used.

```text
XHS_LIVEPHOTO_RELATIONSHIP_MAPPER_READY = YES
XHS_LIVEPHOTO_ASSET_SPEC_MAPPER_READY = PARTIAL_BLOCKED_BY_FORMAT_EVIDENCE
XHS_LIVEPHOTO_PROVIDER_INTEGRATION_READY = NO
```

The first status authorizes only a later offline relationship mapper that
preserves same-item ownership and source order. The second remains blocked
because no trusted payload-native format or per-item extension was proved. The
third remains closed because host admission, backup selection, final
`MediaAsset`, Provider manifest, and download authority have not been reviewed.

## Expected Next Mapper Boundary

The next PR may accept an already-selected exact target-note mapping and emit:

- an exact requested/payload identity gate whose values are not retained;
- ordered, URL-free and identity-free relationship structure for a later
  `XhsRichMediaAssetSpec` builder;
- explicit still/motion relationship evidence; and
- fixed `unverified` format status without extensions.

It must not construct `XhsRichMediaAssetSpec`, `RichMediaContractV1`, final
`MediaAsset` values, manifests, tokens, or URLs. The current XHS Provider must
continue emitting no rich-media contract until later, separately reviewed
format, host, and wiring tasks.

## Evidence Artifact

The machine-readable, URL-free record is:

```text
research/evidence/xhs_livephoto_payload_evidence_v1.json
```

Offline tests validate its schema, identity fingerprints, positive and
negative gates, hostname-only values, format families, forbidden heuristic
flags, and the computed mapper-readiness decision.

## Runtime Impact

```text
PROVIDER_DIFF = 0
REGISTRY_DIFF = 0
PUBLIC_ROUTE_DIFF = 0
UI_DIFF = 0
DOWNLOAD_DIFF = 0
HOST_ALLOWLIST_DIFF = 0
MIGRATION = NONE
DEPENDENCY_DIFF = 0
PRODUCTION_CHANGE = NO
```
