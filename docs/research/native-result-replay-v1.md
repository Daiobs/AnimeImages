# Native Result Replay And Shared Normalization v1

## Scope and evidence

2026-10-13, stacked on PR #94 at `47219dea5b099d9060333ff9d299f2f735288c80`.
Scout input version: `728610da5b1a2f44c66066b4a83cbbee09c72baa`.
This is an offline conversion increment, not new platform discovery, production
activation, resource authorization, or file-delivery acceptance.

The frozen Phase 1/1B index has 5,627 historical rows. Of these, 1,349 archive
entries contain 1,360 top-level captured records. The earlier label "1,349 native
objects" counted archive entries, not every top-level record or successful work.
The other 4,278 rows have no native conversion input. Twenty-three observed tags
overlap; they are not 23 independent contracts or mutually exclusive groups.

All original 5,357 evidence files retain their hashes. Original execution status,
identity decisions, the 20 confirmed service-media records and three insufficient
records remain unchanged; later pending evidence is not promoted to confirmed.

## Same-input changes

| Provider / real fixture ID | Before | After / actual path |
| --- | --- | --- |
| ParseHub `b57196ed22696db0ca991f21`, held-out `2fb71c141a4a5cd6bb02efa4` | Rich text contained explicit Markdown images but produced no candidates. | Worker reads only explicit body image references, without following links or scripts: 30 and 4 ordered image candidates. Existing typed roles are preserved. |
| gallery-dl `5d6c88d0a3cb75de9a7b28d4` | Scoped `file.width/height/duration` discarded before shared normalization. | Exact-message-URL descriptor contributes 1920 x 1080 and 105 seconds; sibling player/HLS URLs are not substituted. |
| gallery-dl `3f3b2e281194564de69755e1` | M4A candidate had unknown media type. | Shared conversion retains audio type; absent codec, pairing and track information remain unknown. |
| yt-dlp `421dcc02aca139ef0727c86a` | Language disappeared between Worker and candidates/Lab. | `language` retained through candidates, Lab serialization and both page consumers. 565 candidate occurrences across 55 archives regain declared language. |
| yt-dlp `88e9b92a7c517fb33ce435a4` | One list-valued optional thumbnail URL crashed processing of a valid MP3. | Invalid optional thumbnail is omitted only from processing, not the capture; actual media is retained as audio. No thumbnail is promoted to media. |
| videodl `996ddbca94d3f56df92eba8d` | Cover inherited the video's MP4 extension. | Cover extension comes only from its own recognized image path suffix, otherwise unknown. Twenty captured covers change. |
| videodl `50db251dc5c7b043b8feb342`, `9e5cb53d95362646ed898dd6` | HLS candidates were labeled generic HTTPS/candidate-only. | Twelve captured manifest candidates expose streaming transport/state, without enabling stream download or inventing tracks. |
| Kukutool `1d723e3f60f5f7b7634cd84a` | Root URL alias duplicated an existing typed candidate as unknown. | Nine archives lose the duplicate root alias. The representative retains seven quality variants, primary video and image (10 -> 9), including variants that share URLs. |
| TianTian `40b6b3302653089b0ed07351`, `e162b21c7cd6812ff941e9cc` | Existing formats and ordered images were already retained. | Regression only: seven quality labels plus image; nine ordered images. No invented resolution from labels. Twelve media returns and 996 business rejections remain distinct. |
| Home / Batch context | Existing candidate-count template received no returned/retained counters. | Shared context and homepage application pass actual counts; both templates show the same candidates as Lab. |

These are structural compatibility benefits, not newly deployed sites or proof of
best quality. URL suffixes and upstream quality labels are metadata evidence, not
verified MIME/codec or downloaded-byte evidence. Missing values remain unknown.
The yt-dlp type correction also preserves 16 existing audio-format candidates and
one explicitly image-typed candidate across 11 other archived returns; those
previously fell through to the unconditional video classification.

## Replay boundaries

`tools/native_result_replay.py` uses production Worker and converter functions:

- yt-dlp: restore the captured `process_video_result` input at an extractor
  return boundary, then run the actual bounded `parse_ytdlp` metadata processing.
- gallery-dl: restore already captured messages (not lazy iterators), then the
  actual `parse_gallery` compactor.
- ParseHub: explicit allowlist of known result/reference classes and stored
  fields, no archive-selected imports, constructors, properties or evaluation;
  run `normalize_result` inside the existing pinned runtime.
