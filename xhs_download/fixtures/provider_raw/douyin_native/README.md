# Douyin Native pure mapping fixtures

Both JSON documents are complete, controlled **synthetic** inputs. They are not
captured Native responses. `provenance.complete: true` means the entire constructed
input is retained, including `upstream_only`; it does not certify a real recording.
No Provider or media requests were used to create or validate these fixtures.

## Input boundary

`DouyinMapper.map(raw, source_url)` accepts a raw aweme dictionary or one explicit
Native `aweme_detail`, `aweme_list`, or mobile-share `item_list` envelope. The
selected `aweme_id` must match the actual source identity. Duplicate matches,
mixed envelope shapes, legacy results, normalized service markers and absent
media fail closed. Envelope/API headers are not treated as media headers.

Opaque short links are not resolved by this mapper. P12 must pass the actual
resolved work URL as `source_url`; copying an arbitrary raw aweme ID into a
fabricated source URL is not resolution evidence. Fetch, HTML extraction,
capture, routing and legacy integration remain outside this module.

## Local schema evidence

- `services/platforms/douyin.py`: `fetch_mobile_feed_aweme` and
  `fetch_mobile_share_aweme` return selected raw aweme objects before
  `parse_aweme`; `get_video_media_candidates` reads `video.bit_rate[]` and root
  `play_addr`, `download_addr`, `play_addr_h264`, `play_addr_265` address objects.
- `get_image_items` recognizes `images`, `image_infos`, `image_list`, `imageList`.
  `get_image_url` recognizes string addresses, `url_list`, `origin_url`,
  `large_image`, `display_image`, `image`, `image_url`, `download_url_list`.
- `get_aweme_metadata` supplies the `share_info.share_title` fallback;
  `get_video_cover_urls` also reads `big_thumbs[].img_urls/img_url`.
- `xhs_download/tests.py`, tests beginning at 3598, supplies controlled examples
  of `playwm` URLs, raw image CDN URLs, and audio addresses under `video.play_addr`
  (`mime_type=audio_mp4`, `/play/music/`, `/obj/ies-music...mp3`). The new mapper
  preserves these audio resources instead of discarding them.
- Installed yt-dlp 2026.6.9, `yt_dlp/extractor/tiktok.py:401`,
  `_parse_aweme_video_app`, is also called by `DouyinIE` at line 1475. This path
  reads `play_addr_bytevc1`, `bit_rate[].FPS`, native `data_size`, dimensions,
  and video/download-address `duration` in milliseconds, verified against the
  existing isolated compatibility Runtime's installed package source.
  No TikTok-only web parser facts are used as Douyin evidence.

`music.play_url` has no existing real recording in the inspected evidence. Its
support is an explicitly authorized synthetic shape, not a claim of observed
Native coverage. Explicit `tracks`, service-context, DRM and segmented fields in
unit-test mutations are also controlled declarations, not recorded split-track
responses. Unknown business codes stay `unavailable`; no unverified code is
declared to mean deletion/private content.

## Mapping boundaries

- All advertised video formats and root addresses survive. Equal URLs with
  different format observations remain distinct variants; each retains its own
  label, dimensions, FPS, bitrate and size. Same-URL access restrictions are
  combined conservatively, without filling a missing-context observation from
  a different observation or leaking restrictions to unrelated URLs.
- Image alternatives remain within their content Item. Root image dimensions
  apply only to that image's own `url_list`, not different preview/display
  representations. Missing image dimensions can use consistent facts for the
  exact same URL. No cross-URL dimension extrapolation is performed.
- Music and explicit native audio addresses are separate audio Items, with valid
  Item defaults. They cannot become the video's default variant. This says
  nothing about page switching or a Work-level audio default.
- No executed Native video/audio pairing structure was established. No
  `merge_with` is invented. A controlled video-only Item without a default is
  rejected as `DOUYIN_ITEM_WITHOUT_DEFAULT` under contract 1.4; this is not a
  real-recording blocker or a request to change the kernel.
- Video duration uses milliseconds. Music duration uses seconds only when the
  output is the single audio Item from that `music.play_url`; it never fills
  missing video/gallery Work duration. Unknown units/associations stay absent.
- `work_caption_presentation` keeps full body text and separates topics. Cover
  URLs and media URLs retain raw CDN/path/query values. The only media URL
  resolution is an explicit protocol-relative reference to `https:`. No
  `playwm` replacement, ratio expansion, image reformatting or probing occurs.
- Existing Douyin host suffixes and #145 request-context semantics are reused.
  CORS remains unknown without facts. No header values enter Work. Provenance
  contains fixed schema paths, not arbitrary upstream display strings.

`tests_provider_adapter_douyin.py` uses the Django-discoverable test naming
convention and guards every map invocation against network, file, subprocess,
DNS, clock and sleep I/O. The independent reviewer boundary tests also deny DB
connections. These are offline mapping checks, not live end-to-end acceptance.
