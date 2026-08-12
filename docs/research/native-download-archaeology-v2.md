# Native Download Archaeology v2

Date: 2026-08-12

Final status:

```text
HISTORICAL_NATIVE_PATH_IDENTIFIED_EXTERNAL_BEHAVIOR_CHANGED
```

This is a research record, not a deployment record. It deliberately omits the
source URL, complete signed CDN URL, Provider credentials, cookies, manifest
tokens, and response bodies.

## Start State And Safety Boundaries

| Item | Value |
| --- | --- |
| Known-good production | `a5e0254e001d56429008178ce39364be071da24a` |
| Browser Blob recovery code | `530d0cc` (contained by the production SHA) |
| Research start | `df28dac1d7020f382add0b15b216a0c9ca043bfe` |
| Research branch | `research/native-download-archaeology-v2` |
| Production checkpoint | `checkpoint/production-browser-blob-recovery-v1` -> `a5e0254` |
| Research checkpoint | `checkpoint/native-archaeology-start-20260812` -> `df28dac` |
| Protected main | `d98b3098bd55031c859c125e44c5d84b1f458f17` |
| Protected PR #32 | Draft/Open, head `d93977ec383538871f1199ff0a825de28b99a988` |

Both checkpoint refs were pushed before experimentation and were not moved.
No main commit, PR #32 state, production code, service, environment, database,
Nginx, or systemd configuration was changed.

## Method

The audit used five evidence classes and kept them separate:

- `CODE_ONLY`: what a historical tree intended.
- `HISTORICAL_TEST_EVIDENCE`: offline tests run from the unmodified historical
  tree.
- `CURRENT_RUNTIME_REPRODUCTION`: the historical primitive executed today.
- `CURRENT_BROWSER_NETWORK_PROOF`: Chrome CDP request/response/download events.
- `PRODUCTION_ACCEPTANCE`: a previously completed real production acceptance.

All refs, merge parents, PR child commits, PR descriptions, historical tests,
and the production reflog were read. Detached worktrees were used for candidate
trees. When an old application tree was not suitable for a current end-to-end
launch, a temporary harness reproduced its exact browser primitive without
adding modern fallback logic.

Only one fresh TianTianJieXi parse was used. The same descriptor was reused for
all CDN, native, legacy-fetch, and Blob comparisons.

## Safe Sample Fingerprint

| Field | Value |
| --- | --- |
| Provider | `tiantianjiexi` |
| CDN hostname | `v11-default.365yg.com` |
| Declared/observed bytes | `110,691,390` (105.56 MiB) |
| Quality | Original |
| URL SHA-256 | `dde29e526b46adc97a1921ac29c17d42bc59e61231d28a34a868becc208b470b` |
| Obtained at | 2026-08-12 09:49 +08:00 |
| Parse duration | 1,783 ms |
| Candidate count | 5 |
| Path fingerprint | SHA-256 `32dae1e494b23cae2c02606af0428805e2bfc562ca1705b3ff5ab01d22ced3d9`, 7 segments, no `.mp4` suffix |
| Query key names | `a`, `br`, `bt`, `btag`, `cd`, `ch`, `cquery`, `cr`, `cs`, `cv`, `dr`, `dy_q`, `er`, `ft`, `l`, `lr`, `mime_type`, `net`, `qs`, `rc` |

No query value is recorded.

## Candidate Results

### Summary Matrix

