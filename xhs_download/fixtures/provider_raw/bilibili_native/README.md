# Bilibili Native P8 synthetic raw fixtures

Every JSON file here is explicitly synthetic. No complete Native view/playurl
recording was available for P8, and no new request or capture was performed.
These fixtures exercise the lowest API JSON shapes read by
`platforms/bilibili.py:api_get_json`, before `get_play_items` or any legacy
ParseResult, MediaAsset, candidate, normalization, filename or download logic.
They are not replayed legacy objects and do not prove current upstream success.

## Input Boundary

`BilibiliMapper.map(raw, source_url)` is a pure RawMapper. The wrapper contains:

- `view`: complete view API JSON, including its `code` and `data`.
- `playurl`: complete playurl API JSON, including its `code` and `data`.
- `selection.page`: the actually selected page number, not an inferred fallback.
- `selection.playurl_params`: the executed parameters, including `cid` and
  `bvid` or `avid`/`aid`. View identities, source identities, page/cid association,
  playurl response identities and explicit resource identities must agree.
- `selection.page_url`: the actually resolved ordinary video URL, required for
  b23 short sources; map never resolves a URL. Dynamic/opus routes are excluded.
- Optional `view_status` / `playurl_status`: final observed HTTP statuses.

The selected page must exist and match the request. No fallback to the first
page is performed. API cookies, headers and execution parameters are not media
headers. Media context comes only from the playurl media facts, scoped to the
resource. Ordinary Referer/UA values do not create requirements; explicitly
required names do. Same-URL observations independently evaluate browser context
before restrictive facts are aggregated. Header values never enter Work.

## Cases And Authorized Incomplete Results

- `synthetic_complete_page2.json`: an actual-selection-shaped page 2 wrapper,
  with original title/author separate, native size, P label and a same-format
  backup. The API Cookie is a redacted synthetic marker. The two URLs remain
  variants of one video item, with identical quality facts, not new videos or
  invented quality levels. Explicit HTTP media and cover URLs remain HTTP;
  protocol-relative CDN references are resolved to HTTPS as in Native.
- `synthetic_multi_durl.json`: both sequential segments, their order, sizes,
  lengths and backups are retained in raw. Human authorized
  `UNSUPPORTED / BILIBILI_SEGMENTED_DURL`; no Work is constructed, no first
  segment is substituted, and no concatenation or new Group is introduced.
- `synthetic_dash_only.json`: full separate video/audio formats, video backup,
  fractional frame rate, Dolby and FLAC alternatives. Without a complete file
  or an upstream-executed pair, Native returns
  `UNSUPPORTED / BILIBILI_SEPARATE_TRACKS_ONLY`. The current Native API supplies
  no executed pair; the mapper does not guess one, even with one audio format.
  An explicit split-track fact on a durl row is likewise not a complete file
  merely because its URL has a progressive-file suffix.
- A single durl with a manifest/segment path is covered by synthetic mutations
  and returns `UNSUPPORTED / BILIBILI_SEGMENTED_PATH`, not a partial Work.

Tests also combine complete durl and DASH facts. The complete variant remains
the default of one video item; unpaired video variants stay video_only with
empty merge_with and are rejected for download by existing kernel rules. The
same cid's audio, Dolby and FLAC representations form one separate audio item
with format variants, not one independent content item per codec.

Size and dimensions come from the actual media row. View dimensions, requested
qn and numeric quality IDs do not supply variant resolution; an explicit P
label can supply height. Frame-rate fractions are parsed without guessing.
Duration comes from the selected page or playurl timelength milliseconds, not
an unrelated page or aggregate multi-page view duration. Host policy reuses the
existing Bilibili suffixes. URL deadline and explicit expiry are retained.

Native has no observation hook at this boundary today. P12 owns capture and
fetch; this mapper does not import an old result, add I/O, or change old paths.