- videodl: stored `VideoInfo` fields only, then the actual Worker normalizer and
  Worker-to-product mapping. No Backend constructor or remote invocation.
- TianTian: actual business validation, unwrap and existing result normalizer.
- Kukutool: capture already follows helper business/protocol unwrap; replay
  starts there and does not claim to revalidate authentication/decryption.

All then execute `quality_execution.execute_provider`, shared candidate creation
and `parse_result_to_lab`. Only the external execution boundary is replaced.
URL syntax/scheme/credential rejection still runs. Current DNS/download admission
is explicitly unverified, not mocked into an authorized downloadable asset.

Replay uses isolated in-memory DB/cache and a synthetic key. HTTP, DNS, socket
connect and subprocess attempts fail loudly in the replay process. A dedicated
test proves this before replay. ParseHub's separate process installs the same
guard before reconstruction; no daily cache, settings, account or DB is read.
No production resource authorization is issued. Report exports omit tokens/IDs
that could be mistaken for resource capabilities.

Consumer regression passes actual converted results through homepage context,
Batch row construction, Lab serialization and real templates. Resource actions
are disabled in the offline artifact; no historical media loads. Browser opening
of the local file artifact was blocked by tool URL policy; no workaround or
visual-browser PASS is claimed. Template and frontend DOM assertions do pass.

## Results and remaining boundaries

Same-input baseline: 264 converted, 1,084 rejected, one processing exception.
After correction: 267 converted and 1,082 rejected. Rejections include 996
pre-existing business refusals; they are not newly discovered code regressions.
Four rejected captures contain internal partial media despite final execution
failure. The live path did not return that partial data as a valid result, so
replay does not manufacture an online success from it.

Four archives contain non-exportable objects; 130 contain redacted fields.
Their visible conversion data can be exercised, but omitted lazy objects and
secret-dependent behavior cannot be reconstructed. Collection exports are not
advanced. Two live titles acquire processing-time suffixes in yt-dlp; those
textual deltas are not counted as fixes.

The 23 overlapping tags are covered in the private structure-coverage report.
They group into image/body content, audio/video/formats/quality, streams/tracks,
typed/service wrappers, and negative/partial results. Existing bounded retention,
unknown track/merge semantics and explicit relationships remain in regression.
No new real Live Photo pairing evidence was obtained. No automatic pairing is
inferred from neighboring still/video fields.

Current DNS reachability, live Provider integration, resource authorization,
preview bytes and full downloads remain **NOT_TESTED**. Provider requests and
media bytes for this phase are both **0**. Human routes and audit are unchanged.

## Reproduction

Use the existing application venv and pinned optional contract runtimes; no
application requirements change. Tested with Python 3.14.5, yt-dlp 2026.3.17,
gallery-dl 1.32.4 and ParseHub 2.2.0 at pin
`144260f4349c5bb57415f7555a572fcd48c3b8ba`. The videodl converter remains pinned
to `6cd1de0fe1209380b183b654c65e98a629637873`; no runtime reinstall occurred.

Standalone replay does not load the project's `.env`:

```sh
.venv/bin/python tools/native_result_replay.py \
  < xhs_download/fixtures/native_replay/88e9b92a7c517fb33ce435a4.json
```

The 15 reduced real captures and provenance index are under
`xhs_download/fixtures/native_replay/`. URLs are non-routable example-domain
fixtures using reserved domains with original protocol/extension/relationships preserved; prose,
signatures and secret values are removed. Unused native internals are omitted
explicitly. They are not new synthetic successful Provider responses.

```sh
PARSEHUB_COMPAT_PYTHON=/path/to/existing/pinned/runtime/bin/python \
  python manage.py test xhs_download.tests_native_result_replay \
  xhs_download.tests_upstream_parsehub_real_types --noinput
python -m unittest tools.tests_upstream_worker_quality -q
node --test tests_js/provider_lab_quality.test.js
```

Run project tests only with isolated test settings/database/cache and no real
mail/provider credentials. Full private archives, before/after outputs, HTML/CSV
indices and the read-only batch replay driver remain outside Git. The private
driver rejects an existing run label rather than overwriting historical output.
CI uses the small sanitized fixtures, not the original corpus.

## Execution-path review, 2026-10-15

This is a later observation, not a rewrite of the Phase 2A acceptance above.
The reviewed starting Head is `045dffdec2a726cefd39c3b9d210da4115779df6`.
Previously recorded WeChat browser acceptance and Audius live Batch audio success
remain valid within their stated limits. They are not repeated in this review.

### Actual paths