| Candidate | Date | Historical classification | Reproduction | Current result | Download task before full body | Request host | Blob | Server media bytes | Playback | Final native file | Duration / rate |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `7922c6a` | 2026-07-02 10:59 +08:00 | `BROWSER_FETCH_BLOB` or File System Access | `FAITHFUL_PRIMITIVE_REPRODUCTION`; 19 historical tests passed | Exact legacy fetch options failed with `TypeError` after 226 ms; request carried a Referer and no readable response was acquired | No | CDN request attempted | No, because fetch failed | 0 | No | None | 226 ms to failure |
| `8d453b2` | 2026-07-02 21:41 +08:00 | `NATIVE_CDN_BROWSER` intent | `FAITHFUL_PRIMITIVE_REPRODUCTION`; 19 historical tests passed | `_blank` anchor opened a CDN MP4 playback tab; no download event | No | `v11-default.365yg.com` | No | 0 | Yes, top-level | None | Not applicable |
| `9699a94` | 2026-07-06 17:11 +08:00 | Native first click inside `MIXED_FALLBACK` | `FAITHFUL_PRIMITIVE_REPRODUCTION`; 38 historical tests passed | Hidden iframe received a playable MP4 document; no download event | No | `v11-default.365yg.com` | No | 0 | Yes, iframe document | None | Not applicable |
| `42304a9` | 2026-07-07 00:06 +08:00 | `BROWSER_FETCH_BLOB` primary plus server attachment fallback | Historical tree/test plus faithful exact-fetch comparison; 39 Provider Strategy tests passed | Exact historical fetch options fail today when a page Referer is sent. A no-referrer Blob control succeeds, but is not native | No | CDN | Yes only in corrected control | 0 on Blob path | No on Blob control | No native file; Blob control exact | Blob control 35.086 s, 3.009 MiB/s |
| `485951b` | 2026-08-09 11:45 +08:00 | Single `SERVER_ATTACHMENT_BROWSER_MANAGER`; Batch `BROWSER_FETCH_BLOB` | `HISTORICAL_TEST_EVIDENCE`; 118 Provider Strategy tests passed | Rejected as native by construction; Single requires AnimeImages media bytes | Not a native candidate | AnimeImages for Single | Batch only | Positive for Single | No | Not tested in this task | Not applicable |

The tests above ran without changing the historical trees and without network
access. They prove routing contracts, not historical production acceptance.

### Candidate 1: `7922c6a`

The commit name “Download direct routes in browser” does not describe a native
download-manager data plane. The implementation immediately calls `fetch()`,
then either streams into File System Access or builds a complete Blob/object
URL. Today, its exact fetch options send the page Referer and fail before a
readable response. The candidate is useful historical context but cannot pass
the native gate by design.

### Candidate 2: `8d453b2`

This is the strongest pre-TianTian native-intent candidate. A selected single
video bypasses the fetch/Blob code and immediately clicks a cross-origin anchor
with `download` and `_blank`. Current Chrome opened a `365yg.com` playback tab.
There was no `Page.downloadWillBegin`, no `.crdownload`, and no final file.

The exact commit is an ancestor of main, but production reflog shows only its
branch ref being fetched. Production checked out `3c18fb4`, whose tree equals
the later server-attachment tree. The exact native tree's production use is
therefore `DEPLOYMENT_HISTORY_UNKNOWN`.

### Candidate 3: `9699a94`

This is the strongest TianTian native-intent candidate. It expands the
browser-first Provider set to TianTian, changes the runtime default to
`browser_first`, and uses the hidden no-referrer iframe primitive inherited
from `1cdc2c6`. A repeated click could use server single attachment, but the
first click itself was intended to remain CDN-to-browser.

Current CDP evidence for the first click:

```text
request type       Document
response status    200
response MIME      video/mp4
Content-Length     110,691,390
Content-Disposition absent
redirects observed 0
Origin present     no
Referer present    no
child target       iframe
download events    0
```

The MP4 became an iframe document. It did not become a download task.

This commit lived for 14 minutes before `8a4dffa` added an automatic 800 ms
server fallback, then another 19 minutes before `a7adce5` made Blob primary.
PR #10 was squash-merged only after both changes. The PR says its final branch
was deployed and “browser download” was verified, but that wording and the
final tree do not prove a native CDN data plane.

### Candidate 4: `42304a9`

The final PR #10 tree uses these effective predicates:

1. `single_video_prefers_browser_direct()` admits manifest-valid TianTian and
   KuKuTool assets under `browser_first`.
2. `directRouteNeedsServerFallback()` recognizes those Provider items.
3. `directRouteCanUseProviderBlobDownload()` runs first and admits an HTTP(S)
   item up to 1 GiB.
