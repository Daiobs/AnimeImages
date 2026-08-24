# XHS LivePhoto Relationship Mapper v1

## Status

```text
REAL_PAYLOAD_EVIDENCE = COMPLETE
RELATIONSHIP_MAPPER = IMPLEMENTED_IN_DRAFT
XHS_LIVEPHOTO_FORMAT_RESOLUTION = BLOCKED_NOT_PART_OF_RELATIONSHIP_MAPPER
ASSET_SPEC_MAPPER = NOT_STARTED
PROVIDER_INTEGRATION = NOT_STARTED
CURRENT_XHS_LIVEPHOTO_RELATIONSHIP_MAPPER_CONSUMERS = 0
CURRENT_PROVIDER_RICH_MEDIA_EMISSION = 0
PUBLIC_SUPPORT = NO
DEPLOYED = NO
```

PR #67 is the merged evidence baseline. It proves exact-note, same-item
marker/still/motion ownership across two LivePhoto samples and the required
gallery, video, and invalid controls. This module implements only the dormant,
network-free relationship layer authorized by that evidence.

## Input Boundary

The mapper accepts:

```text
requested_note_id
payload_note_id
image_list
note_level_video_present
```

The requested identity must already come from the existing Source Identity
flow. The payload identity and `image_list` must already belong to the exact
note selected by the current-note selector. The mapper does not fetch HTML,
resolve short links, parse initial state, select a note, inspect `firstNoteId`,
or search recommendations.

Both identities must be lowercase 24-character hexadecimal values and must be
equal. They are used only for the admission gate and are never retained in the
result. Invalid or mismatched identities produce a fixed-code error without
echoing either value.

## Confirmed Field Shapes

A single bounded recheck of the five PR #67 samples confirmed these type-only
facts without storing payloads or media values:

- `item.livePhoto` is boolean: `true` for positive items and `false` for the
  mixed/negative regular-image items;
- still evidence is a non-empty string at `item.urlDefault` or
  `item.infoList[].url`;
- `item.infoList` is a list whose reviewed entries are mappings;
- `item.stream` is a mapping from dynamic codec-family keys to lists;
- reviewed stream entries are mappings with `masterUrl` and/or `backupUrls`;
  and
- `backupUrls` is a list of candidate strings.

Missing `livePhoto` is treated as negative. A present marker with any type
other than exact boolean is rejected. The mapper reads only the reviewed field
paths, ignores unrelated raw fields, and does not validate or retain candidate
URLs.

## Relationship Rules

A LivePhoto relationship requires all of the following in one `imageList`
item:

1. `livePhoto` is boolean `true`;
2. at least one reviewed still candidate is present; and
3. at least one reviewed motion candidate is present.

Its structural result is:

```text
item_kind = live_photo
relationship_basis = same_item_explicit_fields
member_order = still, motion
format_status = unverified
```

A regular image requires a missing or boolean-false marker, at least one still
candidate, and zero motion candidates. Motion without a positive marker is an
error. A positive marker with a missing still or motion member is an error for
the entire mapping call; it is never downgraded or completed from another
item.

Cross-item search, adjacent or parallel indexes, compacted array alignment,
filenames, URL similarity, dimensions, codec names, and order similarity are
not relationship evidence.

## Order Contract

Each image-note source item maps to one relationship item in the original
`note.imageList[]` order. `source_item_ordinal` is zero-based and only records
source order/location. It never proves pairing.

Future physical expansion is documented, not implemented:

```text
regular image -> still
LivePhoto -> still, then motion
```

The mapper accepts at most 64 source items. `infoList`, codec-family, stream
variant, and per-item candidate counts are also bounded before structural
output is produced.

## Video Note Boundary

When `note_level_video_present` is true, a normal video note returns:

```text
note_kind = video
items = empty
```

The note-level video is never treated as an image-item motion member. If a
video note also contains a complete explicit LivePhoto item, the unreviewed
mixed shape fails closed with `XHS_VIDEO_NOTE_LIVEPHOTO_SHAPE_UNREVIEWED`.

## URL-Free Output

The frozen output types are:

- `XhsLivePhotoRelationshipItemV1`
- `XhsLivePhotoRelationshipMapV1`

`to_structural_dict()` exposes only schema version, fixed enums, candidate
counts, source ordinal, member order, note kind, and aggregate counts. Output,
`repr`, and fixed errors contain no:

- URL, path, query, token, Cookie, or header;
- note identity or fingerprint;
- hostname or codec-family name;
- title or author;
- extension or MIME;
- Provider, manifest, asset ID, or group ID.

## Format Boundary

PR #67's 128-byte representative probes observed JPEG stills and ISO BMFF/MP4
motion, but that is not per-item payload-native format proof. Every mapped item
therefore has:

```text
format_status = unverified
```

The mapper does not populate `jpg`, `jpeg`, `mp4`, `heic`, `webp`, or `avif`.
It does not modify existing rich-media extension sets.

## Future Bridge Requirements

A later Asset Spec mapper remains blocked until it separately proves:

- trusted per-member format and extension;
- motion host admission and redirect behavior;
- deterministic master/backup selection;
- URL-to-asset authorization without leaking source values; and
- stable asset/group identity rules that do not reuse evidence fingerprints.

Only after those gates may another PR review `XhsRichMediaAssetSpec`,
`RichMediaContractV1`, final `MediaAsset`, Provider manifest, registry, UI, and
download behavior. This mapper deliberately imports and constructs none of
them.

## Runtime Impact

```text
XHS_ADAPTER_CHANGED = NO
REGISTRY_CHANGED = NO
PROVIDER_EMISSION_CHANGED = NO
UI_CHANGED = NO
DOWNLOAD_CHANGED = NO
NETWORK_IO = NO
MIGRATION = NONE
DEPENDENCY_DIFF = 0
PRODUCTION_CHANGE = NO
```
