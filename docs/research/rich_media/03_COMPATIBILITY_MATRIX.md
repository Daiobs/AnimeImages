# Rich Media Compatibility and Download Impact Matrix

## Compatibility rule

Rich media is an optional future extension. Its absence must not invalidate an
existing `ParseResult`.

| Input state | Future runtime treatment | Legacy output |
| --- | --- | --- |
| No rich-media value | Accept as current legacy result | Unchanged |
| Valid v1 rich-media value | Validate immediately at `ParseResult` construction | Existing assets and download items remain authoritative |
| Malformed/unknown v1 value | Fail closed before view construction | No partial legacy success from the invalid extension |
| Unknown schema version | Fail closed | No implicit downgrade |

## Producer compatibility

| Producer | Current shape | v1 mapping plan | Behavior in the Runtime Draft |
| --- | --- | --- | --- |
| Xiaohongshu video | One video plus cover metadata | `video`, cover remains non-canonical | Unchanged |
| Xiaohongshu gallery | Ordered image tuple | `image` or `gallery` using tuple order | Unchanged |
| Douyin video/gallery | One video or ordered images | `video`, `image` or `gallery` | Unchanged |
| Bilibili video | Ordered video candidates/segments | Legacy `video` until a dedicated plan chooses one presentation | Unchanged |
| Generic gallery | Potential ordered mixed tuple | `gallery` or `multimedia` without sorting | Unchanged |
| yt-dlp selected video | One ordinary video | `video` | Unchanged |
| Server-generated merge | One generated video | `video`; never source relation inference | Unchanged |
| Future Bilibili Dynamic | Not implemented | First candidate for image/gallery/Live Photo/multimedia | Plan only |

Every current Provider/adapter continues to construct `ParseResult` without a
`rich_media` argument, so the runtime default is `None`.

## Runtime consumer audit

The exact-main audit found 107 `ParseResult` constructors, all using keyword
arguments. The additive last field still retains the old five-argument
positional API for external/legacy callers.

| Consumer | Positional construct? | Keyword construct? | Reads metadata? | Dataclass serialization / equality / repr? | Assumes exact field count? | Reads post type / filtered assets / tuple order? | Can observe optional field now? |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Current platform Providers and adapters | No in repository | Yes | Some | No automatic serialization; normal dataclass equality only in tests | No | Build post type and ordered tuple | No; all omit it and receive `None` |
| Third-party result normalizer | No | Yes | Yes | No | No | Copies post type and asset tuple | No for current Providers; future preservation needs its own consumer change |
| Legacy parser serializer | No | No | No | Calls explicit `to_legacy_dict()` | No | Reads post type and filtered assets | No; method intentionally ignores it |
| Provider manifest | No | No | No | No | No | Reads one physical download item | No |
| Single result context | No | No | Yes | No | No | Reads post type, tuple, `image_assets`, `video_assets` | No |
| Batch result/context | No | No | Yes | No | No | Reads post type and filtered assets | No |
| Gallery and Browser Photo | No | No | No | No | No | Reads explicit image item/DOM order | No |
| Browser Streaming | No | No | No | No | No | Reads one existing physical download descriptor | No |
| Browser/Server ZIP and download-all | No | No | No | No | No | Reads explicit submitted item order | No |
| Mixed export and copy-all | No | No | No | No | No | Reads current rendered item collections | No |
| Tests/debug repr | Yes, compatibility coverage | Yes | Sometimes | Legacy equality preserved; `rich_media` excluded from repr | No | Varies | Tests can inspect it explicitly |

## Consumer matrix

| Consumer | Compatibility requirement | Required future work |
| --- | --- | --- |
| Provider manifest | Continue signing one admitted delivery asset | No group token and no group-level authorization |
| Download routing | Continue validating every selected item | Resolve semantic IDs to already admitted runtime assets only |
| Single result page | Legacy image/video sections unchanged when extension absent | Add one ordered projection for multimedia before rollout |
| Batch result page | Legacy cards and progressive protocol unchanged | Share the same ordered projection as Single |
| Browser ordinary video | Existing one-video descriptor unchanged | A motion member is ordinary video only after independent admission |
| Browser photo archive | Existing selected image order unchanged | Never include a Live Photo motion member implicitly |
| Server ZIP | Existing explicit item list remains authority | Do not expand a selected group automatically |
| Export/raw tools | Existing Staff boundaries unchanged | Add semantic labels only after public/staff review |
| Observability | Existing platform/Provider fields unchanged | Log fixed post/relation classes only, never document text |
| Admin/Policy | No current dependency | No new control surface in contract PRs |

## Order matrix

| Layer | Current order behavior | Canonical? |
| --- | --- | --- |
| `ParseResult.assets` | Producer tuple order | Yes, future source |
| `image_assets` | Image-only relative order | No for mixed content |
| `video_assets` | Video-only relative order | No for mixed content |
| legacy dictionary | Separate image/video collections | No for mixed content |
| Single template | Video section before image section | No |
| Batch template | Image section before video section | No |
| Browser photo archive | Selected image DOM order | Canonical only within that explicit image selection |
| Download submitted item list | Client-selected order | Operational order, not source-semantic proof |

`ASSET_ORDER_SOURCE = PARSE_RESULT_ASSETS_TUPLE`

No future mapper may reconstruct mixed order by concatenating filtered image and
video views.

## Download impact

The v1 relation is descriptive. It cannot create delivery authority.

For every asset selected from a group, future code must still enforce:

1. current request/user access;
2. current Provider and runtime gates;
3. exact asset identity;
4. Provider manifest validation when required;
5. page/media host admission and redirect checks;
6. delivery strategy and generated-output rules; and
7. existing public/staff presentation boundaries.

Selecting a still image must not automatically select its motion video.
Selecting a Live Photo UI group may propose two checked items, but the final
request must contain and validate both independently.

`RICH_MEDIA_RELATION_IS_NOT_DOWNLOAD_AUTHORITY`

## Delivery-class compatibility

| Asset class | Existing delivery contract | Effect of relation |
| --- | --- | --- |
| Ordinary admitted image | Browser image/photo path | None |
| Ordinary admitted video | Browser ordinary-media path when capable | None |
| Server-generated merge | Existing generated-output path | Cannot be converted into ordinary direct media |
| ZIP/archive | Existing explicit selected-items path | Group cannot silently add members |
| Preview cover | Presentation-only proxy/preview path | Not a canonical member |

## Migration and persistence

The runtime contract is a validated in-memory value only. It adds no model,
database column, JSON field or migration. If persistence is ever proposed, it
requires a separate data-retention and backfill review.
