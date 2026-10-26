# ParseHub P7 raw fixtures

## Recorded

All four files come directly from the full `observation.records[0]`
object in the existing local census archive directory. No Worker-normalized assets,
public descriptors, or `to_dict()` exports were used to rebuild raw data.
The original object class, every field, media shape, complete Markdown and
existing plaintext content are retained. All archives declared
`completeness=returned_object_export`, `truncated=false`,
`redacted_fields=0` and `not_exported_types=[]`.

### recorded_bilibili_native.json

- Archive: `census-phase1-v1/results/2ff0242a05d8ed32ae20fe6a.json.gz`
- Compressed archive SHA-256: `5997611ff86370985ee64726985b2e02eae2aa26436a9202856433de67d9d76d`
- Finished: `2026-10-13T05:01:14.649137+00:00`
- Original status: `RETURNED`; semantic: `MEDIA_EVIDENCE_PENDING_REVIEW`

### recorded_douyin_native.json

- Archive: `census-phase1-v1/results/323e0274af9f9225e8bfef4f.json.gz`
- Compressed archive SHA-256: `bc2202e8a9cb35b8027ab53ca57b93e79cf38509f7b451eeb50f498bfb8559d3`
- Finished: `2026-10-13T05:04:13.685990+00:00`
- Original status: `RETURNED`; semantic: `MEDIA_EVIDENCE_PENDING_REVIEW`

### recorded_weixin_richtext_short.json

- Archive: `census-phase1b-v1/results/2fb71c141a4a5cd6bb02efa4.json.gz`
- Compressed archive SHA-256: `2a5fa1f7b6e291aa9e356d60bfcf44a7ee70abb08f96452e40b00205e9d83fe0`
- Finished: `2026-10-13T07:41:43.320934+00:00`
- Original status: `RETURNED`; semantic: `STRUCTURE_UNKNOWN_OR_EMPTY`

### recorded_weixin_richtext_long.json

- Archive: `census-phase1b-v1/results/b57196ed22696db0ca991f21.json.gz`
- Compressed archive SHA-256: `19939718a3b8f230bf60a94d2f59af2c9e498f7a0fc31076133e05c4d9757d3c`
- Finished: `2026-10-13T07:41:35.969303+00:00`
- Original status: `RETURNED`; semantic: `STRUCTURE_UNKNOWN_OR_EMPTY`

The old census status is not upgraded by these tests. These are historical
returned objects, not current provider availability, target-confirmed playback,
or real download evidence. No new upstream or media requests were made.

## Sanitization

Each complete object is wrapped in the P2 recording envelope and processed by
`services.provider_adapters.recordings.sanitize/check`. Credential and signature
values are replaced with `REDACTED`; object keys and non-secret facts remain.
An additional URL-query pass masks `upsig` (case-insensitive, decoded key), which
the P2 sanitizer does not recognize. Exactly one such value was replaced in the
Bili raw URL. The Douyin cover's `x-signature` is also redacted. Tests explicitly
check these values and run the independent P2 scanner. No signed URL is fetched.

## Synthetic

`../parsehub_xhs_images/synthetic_live_photo.json` is a constructed page-state fixture, not a
recording. It exercises same-imageList-item livePhoto groups, independent stills,
stream backups and no positional pairing. Additional raw-object, identity,
header-scope, alias and Markdown edge cases are explicitly synthetic in
`tests_provider_adapter_parsehub.py`.

## Mapping And Worker Comparison

The Bili and Douyin records each retain one native VideoRef, dimensions, cover
and unknown tracks. Class-specific download headers are not invented. Bili's
URL deadline maps to expiry; explicit expiry is separately tested. The raw
MediaRef duration is item-level; no aggregate Work duration is inferred.

The two full Weixin records yield 4 and 30 body images, respectively. Markdown
is rendered only in memory to identify explicit img URLs; it does not replace
raw content/desc, enable extensions, discover linked pages, or persist generated
HTML. Empty, fragment and data:image placeholders are skipped. Body order and
typed tail ordering follow `tools.upstream_parsehub_worker.explicit_richtext_media`.
Unknown image path extensions stay unknown, even when a URL has a wx_fmt query.

Set `PARSEHUB_COMPAT_PYTHON` to the existing pinned ParseHub Runtime to run the
optional offline parity test. It reconstructs the recorded RichTextParseResult
without invoking constructors, parsers or download methods and compares both
functions on exactly the same complete sanitized recording. The child blocks
network before loading native libraries and then installs the Worker's full
offline guard. The parent mapper runs under `deny_map_io`.