4. Browser fetch/Blob is attempted.
5. Failure submits server single attachment.
6. Raw native handoff is reached only when Blob admission does not apply.

Provider Strategy metadata independently says
`download_strategy=browser_blob` and `fallback_strategy=server_attachment`.
This is why final TianTian integration did not create a visibly new downloader:
it mainly changed the source and quality of the parsed asset.

### Candidate 5: `485951b`

PR #23 is a useful false-positive control. Single video is prepared and served
as a same-origin attachment, so Chrome can create a normal task and show real
progress. The request host is AnimeImages and the server carries the media
body. Batch still uses browser fetch/Blob. This proves why “the browser download
manager appeared” cannot distinguish native CDN delivery from relay.

## Direct CDN And Blob Controls

The controls used the same descriptor, browser, machine, and session.

### 32 MiB CDN Range

```text
HTTP status         206
response latency    2,999 ms
first chunk latency 3,156 ms
duration            20,876 ms
bytes               33,554,432
Content-Range       bytes 0-33554431/110691390
Content-Type        video/mp4
average             1.533 MiB/s
AnimeImages media   0 requests / 0 bytes
```

### Complete Browser Blob Baseline

```text
HTTP status                 200
response latency            963 ms
complete fetch/Blob time    35,086 ms
received bytes              110,691,390
saved file bytes            110,691,390
average                     3.009 MiB/s
request host                v11-default.365yg.com
Origin                      absent
Referer                     absent
Content-Type                video/mp4
Content-Disposition         absent
Page.downloadWillBegin      after complete Blob acquisition
playback                    no
AnimeImages media           0 requests / 0 bytes
```

The exact saved-byte equality proves the baseline. It also proves that Blob is
not native: the browser download event occurs only after JavaScript has fetched
the complete media body. The speed difference between the Range and complete
fetch within one session also warns against assigning one fluctuating CDN rate
to a transport implementation.

## External Difference Analysis

### Historical URL Semantics

Historical KuKuTool and TianTian code both accepted `365yg.com` and
`douyinvod.com`. Fixtures use signed `video/tos/...` resources, and normalizers
read `original_video_url`, `play_url`, `download_url`, or
`video_fullinfo.url`. No retained evidence shows a special attachment endpoint,
`download=1`, filename parameter, MIME override, octet-stream endpoint, or
same-origin redirector.

Historical response headers, complete redirects, and real browser Network
captures were not retained. Consequently, historical Content-Type,
Content-Disposition, and redirect behavior are unknown.

### Current URL Semantics

The current fresh asset is in the same broad `365yg.com` host family and uses a
signed media path and query contract. It currently responds directly with:

```text
status              200 (full) / 206 (Range)
Content-Type        video/mp4
Content-Disposition absent
Content-Length      110,691,390 (full)
redirects observed  0
```

The HTML Standard has long required cross-origin download links to be paired
with `Content-Disposition: attachment` for an unambiguous download operation.
Therefore a generic “Chrome recently changed” explanation is not proved. The
current failure mechanism is proved: a cross-origin navigation to playable MP4
without attachment semantics becomes media playback.

What remains unknown is which historical external condition, if any, differed:

- a prior CDN response could have included attachment semantics;
- a prior Provider URL or redirect could have reached a download endpoint;
- an old browser/environment could have handled the request differently;
- the remembered browser-manager period could have been PR #7/PR #23 server
  attachment rather than true CDN-native;
- the remembered high-speed period could have combined a direct browser data
  plane with site progress or a later Blob save.

None of those possibilities is promoted to fact without retained Network
evidence.

## Best Current Native Assessment

| Question | Result |
| --- | --- |
| Candidate primitive | Hidden no-referrer iframe (`9699a94`), with `_blank` anchor (`8d453b2`) as comparison |
| Production-safe today | No |
| Capability deterministic | Yes in the negative for the tested contract: current response opens playable media, not a task |
| Double-download risk | High if a timed Blob/server fallback is added after an unobservable navigation |
| Shared Single/Batch support | Not implementable safely from this primitive without guessing handoff success |

