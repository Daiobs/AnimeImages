# TianTian raw provenance and gaps

These files are inputs to `TianTianAdapter.map(raw, source_url)`, not normalized
payloads, candidates or old ParseResult objects. Four recordings reuse historical
archives without new requests; two TianTian XHS recordings come from the
separately authorized requests described below. This fixture/test sidecar makes no requests. Sanitized
URLs are observations, not current download authorization or playback evidence.

## Complete historical recordings

The four historical recordings listed below reuse the complete `observation.records[0]`
from private `census-phase1b-v1/results/<archive-id>.json.gz` archives beneath
`~/animeimages-scout/.local/`. Each archive has exactly one returned JSON object,
`truncated=false`, `redacted_fields=0`, `not_exported_types=[]` and
`completeness=returned_object_export`. The historical overview records
`RETURNED` and `TARGET_CONFIRMED`. Request URLs come from the matching `state.json`
unit; the raw objects themselves contain no work ID or source URL.

| Fixture | Archive ID | Finished (UTC, 2026-10-13) | Request-derived work ID |
| --- | --- | --- | --- |
| `recorded_acfun.json` | `40b6b3302653089b0ed07351` | 07:37:31 | `ac37303684` |
| `recorded_weibo_gallery.json` | `e162b21c7cd6812ff941e9cc` | 07:41:31 | `RkWevk3D3` |
| `recorded_bilibili.json` | `d64c64a5f532f329bae63533` | 08:05:49 | `BV13x41117TL` |
| `recorded_douyin.json` | `6e94a11302af1776b2db3956` | 08:11:10 | `6961737553342991651` |

Original compressed archive SHA-256 values, in the same order:

```text
525a477a2b14d97dcf15167a9fc8d772022ac19aae19b2ba72628195d45b377c
5a452e8ffc40219c425b0618c97d2c256121cfbd42e441b279c7f6947e91f947
71571831b6c963d42f56fa33325d79a5656b4e5d8ced19c4e4f71981031778af
c705e2d5cf5e4e180afdecf7c9feb239f3fd755be7fab3904875606b79351d49
```

The repository's AcFun/Weibo `native_replay` copies omit fields and replace prose
and URLs. They were locators only, never the fixture input. P2 `recordings.sanitize`
processed the complete original JSON envelopes. Bilibili's `upsig` query value
was additionally replaced by `REDACTED`, because P2 does not recognize that
provider-specific signature name. No key, container, resource, quality label,
byte size or non-secret raw fact was reconstructed or omitted. ASCII JSON escapes
preserve the original text values.

## Expected mapping facts

- All four have `code/data/msg`; `data` has only `desc/images/videos`. They do not
  state native work ID, author, duration, tracks, dimensions, FPS, bitrate,
  original image format facts, request context or browser/CORS capability.
  Work identity comes from the request path and must not be attributed to the
  Provider. `platform` and `provider` remain empty until routing fills them.
- AcFun has one still URL and one video with seven ordered streams, including
  distinct `480P` and `540P` labels despite equal sizes and a shared path. The
  root video URL aliases the `2160P` stream, not an eighth independent format.
- Douyin has one still URL and four video streams: `540p`, `720p`, `1080p`, and
  original (`\u539f\u753b`). The root aliases `1080p`. Explicit P labels establish
  height; original does not establish a height. Original is the default even
  though its height is unknown. URL bitrate/quality hints do not invent facts.
- Bilibili has one still URL and one MP4 URL with an empty streams array. No
  height, quality, size or track fact is supplied. `deadline` is an observed
  expiry, not permission to renew the URL. Ordinary Referer is not a special
  required header (INV-03).
- Weibo has nine ordered JPG URLs and no videos. No height is inferred from
  the `osj1080` path. Generic images accompanying a video have no explicit
  LivePhoto relation: no positional pairing is allowed.

## Approved XHS recordings: no raw pair facts

`recorded_xhs_unpaired_media.json` is the complete JSON from one separately
approved TianTian x XHS request, supplied by the parent task after P2 sanitizing.
The reported response was HTTP 200 with JSON `code=200`. Its `data` contains only
`desc`, nine string entries in `images`, and nine `videos` objects containing
`url` and `streams: []`. It supplies no type marker, pair ID, relationship field,
or shared resource-path identifier. Source tokens and signatures are not
reproduced in this provenance note.

`recorded_xhs_unpaired_media_followup.json` comes from a second TianTian x XHS
request explicitly authorized by Human as an additional request, not an automatic
retry or an extension of the first request's budget. The parent supplied its
complete JSON after P2 sanitizing. The reported response was again HTTP 200 with
JSON `code=200`: `data` has only `desc`, six image strings and six video objects
with `url` and `streams: []`. It still contains no type or pairing fields.

Human confirmed that the source work is LivePhoto. That external confirmation
does not supply relationships to `map(raw, source_url)`: equal array lengths do
not authorize zipping images and videos, and Native `note.imageList` evidence
is not TianTian raw. The existing offline boundary test runs both recordings:
it preserves 18 resources (nine image and nine video items) for the first, and
12 resources (six image and six video items) for the follow-up, in their
respective raw orders, with `groups=()` for both. The still URLs have no path extension; that missing
format fact remains missing. The video paths explicitly end in `.mp4`. No size,
height, quality or track facts are invented; tracks remain unknown.

Human approved this P5 boundary: preserve all resources without generating
groups in the absence of per-item pair facts. TianTian's cover + short-video
presentation is background knowledge, not positional-pairing evidence. Mixed
notes can contain both Live Photos and ordinary stills. P10 will use Native's
same-imageList-item facts; combining Native pairs with TianTian media is a later
step-3 enrichment decision. Shadow comparisons of already-combined old results
will use OLD_RESULT_MERGED. This is not LivePhoto groups acceptance. The set has
six real recordings and four synthetic inputs.

## Existing caption semantic boundary

Caption expectations use the existing `work_caption_presentation` plus `_TOPIC`
from `services/work_caption.py`. P5 does not change that helper or introduce a
TianTian/Weibo-specific splitter. Its whitespace-boundary hashtag grammar is not
paired-`#topic#` grammar: this Weibo recording yields only the opening topic
`\u8fd9\u5f88\u5c3c\u5eb7Z63`, leaves that topic's closing `#` at the beginning of
the description, and retains the embedded `#\u5c3c\u5eb7\u4e91\u521b#` text because
it is not preceded by whitespace. The derived title follows the same helper.
The offline test pins this existing limitation; it does not claim paired-hash
topics are fully normalized. The raw recording remains unchanged.

## Explicit deficits and synthetic inputs

The initial census/local-evidence search found no complete TianTian XHS or
LivePhoto recording. The later approved XHS recordings above close the raw
capture gap, but not the explicit pairing-facts gap. Existing XHS LivePhoto
evidence from Native HTML `note.imageList` remains excluded. There is no claimed
real LivePhoto pair mapping acceptance or invented TianTian pair schema here.

`synthetic_xhs_video.json` and `synthetic_xhs_gallery.json` use the observed
TianTian `code/data/msg` and `images/videos/streams` shape with fabricated
ordinary media, titles and IDs. They establish offline mapping expectations,
not successful TianTian XHS responses. All media hosts are reserved examples.
`complete=true` means the complete constructed input, not a complete real capture.

`synthetic_failure_1001.json` and `synthetic_failure_unknown.json` are constructed
business-code inputs; no complete 1001 recording was found. Both must remain
`unavailable`. An unknown code must not become `not_found` merely because its
message contains deletion language. Parent covers client/fetch/context mapping;
these tests use only map and the public kernel model under `deny_map_io`.
