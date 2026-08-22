# Rich Media Domain Contract v1

## Status

The offline design baseline is complete. The machine-readable shape and
standard-library validator live in `tools/rich_media_contract/` and are not
imported by production code.

An independent AnimeImages-owned runtime implementation now exists in Draft at
`xhs_download/services/rich_media.py`. It uses closed frozen dataclasses and
does not read this JSON schema or import anything from `tools/`.

Status:

- `OFFLINE_CONTRACT = COMPLETE`
- `RUNTIME_CONTRACT = IMPLEMENTED_IN_DRAFT`
- `FIRST_CONSUMER = BILIBILI_DYNAMIC_LIVEPHOTO`
- `PLATFORM_IMPLEMENTATION = NOT_STARTED`

## Goals

The contract provides an AnimeImages-owned representation for:

- ordered physical image/video assets;
- Live Photo still/motion relationships;
- animation as a presentation role over a physical image or video;
- ordered multimedia posts; and
- inert rich-text blocks that reference canonical assets by ID.

It must remain independent from locations, Provider payloads, credentials,
delivery manifests and download authorization.

## Non-goals

v1 does not define parser APIs, platform endpoints, host allowlists, Provider
selection, rendering HTML, download routing, public rollout, storage, database
models or migration. It does not install or import ParseHub.

## Vocabulary

### Physical media type

Only these physical types exist in v1:

- `image`
- `video`

Animation and Live Photo are not physical media types. Animation is an asset
role. Live Photo is a relationship between two physical assets.

### Post type

The closed post semantic enum is:

- `image`
- `video`
- `gallery`
- `live_photo`
- `multimedia`
- `rich_text`

Unknown values fail closed. Adding a value requires a contract version review.

### Canonical asset

A canonical asset is one item in the top-level `assets` array. Its position is
source order. Its `asset_id` is unique within the document. The contract
contains only structural descriptors:

```json
{
  "asset_id": "asset:example:1",
  "media_type": "image",
  "extension": "jpg",
  "role": "primary"
}
```

No location or delivery field is permitted.

`ASSET_ORDER_SOURCE = PARSE_RESULT_ASSETS_TUPLE`

When runtime integration happens, the rich contract must be built from the
existing tuple without sorting by type, filename, quality, role or group.

### Asset roles

The closed role enum is:

- `primary`
- `gallery`
- `inline`
- `still`
- `motion`
- `animation`

Image assets may use `primary`, `gallery`, `inline`, `still` or `animation`.
Video assets may use `primary`, `inline`, `motion` or `animation`.

Animation retains a physical type and uses a bounded format rule:

| Physical type | Allowed animation extensions |
| --- | --- |
| image | `gif`, `webp`, `avif` |
| video | `mp4` |

Other image/video formats remain valid for non-animation roles under the schema
allowlist. SVG, HTML and arbitrary binary formats are not part of v1.

## Top-level document

The document has exactly six fields:

```json
{
  "schema_version": "1.0",
  "case_id": "fictional_case",
  "post_type": "multimedia",
  "assets": [],
  "groups": [],
  "content_blocks": []
}
```

Unknown top-level fields are rejected. `case_id` is an offline correlation ID,
not a source post ID and not a Provider identifier.

## Live Photo relationship

v1 supports one relationship type: `live_photo`.

```json
{
  "group_id": "group:example:1",
  "relationship_type": "live_photo",
  "members": [
    {"asset_id": "asset:example:still", "role": "still"},
    {"asset_id": "asset:example:motion", "role": "motion"}
  ]
}
```

Invariants:

1. A group has exactly two members.
2. Exactly one member is an image with asset role `still`.
3. Exactly one member is a video with asset role `motion`.
4. Both references resolve inside the same document.
5. A member is unique within the group.
6. One asset cannot belong to more than one Live Photo group.
7. A `live_photo` post contains only fully grouped assets.
8. A multimedia or rich-text post may contain Live Photo groups plus other
   canonical assets.

`RICH_MEDIA_RELATION_IS_NOT_DOWNLOAD_AUTHORITY`

The relationship never authorizes fetching, presenting or downloading a member.
Each member must separately pass current asset, Provider manifest, host, access
and delivery checks.

## Multimedia

A `multimedia` post has at least one image and one video. Its top-level asset
array is the ordered source sequence. Relationship groups annotate that
sequence; they do not reorder or duplicate it.

Current templates split image and video sections. A future presentation layer
must introduce one ordered projection before claiming multimedia support.

## Rich text

Rich text is an ordered list of closed blocks:

```json
[
  {"type": "text", "text": "A plain-text paragraph."},
  {"type": "media", "asset_ids": ["asset:example:1"]}
]
```

Text blocks:

- are inert Unicode plain text;
- contain no raw HTML;
- contain no Markdown link/image syntax;
- contain no location, credential or executable scheme;
- are limited to 4096 characters per block and 16384 per document.

Media blocks contain only existing canonical asset IDs. Every asset in a
rich-text document must be referenced by at least one media block. Members of a
Live Photo group must be referenced together in one media block so presentation
cannot silently split the pair.

No HTML or Markdown renderer is implied. A future UI must render text through
normal escaping and build media components from admitted asset objects.

## Collection limits

| Collection | Limit |
| --- | ---: |
| assets | 64 |
| relationship groups | 32 |
| content blocks | 128 |
| asset references per media block | 64 |
| document nodes | 4096 |
| nesting depth | 6 |

Limits are intentionally conservative and versioned. They are not runtime
Provider or Batch settings.

## Cover boundary

A `RichMediaContractV1` contains no `cover_url`, `cover_preview_url`,
`cover_asset_id`, cover role or cover relationship. Cover metadata therefore is
not automatically promoted into canonical ordering, grouping, blocks, download
items or Provider manifests.

The Provider/result builder is responsible for constructing
`ParseResult.assets` from canonical physical assets before relationship
validation begins. Runtime validation cannot infer upstream cover provenance
from location equality: a real content image may also be reused as a cover and
remain a valid canonical asset when the builder includes it explicitly.

The offline Parser Lab mapper can reject a source record explicitly labelled
with `role=cover`; this does not imply that Runtime can discover cover
provenance from existing `MediaAsset` locations.

`COVER_IS_NOT_CANONICAL_ASSET`

`COVER_AUTOMATIC_PROMOTION = NOT_SUPPORTED`

`COVER_PROVENANCE_VALIDATION = PROVIDER_BUILDER_RESPONSIBILITY`

## Runtime compatibility contract

The Draft runtime implementation adds an optional rich-media value as the last
field on `ParseResult`.

- `rich_media is None`: current legacy behavior remains valid.
- valid rich-media value: legacy assets remain the delivery source while the
  validated document adds relationship and block semantics.
- invalid rich-media value: reject before view construction; do not partially
  trust it and do not downgrade it into a legacy success.

The runtime value contains `schema_version`, `post_type`, typed asset
descriptors, typed relationship groups and typed content blocks. It deliberately
omits offline `case_id`. It cross-validates every descriptor against the same
position in `ParseResult.assets`, including asset ID, physical type and
normalized extension. No runtime field provides a location or download token.

Current Providers do not emit the value. No view, serializer, manifest or
download path reads it in this Draft.

## Versioning

v1 is closed. New physical types, post types, relationship types, roles, block
types or location-bearing fields require a new reviewed contract version. A
schema version change must include compatibility fixtures and a consumer impact
review.