Home/Batch use shared execution after matching, route selection and admission.
Lab executes the selected provider through the same shared conversion, without
saving a route. Matching/version checks alone do not prove extraction works.

| Provider | Home / Batch and Lab | Native census | Offline replay |
| --- | --- | --- | --- |
| yt-dlp | Configured Python resolved from the approved command; `-I -B upstream_match_worker.py`; JSON identity/match/parse; bounded `parse_ytdlp`, metadata processing, optional `probe_ytdlp_result`, JSON, parent error mapping, candidates. | Same configured Python and isolation, but `upstream_census_worker.py` calls `parse_ytdlp(observe=...)` directly. It does not enter product `handle` or its post-extraction probe. | Captured extractor return enters real `parse_ytdlp` processing and shared conversion, not Worker main or post-extraction probe. |
| gallery-dl | Same isolated match Worker/JSON transport, then real message iteration and compaction; no yt-dlp probe. | Census Worker observes messages before the same compactor. | Stored messages replace iteration; real compaction and shared conversion. No identity/launch evidence. |
| ParseHub | Separate pinned Python, `-I -B upstream_parsehub_worker.py`; pin check, registry/match, parser call, body/type normalization, JSON. Lab uses this shared product bridge, not the legacy Lab parser path. | Separate pinned Python/census Worker; native observation precedes normalization. | Explicit allowlisted object reconstruction and normalization; no parser request or runtime handshake. |
| videodl | Controlled Lab Worker via `[python, worker]` (not `-I`), exact pin/license checks, selected allowed Backend, bounded output, Worker normalization and shared mapping. | Explicit Backend in census Worker, native observation; not product admission/resource authorization. | Stored exported fields into real normalization and shared mapping; no Backend construction. |
| TianTian | In-process client, actual configuration, account serial guard, POST/business unwrap, service normalization, shared candidates; Lab still requires service authorization. | Native capture uses a controlled Session with `trust_env=False`, TLS verification and raw-response observation. Product Session/environment handling is not identical. | Business validation/unwrap/normalization from saved data; no authentication, lock or live POST. |
| Kukutool | Approved Node plus absolute helper and source argument; protocol discovery/auth/encryption/parse/decryption; bounded parent process and service normalization. | Same helper with capture/diagnostic output before product normalization. | Already decoded captured data; skips helper discovery, auth and decryption. |

Python product/census subprocesses receive bounded stdin/stdout/stderr and time,
temporary working directory/HOME and an allowlisted environment. Lab repository
Workers additionally use temporary XDG directories. Kukutool has its existing
allowlisted helper environment/profile cache. In-process TianTian is not claimed
to have subprocess isolation. No paths were made artificially identical.

The known sibling-import defect was introduced by `c74663de` (also present as
`1d6e62c8`), reached main in squash `163119cb`, and was repaired by `045dffde`.
Only the yt-dlp product parse path reaching the optional-probe loader crosses
that location. Identity/version, gallery-dl, native census and offline conversion
do not. Missing historical stack/path information is not reconstructed from a
matching error code or version alone.

### Additional counterexamples and minimal corrections

1. An import failure after identity/matching, during extraction or postprocessing,
   was mapped to `unavailable` and then `PROVIDER_NOT_CONFIGURED`. Actual isolated
   yt-dlp and ParseHub child failures now preserve a fixed
   `UPSTREAM_RUNTIME_IMPORT_ERROR` reason under `PROVIDER_EXECUTOR_FAILED`.
   Initial missing-package checks remain `unavailable`. This classifies an error;
   it does not repair an absent dependency or swallow the failure.
2. A TianTian success wrapper containing only `data.audio_url` was not unwrapped:
   the media-presence predicate omitted the audio aliases already handled by its
   normalizer. The predicate now recognizes those four existing aliases.
   Recognized business 451 without media still fails as unsupported.
3. Shared videodl/service candidates could be audio while the outer ParseResult
   claimed image. An all-audio result now has `post_type=audio`; video precedence
   and ordered mixed/image candidates are unchanged. TianTian and Kukutool real
   client/helper-boundary tests exercise this shared consumer path.

The wrapper predicate dates to the existing TianTian client, while the shared
non-video/image default is present in `720399ac` and merged in `e92bcb33`.
These synthetic before/after counterexamples establish current code defects,
not a count of historical real works affected by them. Existing captured service
image/video returns remain in regression. No source/route/authorization is changed.

### Process regression and evidence limits