No candidate met the native hard gate. Native-first product code was therefore
not implemented, and the known-good Blob path was not removed or weakened.

## Implementation And Verification

Product-code changes:

```text
none
```

Research documentation changed:

- `docs/research/historical-download-transport-timeline.md`
- `docs/research/native-download-archaeology-v2.md`

No migration was added. No dependency, setting, runtime gate, Provider order,
timeout, manifest, download route, or UI behavior changed.

Historical offline verification:

```text
7922c6a XhsDownloadArchiveTests        19 passed
a60105f XhsDownloadArchiveTests        19 passed
8d453b2 XhsDownloadArchiveTests        19 passed
9699a94 TianTian + archive tests       38 passed
42304a9 Provider Strategy tests        39 passed
485951b Provider Strategy tests       118 passed
```

The candidate selection is five commits; `a60105f` was additionally tested as
an adjacent boundary because it is often misclassified as native-primary.

## Local Acceptance

| Gate | Result |
| --- | --- |
| Real Chrome macOS | Used |
| TianTian sample >= 100 MiB | Pass, `110,691,390` bytes |
| Single native download | Fail: anchor opens top-level playback; iframe opens playback document |
| Batch native download | Not attempted: shared primitive already failed the hard gate |
| Download task created | No for native candidates |
| Task created before full media | No |
| Request hostname | `v11-default.365yg.com` |
| AnimeImages ordinary media | 0 requests / 0 bytes in candidate and controls |
| Native Blob count | 0 |
| Control Blob count | 1 complete control |
| Playback outcomes | 2 document outcomes: one top-level, one iframe |
| Native final file | None |
| Blob-control final file | Present during proof, exact `110,691,390` bytes, then removed during cleanup |

## Production Decision

No candidate passed Local Acceptance, so Linux candidate and production canary
stages were correctly skipped.

| Field | Result |
| --- | --- |
| Deploy attempted | No |
| Previous/final production SHA | `a5e0254e001d56429008178ce39364be071da24a` |
| Candidate SHA | None |
| Production Single/Batch | Not rerun for an unqualified candidate |
| Production ordinary server bytes | Unchanged from accepted Blob recovery: 0 |
| Production transport | Browser Blob recovery |
| Rollback | Not needed |
| Production health | Must remain readiness/home HTTP 200 at closeout |

## Final Answers

### A. Did the remembered high-speed native download exist?

A genuine native-intent implementation existed in Git. The retained evidence
does not independently prove that an exact historical production tree created
a CDN-owned Chrome download task. The user's first-hand recollection remains an
important product fact, but it cannot be upgraded to
`PRODUCTION_ACCEPTANCE` without a historical Network trace or equivalent log.

### B. Most likely commit and period

- Pre-TianTian: `8d453b2` at 2026-07-02 21:41 +08:00, followed by the
  direct-plus-manual-compatibility behavior at `1cdc2c6`.
- TianTian: `9699a94` from 2026-07-06 17:11 until `8a4dffa` at 17:25.

The exact TianTian window is the last native code candidate before PR #23.
Exact production deployment remains unknown.

### C. What transport did TianTian inherit?

Its first Provider commits (`0165561` through `0c353bf`) inherited server
attachment. A short PR child then tried native iframe handoff, followed by an
800 ms server fallback, followed by Blob primary. The merged PR #10 tree was
Browser Blob primary plus server attachment fallback. Parsing source and
quality changed; the visible product interaction did not need to feel like a
new downloader.

### D. Why does today's raw URL play instead of download?

Today the response is a directly playable `video/mp4`, has no
`Content-Disposition: attachment`, and has no redirect to a download endpoint.
Current Chrome therefore treats anchor/iframe handoff as navigation/playback.
That current cause is proved. Historical headers, URL endpoint semantics, and
browser Network evidence are absent, so the historical difference cannot be
assigned uniquely to CDN, URL, browser, or request context. Browser change
alone is not supported by the long-standing cross-origin download rule.

### E. Can CDN-to-browser Download Manager be restored reliably today?

