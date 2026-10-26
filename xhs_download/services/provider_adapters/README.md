# Provider adapter foundation

This package implements P2 of [step 2](../../../docs/goals/unified-pipeline-step2.md),
under [the contract](../../../docs/architecture/unified-pipeline.md) sections 2.1,
2.2 and 9. It is not connected to routing, requests, health tests or downloads.

- `RawMapper`: `name`, `platforms`, and pure `map(raw, source_url) -> Work | Failure`.
- `ProviderAdapter` also provides `fetch(source_url, budget)` and `parse`.
- `Adapter` composes one fetch and one map. A fetch failure skips map. It does not
  retry, cache, schedule, select providers, issue grants or decide delivery.
- Use the existing kernel `invoke(adapter, source_url, budget)` to contain
  unexpected exceptions. Each family maps its expected errors to a kernel
  `Failure`, using `failure_for_http` or its explicit `failure_for_code` table.
  HTTP helpers consume a final response status, not intermediate redirects;
  a successful HTTP status does not mean the payload is a valid Work.
- Fetch must reuse an existing bounded client and respect the supplied budget.
  Parse checks the total elapsed budget after fetch and after map; it does not
  interrupt an uncooperative client or claim to stop remote processing.
  This foundation does not introduce another timeout/executor framework.

For raw data available only inside a subprocess, P3-P10 may provide a RawMapper
and fixture tests only. Do not invent a fetch that returns old candidates or
re-executes an upstream for shadow use. Raw transport/capture belongs to P12.
Human subsequently included videodl in P7: map the Worker's observed VideoInfo
using the five existing reduced recordings, without new requests. Its explicit
backend is provenance, not an adapter fallback; production admission is separate.

Map receives only upstream facts, never ParseResult, MediaAsset, candidates,
manifests or tokens. It must not use network, database, cache or time, nor mutate
the input. The kernel fills routed platform/provider and owns validation,
default selection, filenames, delivery and fact-gap reporting. Use its
`height_from_label` and `fact_gaps`; do not duplicate those rules. Unknown tracks
remain unknown, while explicitly known tracks must be preserved.

D-06 host facts are not authorization or proof that a URL is safe. yt-dlp and
gallery-dl emit `host_policy=()` without adding platform-specific routing.
`service_facts._host_policy` preserves all existing platform suffix lists and
their `existing:` provenance; only sources without an existing list use `()`.
This shared behavior applies to ParseHub, videodl, TianTian and KuKuTool.
An empty tuple means any public HTTPS host that passes every per-hop safety
check, including DNS/address checks; it does not disable those checks or change
the existing HTTP server-attachment path. Its `field_sources` value is
`contract:empty_list:any_public_https_host_passing_per_hop_checks`, including
when ParseHub delegates to yt-dlp. Nonempty lists still constrain every hop.

Offline tests in `xhs_download/tests_provider_adapters.py` enforce the forbidden
direct-import boundary across all adapter modules and provide `deny_map_io()`
for later family fixture tests. Each family must run its actual map with that
guard; foundation fixture tests alone do not verify a later adapter.
The tests are discovered by the existing Django CI entry, without workflow edits.

The [raw fixture format and local tool](../../fixtures/provider_raw/README.md)
preserve upstream structure while redacting sensitive values. These are test
recordings, not a second application result model. Shadow cache hits with no raw
will count `SHADOW_NO_RAW` in P12, without refetching.

## P3: yt-dlp

`ytdlp.YtDlpAdapter` maps a complete yt-dlp info dict, or an already expanded
collection of info dicts. `platforms = ("*",)` describes extractor coverage, not
public admission. Routing still supplies Work.platform/provider. The adapter is
not registered in any live request, health test or shadow hook yet.

- `fetch` reuses `platforms.ytdlp_generic.run_ytdlp_json`, its configured runtime,
  process budget and output cap. It does not use the old Normalizer or candidates.
  `parse` composes that one call with `map`; shadow will call `map` only.
- Every media format is retained in upstream order, including DRM and segmented
  variants. Storyboard thumbnail grids (`mhtml`) are not video alternatives.
  A root URL alias is not another format; same-URL formats in `formats` stay
  distinct. Missing optional facts stay missing, with default labels owned by
  the kernel. yt-dlp `tbr` in kb/s is converted to bit/s.
- Explicit codec pairs establish tracks. Derived `audio_ext=none` does not prove
  no audio when the codec is unknown: the complete PeerTube recording demonstrates
  this case. No BMFF or other media probing is added.
- `merge_with` comes only from one `requested_formats` selection (at the root or
  inside one `requested_downloads` entry). No highest-audio heuristic or cross-item
  pair is invented. Other unpaired video-only variants remain visible but cannot
  become the default under contract 1.4.
- `has_drm`, fragment information, protocol and manifest/segment URL paths supply
  D-02 facts; the kernel owns rejection, reasons and default ranking.
- Required header names come from explicit requirements, separate `cookies`, and
  non-default private headers. Work context is inherited; when the CLI root
  contains a selected URL, that root's credentials stay on that resource only.
  Duplicate resource aliases retain all restrictive facts without dropping their
  format labels. Header and cookie values never enter Work.
  Browser CORS ability is unknown: no media-extension or hostname assumption says
  it is readable. Host policy is empty under D-06, allowing any public HTTPS
  host that passes per-hop safety checks; subsequent authorization and per-hop
  DNS/address checks remain required. Signed Google video `expire` values are
  preserved as UTC expiry, not renewed. `is_original` requires an explicit flag.
