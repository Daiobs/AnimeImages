# KuKuTool raw provenance and mapping limits

These are inputs to `KukutoolAdapter.map(raw, source_url)`. There are two complete
historical recordings and three explicitly synthetic JSON objects. This task
made no upstream, sidecar, media, playback, or download requests. Historical
signed URLs are sanitized observations, not current access authorization.

## Historical recordings

Both recordings copy the complete `observation.records[0]` from private
`~/animeimages-scout/.local/census-phase1b-v1/results/<archive-id>.json.gz`.
The request URL comes from the matching unit in that archive's `state.json`.
Both archives report `RETURNED`, capture source `API decrypted business response`,
one returned object, `truncated=false`, `redacted_fields=0`,
`not_exported_types=[]`, and `completeness=returned_object_export`.

| Fixture | Archive ID | Finished (UTC, 2026-10-13) | Request-derived work ID |
| --- | --- | --- | --- |
| `recorded_acfun.json` | `1d723e3f60f5f7b7634cd84a` | 07:37:39.005216 | `ac37303684` |
| `recorded_weibo_gallery.json` | `271e3db7ef010594a96e1eb2` | 08:04:52.067357 | `RkWevk3D3` |

Original compressed archive SHA-256 values, in table order:

```text
defba1a906fe785fc237ae1e6eca34fa3b6534411ae7cb8606ac7920b3e20250
ee38a0bd2999f1c00a49ee5c71057fd384cf57026408ac3280d039cfa0ce0dae
```

P2 `recordings.sanitize` processed the full envelopes and `recordings.check`
verified the results. AcFun `sign` and Weibo `token` values become `REDACTED`;
keys, containers, order, prose, numeric facts and URL host/path remain intact.
ASCII JSON escaping preserves the original text. No `native_replay` copy,
normalized payload, candidate list, manifest, or legacy conversion was used.

### Observed facts

- AcFun directly returns `title/type/cover/url/videos/pics`, not `code/data`.
  Its one video has `url` and seven ordered `video_fullinfo` rows, each containing
  only `url/size/type`: 360P, 480P, 540P, 720P, 1080P, 1080P+, 2160P. The two
  root URLs alias the 2160P row. 480P and 540P have the same size and path but
  remain distinct labels. The cover also appears as a one-element `pics` list.
  That list is preserved as an image item; it is not an observed LivePhoto pair.
- Weibo directly returns `title/type/videos/pics`; there is no `cover` key.
  There are no videos and nine ordered image URLs at `data.hellotik.app` with
  no path extension. Their distinct original tokens sanitize to the same URL.
  Redaction does not remove the nine positions or establish JPEG/container facts.
  The input has no explicit cover; the mapper must not invent one.
- Neither recording gives a native work ID, author, duration, width, height,
  FPS, bitrate, tracks, CORS, request headers or explicit original-file status.
  Only AcFun's P labels justify height. Request-derived identity is attributed
  to the request; routing still owns the empty `platform/provider` fields.
  Unknown tracks remain unknown. URL query hints and image paths are not probes.
- Caption mapping uses existing `work_caption_presentation` and `_TOPIC`.
  The replay tests do not change that helper or add platform-specific parsing.

## Synthetic inputs

`complete=true` means the complete constructed input, never a real capture.
All `/synthetic/` media paths and source IDs in these three inputs are fabricated.
Host suffixes intentionally match existing platform policy; no host rule is added.

- `synthetic_sidecar_best.json` follows the unconverted envelope constructed in
  `test_douyin_kukutool.py::test_sidecar_payload_converts_to_existing_kukutool_shape`:
  `contract_version/status/cached/cache/timing/best`, with `url/final_url`, size,
  bitrate, quality score and object key. It is fed directly to the new adapter,
  never through `convert_sidecar_payload_to_kukutool_payload`. A descriptive
  ultra-quality label is not an original-file, height, track or container fact.
- `synthetic_sidecar_resources.json` extends that envelope with `resources`,
  a matching `best` alias, and fields explicitly accepted by the old converter:
  `type/quality_label`, `size/size_bytes/verified_size`, and `bit_rate`. The
  old converter's acceptance of a field is schema evidence, not a real response.
  Explicit 1080P+ establishes height; 4K and ultra-source prose do not. Only the
  original label establishes original status. A fabricated custom header proves
  that only its name reaches Work; ordinary headers and all values do not.
  The null alias size must not erase the `best` size. No bitrate is inferred
  from URL query hints and no default is chosen merely because it is `best`.
- `synthetic_generic_audio.json` extends the decrypted direct-dict shape using
  the existing generic normalizer's accepted `audios` array form (string and
  object rows). It is not a recording or converted normalizer output. Audio
  remains a separate item, not a video variant, and the raw contains no track
  declarations, so the media suffix or item kind must not fabricate tracks.

## Scope and gaps

The phase1 and phase1b unit/result archives were inspected for KuKuTool responses.
Phase1 contains no successful KuKuTool returned object. Phase1b contains 14
returned objects; none is a successful Douyin response or contains audio arrays.
Two small representative inputs were selected here; other AcFun captures are
redundant and other image/video captures are not claimed as tested.

No complete real sidecar response was found or requested. Human authorized P6
sidecar map plus synthetic envelope tests only; sidecar fetch/raw capture belongs
to P12. Existing `capture_upstream` uses the URL helper and cannot establish
unconverted sidecar capture. These tests do not prove sidecar acquisition,
network access, current playback, file delivery, or production acceptance.

Replay verification runs the actual adapter twice under `deny_map_io`, checks
input immutability and frozen outputs, every Work field and variant fact,
field-source attribution, all recorded positions, and model-derived defaults.
No contract, kernel, old path, runtime, database, or daily 8000 process is changed.
