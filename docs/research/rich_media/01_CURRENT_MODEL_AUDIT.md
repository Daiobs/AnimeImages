# Current Media Model Audit

## Scope and method

This is a code-level audit of the AnimeImages baseline at
`153e3f438142da71b6dc1f217ce002352c4968d7`. It is not a runtime change and it
did not execute a Provider, parser, download, database query or network request.

The audit follows media from adapter output through display, Browser delivery,
server-generated output and compatibility serialization. Paths are named for
review; no source or media location is retained here.

## Current canonical objects

`xhs_download/services/assets.py` defines the two current domain objects.

| Object | Current fields | Current meaning |
| --- | --- | --- |
| `MediaAsset` | physical type, location, platform, preview/cover metadata, filename/extension, quality/size, request headers, admitted hosts, Provider/cache/manifest identity | One physical image or video plus delivery provenance |
| `ParseResult` | platform, `post_type`, filename, `assets` tuple, metadata | One parse result with a flat ordered tuple of assets |

`ParseResult.image_assets` and `ParseResult.video_assets` are filtered views of
the tuple. Each view preserves relative order within its physical type, but the
two views cannot reconstruct cross-type order. `to_legacy_dict()` similarly
produces separate image and video collections.

Current physical media types are effectively `image` and `video`. Current
`post_type` values are mostly `image`, `video` and `unknown`. A type does double
duty as both a presentation hint and a coarse content model.

## Producer audit

| Producer | Asset behavior | Order evidence | Model loss |
| --- | --- | --- | --- |
| Xiaohongshu adapter | One video asset, or an ordered tuple of image assets | Image extraction order is retained in tuple construction | No Live Photo relationship; cover is attached to video metadata |
| Douyin adapter and Provider strategy | Ordered image tuple or one selected video | Image list order is retained; selected video is singular | No mixed post or pairing model |
| Bilibili adapter | One or more video candidates/segments with one cover value | API segment/candidate order is normalized before tuple construction | Only baseline video pages; no Dynamic/Opus image or Live Photo contract |
| `gallery_dl_generic` | Appends image and video assets in normalized tool-output order | The mixed tuple retains append order | `post_type` becomes `video` when any video exists, so mixed meaning is lost |
| `yt_dlp_generic` | One selected video asset | Singular | No rich-media structure |
| merge providers | One server-generated video asset | Singular | Correctly models generated output, not source relationships |
| third-party strategy | Passes the selected Provider's `ParseResult` through | Provider tuple retained | Adds Provider outcome metadata only |

This audit found no alternate canonical ordering field. Therefore:

`ASSET_ORDER_SOURCE = PARSE_RESULT_ASSETS_TUPLE`

That decision applies to a future rich-media extension. It does not claim that
today's templates preserve mixed order.

## Consumer audit

| Consumer | What it reads | Ordering / authority behavior | Rich-media impact |
| --- | --- | --- | --- |
| Provider manifest | Individual asset identity, Provider/cache identity, location and physical type | Exact per-item equality is required | A group must never authorize a member |
| Download admission and routing | Individual download-item dictionaries | Validates each selected item and its current strategy | Every related member remains independently admitted |
| Single result context | `image_assets` and `video_assets` separately | Cross-type order is discarded | Needs an ordered presentation projection before multimedia rollout |
| Single result template | Video section, then image section | DOM order differs from source tuple for mixed media | Cannot be treated as canonical order |
| Batch result context | Separate `image_items` and `video_items` | Cross-type order is discarded | Same projection gap as Single |
| Batch result template | Image gallery, then video tools | DOM order differs from both Single and possible source order | Requires one shared ordered projection |
| Browser photo archive | Selected image DOM nodes | Preserves image DOM order only | Live Photo motion member is outside the photo archive contract |
| Ordinary-media Browser download | Selected video descriptor | Singular ordinary video flow | A relationship adds no delivery authority |
| Server ZIP / generated output | Client-selected item array | Processes submitted order under existing validation | Must not infer group membership as permission to add assets |
| Compatibility serializer | Separate image URLs and video items | Mixed order cannot round-trip | Legacy output must remain unchanged while rich data is optional |
| Batch summaries and filters | Counts separate image/video lists | Counts physical assets | May add semantic labels later, not in this baseline |
| Health/admin inspection commands | Assets, physical types and first video | Operational only | No relationship awareness today |

## Cover audit

`cover_url` and `cover_preview_url` are presentation metadata on a video asset.
They are not members of `ParseResult.assets`, do not have their own canonical
asset identity and are not independently download-authorized by the current
model.

The v1 contract preserves that boundary:

`COVER_IS_NOT_CANONICAL_ASSET`

A future parser may emit the same physical image as an explicit canonical asset
only when it is genuinely part of the post and receives its own admitted asset
identity. A preview cover alone must not be promoted automatically.

## Gaps confirmed by the audit

1. A flat tuple cannot express that one still image and one motion video form a
   Live Photo.
2. Physical media type and post semantics are conflated.
3. Mixed source order exists in some producers but is lost in current display
   projections.
4. Animation has no explicit role and is liable to be treated as a static image
   or ordinary video.
5. Rich text has no ordered inert text/media block contract.
6. There is no closed cross-reference validator for asset IDs and groups.
7. A cover can be mistaken for source content if future code treats all visual
   fields as assets.
8. The current manifest is deliberately location-bearing and delivery-specific;
   it is not suitable as a URL-free semantic relationship document.

## Compatibility baseline

The future runtime integration should add an optional, AnimeImages-owned rich
media value beside the existing fields. Absence means the legacy result remains
valid. Existing `assets`, filtered views, `to_download_item()` and
`to_legacy_dict()` remain authoritative until each consumer is explicitly
migrated.

Invalid rich-media data must fail closed before view construction. It must not
silently fall back to a partially trusted relation while continuing download.

No runtime object, parser, Provider, view, template, JavaScript, download path,
model or migration is changed by this research baseline.