No, not with the identified historical primitives and current TianTian CDN
contract. A timed automatic fallback would risk duplicate transfers because
page JavaScript cannot reliably observe whether navigation became a download.

## Recommendation

Production should continue to prefer the verified Browser Blob recovery at
`a5e0254`. Do not restore anchor/iframe native launch and do not reintroduce
ordinary-video server relay.

The next independent task should be `BROWSER_BLOB_LARGE_FILE_LIMITATION`: test
100/300/500 MiB memory and reliability, then evaluate a browser-side streaming
architecture with zero AnimeImages media bytes. PR #32 may remain Draft as
research material. A Provider-supported attachment URL/header contract would
be the only simple reason to reopen native download-manager recovery.

## Closeout Status

```text
historical native primitive     identified
historical production success  unproved
current native hard gate        failed
product implementation         unchanged
production deployment          not attempted
known-good production          preserved at a5e0254
final status                    HISTORICAL_NATIVE_PATH_IDENTIFIED_EXTERNAL_BEHAVIOR_CHANGED
```

## Required Report Index

This index maps the task's required 74 report fields to the evidence above.

1. Production SHA: `a5e0254e001d56429008178ce39364be071da24a`.
2. Blob Recovery SHA: `530d0cc` (contained by the production SHA).
3. Research start SHA: `df28dac1d7020f382add0b15b216a0c9ca043bfe`.
4. Research branch: `research/native-download-archaeology-v2`.
5. Checkpoints: `checkpoint/production-browser-blob-recovery-v1` at `a5e0254` and `checkpoint/native-archaeology-start-20260812` at `df28dac`.
6. Protected `origin/main`: `d98b3098bd55031c859c125e44c5d84b1f458f17`.
7. Protected PR #32: Open/Draft at `d93977ec383538871f1199ff0a825de28b99a988`; unchanged.
8. Native CDN-direct intent periods: `8d453b2` before `1cdc2c6`, and `9699a94` before `8a4dffa`.
9. Browser Blob periods: `7922c6a`; Blob-primary portions from `926c056` through PR #10 and later Batch; current `530d0cc`/`a5e0254`.
10. Server Attachment periods: `dae4d03`, `26bc31f`, `3c18fb4`, PR #7, and PR #23 Single.
11. Mixed fallback periods: `1cdc2c6`, `8a4dffa`, `a7adce5`, final PR #10, the PR #10-to-PR #23 interval, and PR #29/PR #31.
12. TianTian integration point: PR #10 child history ending at squash `42304a9` on 2026-07-07.
13. TianTian path at final integration: Browser fetch/Blob primary, server attachment fallback.
14. Last candidate native period: `9699a94` at 2026-07-06 17:11 +08:00 until `8a4dffa` at 17:25; exact deployment is unknown.
15. Candidate SHAs: `7922c6a`, `8d453b2`, `9699a94`, `42304a9`, and `485951b`; `a60105f` was an adjacent boundary control.
16. Candidate dates: 2026-07-02, 2026-07-06, 2026-07-07, and 2026-08-09 as listed in the matrix.
17. Historical classifications: Blob/FS, native intent, native-first mixed fallback, Blob-plus-relay, and server attachment respectively.
18. Reproduction type: faithful primitive reproduction for native/legacy browser paths; unmodified historical tests for every selected tree.
19. Current runtime result: legacy fetch failed; anchor and iframe became playback documents; no native candidate passed; corrected Blob control passed.
20. Download Manager created: no for either native candidate.
21. Task created before media completion: no.
22. Request hostname: `v11-default.365yg.com` for current CDN reproductions; PR #23 Single is AnimeImages-hosted by construction.
23. Blob: zero for native candidates; one complete Blob in the control.
24. Server media bytes: zero for direct candidate reproductions and Blob control; positive by design for PR #23 Single.
25. Playback: one top-level anchor outcome and one iframe document outcome.
26. Final bytes: no native file; Blob control saved exactly `110,691,390` bytes.
27. Duration: legacy fetch failed at 226 ms; 32 MiB Range took 20.876 s; full Blob control took 35.086 s.
28. Throughput: 1.533 MiB/s for the Range control and 3.009 MiB/s for the full Blob control; no native throughput exists.
29. Historical URL semantics: signed `365yg.com`/`douyinvod.com` media paths; no retained attachment endpoint or parameter evidence.
30. Current URL semantics: signed `365yg.com` media path, direct 200/206 response, no attachment redirect.
31. Content-Type difference: historical headers unknown; current response is `video/mp4`.
32. Content-Disposition difference: historical value unknown; current response has none.
33. Redirect difference: historical chain unknown; current observed redirect count is zero.
34. Chrome behavior difference: current anchor/iframe primitives produce playable documents; retained evidence does not prove the historical runtime outcome.
35. Proven cause: today's cross-origin playable MP4 response lacks attachment semantics, so navigation is not admitted as a download operation.
36. Unknown factors: historical CDN headers, endpoint/redirect behavior, exact browser runtime, and exact production deployment of child commits.
37. Best candidate primitive: hidden no-referrer iframe from `9699a94`, compared with the `_blank` anchor from `8d453b2`.
38. Production-safe: no.
39. Capability deterministic: yes in the negative for the current tested CDN contract.
40. Double-download risk: high if an unobservable native navigation is followed by a timed fallback.
41. Shared Single/Batch support: no safe implementation from the failed primitive.
42. Files changed: these two research documents only.
43. Product implementation commits: none; the documentation commit is the head of this research branch.
44. Tests: historical totals of 19, 19, 19, 38, 39, and 118 plus repository static Django gates at closeout.
45. Migration: none.
46. Pushed: the documentation commit and both immutable checkpoint refs are pushed; no main push occurs.
47. TianTian bytes: `110,691,390` (105.56 MiB).
48. Local Single: native hard gate failed because both primitives produced playback instead of a task.
49. Local Batch: skipped after the shared primitive failed the hard gate.
50. Download Manager timing: no native download event; Blob-control event occurred only after complete media acquisition.
51. CDN hostname: `v11-default.365yg.com`.
52. AnimeImages media bytes: zero in all direct candidate/control media transfers.
53. Blob count: zero in native attempts; one in the explicit control.
54. Playback count: two native candidate document outcomes.
55. Final file: none for native; Blob control matched the expected byte count and was removed during cleanup.
56. Production deploy attempted: no.
57. Previous production SHA: `a5e0254e001d56429008178ce39364be071da24a`.
58. Candidate SHA: none, because no product candidate qualified.
59. Production Single: not rerun for an unqualified candidate; accepted Blob recovery remains unchanged.
60. Production Batch: not rerun for an unqualified candidate; accepted Blob recovery remains unchanged.
61. Production ordinary server bytes: unchanged at zero on the accepted Blob path.
62. Production Blob: retained.
63. Production playback: none in the accepted recovery evidence; no new production attempt occurred.
64. Performance: accepted production evidence remains approximately 51 s Single and 16 s Batch; current local Blob control was 35.086 s.
65. Rollback: not needed because no deployment occurred.
66. Final production SHA: `a5e0254e001d56429008178ce39364be071da24a`.
67. Production health: readiness and home remain HTTP 200 at closeout.
68. Did a historical true CDN-native download exist? Native-intent code certainly existed; exact production CDN-native success is not independently proved.
69. Most likely period: `8d453b2`/`1cdc2c6` before TianTian and `9699a94` for the last TianTian native child window.
70. TianTian compatibility: an early child briefly used the inherited native primitive, but merged PR #10 was Blob-primary plus server fallback, preserving familiar UX.
71. Why current raw URL differs: it is an inline playable MP4 with no attachment header or redirect; historical external response/runtime details were not retained.
72. Can native be restored today? Not reliably with the identified primitives and current contract.
73. Production preference: keep the verified Browser Blob recovery.
74. Next task: `BROWSER_BLOB_LARGE_FILE_LIMITATION`, followed by zero-server browser-side streaming research if needed.
