# Native XHS mapper inputs

All fixtures here are controlled synthetic inputs, not live recordings or
production acceptance. They use existing Native page-state and current-note DOM
shapes; no Provider, page, media, DNS, or database requests were made.

- `synthetic_gallery.json`: full constructed `note.noteDetailMap` input with
  ordinary images, image renditions, and same-item `livePhoto`/`stream` evidence.
- `synthetic_video.json`: raw page state plus actual-page execution metadata;
  video-note imageList is cover evidence, not downloadable gallery content.
  The API Cookie is a redaction sentinel and does not imply media authorization.
- `synthetic_richtext.json`: full constructed HTML using the existing
  `.note-content`, `#noteContainer`, and `data-note-id` selectors. It exercises
  current-note text and embedded images, not an invented JSON richtext field.

`XiaohongshuMapper.map(raw, source_url)` accepts HTML text, a complete state
object containing `note`, or an envelope containing `html` and/or `page_state`,
with optional `page_url`, `http_status`, `request_headers`, `response_headers`.
The latter headers describe the page request/response and are never inherited
as media access requirements. Legacy ParseResult, XhsPageState, candidate,
snapshot, and rich-media projections are not inputs. There is no fetch method.
Recognized Native script assignments may contain literal `undefined`, treated
as absent (`null`) outside quoted strings. Their full tree and recommendation
scope are retained; arbitrary target-key fragments are not promoted to notes.

Media URL values retain scheme, path and query. Only relative DOM image
references use the actual page URL as their HTML base. Root image dimensions
belong only to urlDefault or its exact aliases, never to different previews;
video-root dimensions do not describe all encodings. Native `avgBitrate` is bps,
stream-row `duration` is milliseconds, `qualityType` is a label, and explicit
`videoCodec`/`audioCodec` describe tracks. These spellings/units are corroborated
by the installed yt-dlp `extractor/xiaohongshu.py`; its output is not an input.
Unitless video-root durations remain unknown; videoBitrate alone is not total
bitrate. No AV pairing, browser readability or original quality is inferred
from platform, codec family, API headers or imageScene. `urlPre` is retained
without other URLs' dimensions. Bare `video.consumer.originVideoKey` is not
expanded or probed; a key-only input has no usable URL and returns NO_MEDIA.

Same-URL restrictions are combined across observations, evaluating missing
context separately for each observation and its applicable parents. Values of
headers and request bodies never enter Work. Groups only relate the still and
motion from a single explicit livePhoto imageList entry. No cross-Provider
enrichment is performed.

HTML is bounded to 1,000,000 characters, 10,000 DOM nodes and depth 32; JSON
uses the existing page-state limits. Over-limit input fails instead of being
silently truncated. No generated or raw HTML is saved in Work. Real Native
recording/capture remains a P12 evidence gap, not claimed by these fixtures.
Structured media order is authoritative. Current-body images are used only
when structured media is absent, with applicable current-note constraints;
body text can independently fill missing structured description.
