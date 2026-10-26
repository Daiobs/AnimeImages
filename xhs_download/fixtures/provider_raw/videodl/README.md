# VideoDL P7 Mapping Evidence

`VideodlMapper` consumes the list/tuple of native `VideoInfo` instances from
`videodl_worker.run_exact_backend(..., observe=...)`, before validity filtering
or Worker normalization. It reads stored attributes and native `_extra` only,
without evaluating properties or calling methods. It has no fetch,
capture, backend import, fallback, probing or execution methods. P12 owns the
cross-process capture and must supply the exact `backend_id` and
`backend_category` from execution context. Do not infer those from native
`VideoInfo.source`. Its trimmed text is represented separately by a deterministic
`videodl:source:sha256:<digest>` in `field_sources`, never arbitrary display text,
URLs, tokens or exception prose. The digest is an observation fingerprint, not
an execution identity or proof of a particular backend.

## Complete Synthetic Inputs

Both JSON files here are complete controlled constructions, not real captures.
The `_class` / `fields` envelopes are test serialization only; tests restore
plain attribute-bearing objects before calling the real map entry point.

- `synthetic_video_audio.json`: the same VideoInfo's paired video/audio resources, P label,
  per-resource headers/cookies, and private fields that must not be projected.
- `synthetic_encrypted_segments.json`: N_m3u8DL-RE/manifest and decryption facts.

Additional synthetic tests cover explicit ffmpeg, mp4decrypt and decryption
flags/settings, including literal `--decryption-engine`, `--key` and
`-decryption_key` dictionary options. Declared manifest extensions also mark
segmentation when the URL has no extension. These cases test supplied facts, not evidence that every native
backend emits those extra attributes. Executor options are never executed or
included in Work. The native `raw_data` tree is not recursively interpreted.
Callable settings and arbitrary command modification programs are not executed
or analyzed; their effective encryption state is an evidence gap, not a claim
that their output is unencrypted. No such programs occur in the five replays.

## Reduced Historical Inputs

The replay test reads these existing `fixtures/native_replay` files in place:

| Archive | Native label | Stored outcome |
| --- | --- | --- |
| `0151880293e8f4148ded3846` | HuanQiuVideoClient | MP4 |
| `f0464caa40e3f2debb792893` | WeiboVideoClient | Error, no media |
| `50db251dc5c7b043b8feb342` | AcFunVideoClient | HLS, ffmpeg |
| `9e5cb53d95362646ed898dd6` | FoxNewsVideoClient | HLS, N_m3u8DL-RE |
| `996ddbca94d3f56df92eba8d` | MeipaiVideoClient | MP4 |

These are historical reduced observations: URLs/prose/identifiers were replaced,
native internals omitted, and exact registry execution identity not retained.
They are not copied here or relabeled `complete=true`. Replay preserves the
stored fields, restoring only the unambiguous `NoneType` marker to `None`.
The explicit `ReplayConfiguredBackend` is synthetic invocation context, not a
claim about which backend historically ran. No omitted facts are reconstructed.

The replay checks every Work and Variant field and the complete `field_sources`
mapping; the error-only Weibo record checks its exact safe Failure. It does not
infer tracks, dimensions, sizes, authentication, or execution identity from
omitted fields. Explicit false facts in synthetic inputs retain provider
provenance; absent facts retain contract default/unknown provenance.

Tracks without a native role or other explicit fact stay unknown. Same-object
video/audio URLs carry the native pairing semantics documented below; an audio
URL in a different VideoInfo does not. Retained VideoInfos are not combined into
cross-object quality alternatives or live-photo relationships.
The first usable object's cover is Work metadata, not an invented downloadable
item. Error-only observations do not erase usable media, and an `err_msg` on an
object with usable media is not a total failure. With no usable media, an error
observation yields `UNAVAILABLE/VIDEODL_ITEM_ERROR`; otherwise the result is
`INVALID_RESULT/VIDEODL_NO_MEDIA`. Error prose never enters the result.
Known source IDs take precedence over backend-local identifiers; opaque sources
require a nonempty, consistent identifier. The replaced historical identifiers
are only replay values, not proof of original platform identity.

Exact media-URL aliases retain separate items while sharing restrictive facts:
headers, browser-context denial, DRM, segmentation, method, known tracks and
earliest expiry. Missing facts cannot loosen an alias. Conflicting non-GET
methods or known tracks fail safely. A merge partner belongs to its own
VideoInfo and is never copied through a URL alias. Two objects with the same
video URL retain their own audio partners. Different URLs do not inherit each
other's restrictions.

## Native Pairing Evidence And Contract Rejection

Local static inspection used the already-installed `videofetch-0.9.1` runtime,
pinned by its `direct_url.json` to VideoDL commit
`6cd1de0fe1209380b183b654c65e98a629637873`; no runtime import or request was made.

- `videodl/modules/utils/data.py:18-104`: VideoInfo declares stored media fields
  and puts extension attributes in `_extra`.
- `videodl/modules/sources/base.py:344`: a valid `audio_download_url` selects the
  native all-in-one downloader, without a separate merge flag.
- `videodl/modules/sources/base.py:297-326`: that downloader checks the object's
  video URL, takes its own `audio_download_url`, creates an audio resource from
  it, and merges those two resources. It does not select audio from other
  VideoInfos or interpret this field as an independent soundtrack.
- Neither `requires_merge` nor `audio_asset_present` occurs in this pinned
  package's Python source. Worker projections do not prove such raw facts exist.
- `videodl/modules/utils/cmd.py:109-133` accepts literal option dictionaries.

Two valid, distinct video/audio URLs on the same VideoInfo therefore establish
`video_only` / `audio_only` and `merge_with`. No `requires_merge` field is needed
or consulted. An object with only a valid `audio_download_url` remains an audio
item with `audio_only` tracks and no merge partner; an explicitly audio-only
primary resource also remains an audio item. Identical video/audio URLs do not
establish independent tracks.

Pairing tests use controlled synthetic inputs following this pinned native
schema, not a newly discovered historical capture. Tests reorder objects and
provide higher-bitrate audio on a different object to verify that no best-audio
selection or cross-object pairing occurs, including for exact video-URL aliases.
Explicit unpaired `video_only` is not downgraded to unknown or assigned another
object's audio.

Contract section 1.4 requires a default variant and excludes unpaired
`video_only` from default candidates. An item with only such a variant therefore
fails validation with `item_without_default`; the mapper returns
`INVALID_RESULT/VIDEODL_ITEM_WITHOUT_DEFAULT` as the current contract requires.
The synthetic counterexample verifies this rejection, not a contract defect.
None of the five historical reduced recordings contains a nonempty
`audio_download_url` or explicit `video_only`. No real recording triggers this
rejection, no change to the rule is proposed, and no kernel or contract change
is made here.

No complete real VideoInfo capture or actual-provider acceptance is claimed.
No new provider requests, media requests, dependencies or runtime changes occur.