`xhs_download.tests_execution_path_parity` adds 15 cases to the existing full
Django test entry, without Workflow changes: 13 launch subprocesses, one exercises
the in-process TianTian client, and one checks safe error classification directly.
A test-only bootstrap installs
synthetic native extractors or final HTTP responses, then runs the actual Worker
file as `__main__`. It does not pre-import the Worker/probe, inject the repository
root, remove `-I/-B`, or replace handle/probe/serialization. DNS/connect denial
is independent and scoped to test processes, never the daily 8000 process.

Coverage includes audio, complete video, unknown-track probing, HTTP probe
rejection retaining unknown facts, manifests/separate tracks, malformed optional
metadata, real reduced gallery/ParseHub body fixtures, empty results, business
refusal, local late import failure, timeout, clean temporary CWD and paths with
spaces. An unmodified identity command is also run from two CWDs and explicitly
does not count as extraction acceptance. Timeout/output/resource protections
remain intact; no historical media URL is fetched.

TianTian replaces only Session transport. Kukutool executes its real Node CLI
with a test preload replacing final HTTP transport; the synthetic runtime-status
selection is not a Node-version discovery test. CI has no videodl install, so its
test stubs package identity and native Backend output while executing the actual
Worker/protocol/normalizer; this is not proof of a real Backend implementation.
Separate local check-only observations passed for both existing pinned ParseHub
and videodl runtimes, without a source URL or parse.

Full local validation: 2,551 Django tests, 372 Node tests and 30 independent
Worker tests passed. Django check and migration drift passed (no migration).
New remote parses, Provider attempts, downloads and mail sends: **0**.
This review adds no new visual or live-service acceptance claim.

Reproduce with the existing isolated test environment and pinned runtimes:

```sh
python manage.py test xhs_download.tests_execution_path_parity --noinput
python manage.py test --noinput
python -m unittest tools.tests_upstream_worker_quality -q
node --test tests_js/*.test.js
```

Private historical review is a separate read-only `review_history.py` beside the
existing replay evidence, not a new application or Scout interface. Its output
must be a fresh directory; it verifies source hashes before/after, rejects a
nonempty SQLite WAL/journal rather than ignoring it, and exports no source/media
URLs, titles, credentials or response bodies. The local HTML/CSV are derived
evidence, not replacement historical ledgers.

### Historical failure review

The current review reads all 5,627 census state rows, not only the 1,349 native
archives. It includes 1,958 failed rows, of which 895 lack nonempty native input,
377 state-only entries and two unresolved outcomes. Original status and time are
not rewritten. All 5,357 original evidence-file hashes still match the freeze.
The local history DB has 167 physical entries; 25 explicit imported links collapse
to 142 logical records. Cross-source overlap alone is not used to merge calls.

The failure/timeout/unknown subset contains 1,995 records under these explicit
deduplication rules. Two independent evidence axes must not be conflated:

| Evidence category | Records | Distinct source candidates | Recorded platform groups |
| --- | ---: | ---: | ---: |
| Specific probe-import defect: confirmed | 0 | 0 | 0 |
| Specific probe-import defect: suspected with enough path evidence | 0 | 0 | 0 |
| Specific probe-import defect: excluded by native-census path | 1,960 | 1,007 | 962 |
| Specific probe-import defect: insufficient historical evidence | 35 | 14 | 10 |
| Broader local execution problem: confirmed | 7 | 7 | 7 |
| Broader local execution problem: suspected | 11 | 11 | 9 |
| External error response observed, root cause not established | 1,039 | 997 | 955 |
| Broader cause still insufficient | 938 | 807 | 765 |

The first four rows form the specific-defect axis; the last four form a separate
broader-evidence axis. Source/platform counts can overlap between categories and
are not additive. Two failure-subset rows lack a source identity. Source hashes
represent candidate works, not verified distinct works; platform groups can be
recorded targets/aliases/hosts, not independently validated supported platforms.

The seven confirmed local records comprise six recorded module-resolution
failures (the missing module and ultimate installation/root cause remain unknown)
and the preserved same-archive Audius optional-thumbnail counterexample. None is
silently relabeled as the specific sibling-import incident. Eleven TypeError or
AttributeError records are only suspected local failures. External business/HTTP
errors prove an observed response, not permanent lack of platform support.
The two original outcome-unknown calls remain unknown and are not resent.

Theoretical exposure is the product yt-dlp post-extraction probe path during the
identified commit interval, including Home/Batch and Lab consumers. It is not a
historically confirmed affected-work count. The previous live Audius incident is
documented separately from this census/history-ledger denominator.