- `field_sources` identifies upstream observations, request identity, adapter
  resource identity and contract defaults/unknowns separately. There is no
  supplemental probe source in P3.

Expected CLI error mapping uses a bounded diagnostic, never stderr in Failure:
timeout -> timeout; explicit unsupported URL -> unsupported; explicit removed,
private or nonexistent video -> not_found; HTTP status -> P2's status table;
invalid JSON/result, missing media, malformed/unexpanded raw or identity mismatch
-> invalid_result; other CLI/runtime failures -> unavailable. Nonzero CLI exits
have text, not structured yt-dlp exception objects, so unknown cases remain
unavailable, including the ambiguous "Video unavailable" message. No retry or
account recovery is added.

`recorded_peertube.json` is one complete, sanitized live CLI observation using
the configured yt-dlp 2026.06.09, not the historical reduced census capture.
It retains ten formats (five HTTPS files, five HLS), requested output and all
other serialized fields. Tests replay it without network. The synthetic fixtures
are explicitly labelled, cover YouTube split selections/audio/collections and
do not claim real YouTube success. This round's single YouTube metadata attempt
was refused by its login/bot check; no credentials or retry were used. Both
recording calls disabled format test-downloads; there was no full media save.
Worker raw transport remains P12, and no old parsed result substitutes for raw.

## P4: gallery-dl

`gallerydl.GalleryDlMapper` accepts complete extractor messages: Directory (2)
provides work context; each Url (3) is an emitted media resource. Queue (6) is
an unresolved extractor, not media, and is rejected. There is no recursive scan
for URLs in avatars, thumbnails, recommendations or nested fallback data.

This is map-only under the Human-approved P3-P10 boundary. The current `-J` CLI
filters private metadata by default, including per-file `_http_headers`; it is
not used as a supposedly complete fetch. The existing Worker's `observe`
position sees extractor messages before descriptor compaction, but capturing
and exporting them belongs to P12. A capture must copy each message immediately
because extractors reuse mutable metadata. Neither old path is changed here.

- Work identity comes from native album/post/entry identifiers, not a hash or
  old candidate. A common album ID supports per-image Directory messages;
  conflicting file/Directory album IDs and incomplete/unexpanded collections
  are rejected. Known Itaku image/post URLs must match the emitted work ID.
  An album's missing title is not filled from its first child's title.
- Explicit per-file identity (`media_id`, `file.id`, or `num`) groups variants.
  Files without such an identity remain separate ordered items. Equal URLs or
  heights alone do not merge items. Video and image preview sharing one native
  `num` remain separate items. No Live Photo relation is inferred.
- Dimensions, byte size, labels and codecs come from the Url message. Nested
  file facts require its URL to match; uncompressed size is not transfer size.
  P labels use the kernel helper; missing tracks stay unknown. DRM and segmented
  variants remain present for kernel rule 0, not dropped or downloaded.
  Native `ytdl:` media explicitly marked `_ytdl_manifest=hls/dash` are unwrapped
  to their HTTP media URL and retained as segmented; arbitrary delegated pages
  remain unexpanded failures. No extractor or manifest request is made by map.
- `_http_method` is normalized to uppercase `request_method`; an absent method
  uses the contract's GET default. Non-GET resources remain visible and the kernel
  returns `UNSUPPORTED_REQUEST`. Request bodies/values do not enter Work, nor do
  methods/headers from nested metadata replace the Url's request instructions.
  Unpaired `video_only` variants keep `merge_with` empty and the kernel returns
  `NEEDS_AUDIO_MERGE`; no audio selection is invented (D-03/D-04).
- `browser_fetchable` is unknown: no CORS test, browser observation or metadata
  probe is performed. `_http_headers` names are explicit file instructions,
  including Referer/User-Agent; None deletes a header rather than requiring it.
  Directory headers are not inherited. Generic header facts use the existing
  request-context conventions. Header values never enter Work. An extractor's
  shared Session cookies are not captured by these messages; absence here is
  not evidence that the Session has no context. P12 must preserve the raw
  request-context boundary, not claim the legacy CLI export is complete.
- `host_policy` is empty under D-06, allowing any public HTTPS host that passes
  per-hop safety checks. This is not admission or a grant. `is_original` requires
  an explicit upstream flag. Missing expiry remains unknown, never renewed.
- Expected error messages (-1) map explicit not-found/unsupported/timeout types
  and HTTP status via P2's table, including native quoted `'404 Not Found' for ...`
  messages. Other upstream errors are unavailable; malformed
  messages, unknown media kind and invalid Works are invalid_result. A failure
  after some Url messages still fails, rather than claiming a complete success.
  Diagnostics never include upstream text or credentials.

The two `recorded_itaku_*.json` files reuse complete extractor observations from
2026-10-13 (ItakuImageExtractor, one Directory and one Url each). Their original
captures were not truncated, had no unexported types or redacted fields, and had
SOURCE_ID_MATCH evidence. They are extractor-message recordings, not HTTP
responses or Session dumps. All native fields are retained through P2's sanitizer.
Replay covers all Work fields and missing quality/size honestly; nested image
references do not become extra content. Synthetic recordings cover the broader
message shapes and restrictions. This P4 performs no live upstream or media
request and claims no new live platform acceptance. Routing, shadow hooks,
health tests and user downloads remain unconnected until their scheduled PRs.
