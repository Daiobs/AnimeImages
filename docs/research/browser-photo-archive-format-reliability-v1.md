# Browser Photo Archive And Format Reliability v1

Date: 2026-08-17

## Status

```text
PRODUCTION_ACCEPTED
Production SHA: e70848c8b89225beba0838e9b681aaf5b7fdf789
Production checkpoint: checkpoint/production-browser-photo-archive-v1
Rollback checkpoint: checkpoint/production-download-ui-batch-actions-v1
Migration: none
```

Production remained frozen throughout candidate development. The candidate was
developed from canonical `origin/main` at
`2d1a11a38787d787caa72ad3651756919ebf21e3`; no production host, runtime
setting, infrastructure, database, or cache was accessed or changed.

The merged candidate at `e70848c8...` was later exact-deployed and passed the
production-origin Browser Acceptance described below. No redeploy, restart,
collectstatic, Nginx reload, database change, runtime-setting change, or
environment change was performed during the resumed Browser Acceptance.

## Production Browser Acceptance

The final Browser gate used isolated Playwright `1.62.1` with Chromium
`149.0.7827.55` against the real `https://download.cosflow.icu/` origin. The
Codex `@chrome` remote-HTTPS bridge remained unstable and the Codex in-app
browser remained automation-blocked. Those were acceptance-tool failures, not
AnimeImages application failures; the independent Playwright harness completed
page control, DOM, network, CDP-assisted throttling, and filesystem artifact
capture.

Sample B (`6a0f05c9000000000803155e`, author `苍蓝ya`) returned five ordered
resources from `ci.xiaohongshu.com`. Production-origin fetches were HTTP 200,
`cors`, non-opaque, body-readable streams with exposed `Content-Length`.
Upstream content types included `application/octet-stream` and `image/jpeg`;
admission and saved extensions were determined from the supported raster file
headers.

The first Gallery image and the third-image Lightbox action each produced a
real `.jpg` filesystem artifact with JPEG magic and a SHA-256 exactly matching
the browser-fetched source bytes. Their download windows contained no
`/media/download-images/`, `/media/download-tasks/`,
`/media/download-prepare/`, `/media/download-stream/`, or
`/media/download-status/` request and no proxy-image request.

The default five-image action created one `3,491,103`-byte ZIP. Independent
Python `zipfile` validation confirmed five unique UTF-8 entries in `_01`
through `_05` order, method 0 (STORE), valid CRCs, correct JPEG extensions and
magic, and exact source-byte SHA matches for all five entries. Its media
requests came from `ci.xiaohongshu.com`; Provider calls, proxy-image calls, and
AnimeImages media endpoint calls were all zero.

With temporary client-only network throttling, 14 non-terminal UI snapshots
showed real transfer growth from `16.1 KiB` through `2.80 MiB`, nonzero rolling
speeds up to approximately `1 MiB/s`, item progress from `0 / 5` through
`4 / 5`, known-total percentages from 54 through 99, and bounded ETA text from
two seconds to one second. This was progress over actual CDN bytes, not a
synthetic timer.

The production Gallery rendered all five items on desktop and a 390px mobile
viewport with contained intrinsic ratios. Card previews used the 960 variant;
the 1600 Lightbox variant loaded only after opening. Index `3 / 5`, Next, Prev,
Escape, and current-image download passed without losing Gallery state. The
anonymous public surface exposed Gallery, per-image download, download-all,
and copy-all for all five resources while omitting `links.txt`, `aria2.txt`,
raw-link actions, and strategy diagnostics.

Production Single parsed the same five-image work, selected two images, and
created one valid two-entry STORE ZIP with zero AnimeImages media endpoint
requests. Staff permissions, explicit server fallback, dynamic replacement,
server format reliability, and ordinary-video zero-relay behavior were reused
from focused controlled gates; they are not mislabeled as new Production
browser executions. The ordinary-video Service Worker/static identity had
already passed the candidate deployment integrity gate.

## Rollback Finding

The first Batch Gallery production acceptance validated the Gallery and
Lightbox experience, but it did not validate the final photo-download
contract. It exposed three separate defects:

- suffixless `application/octet-stream` images could be mislabeled `.jpg` in
  a server ZIP;
- a cross-origin anchor's `download` filename was not authoritative in Chrome,
  so a single image could reach disk without an extension;
- pure multi-image downloads made AnimeImages fetch and package every image,
  so the page could not report the user's actual CDN transfer rate.

That acceptance was rolled back. Gallery UX was validated during the rolled
back window; the download contract was blocked and is replaced by this
candidate.

## Real Sample Qualification

Three distinct XHS notes were qualified using task-scoped source links. The
links and access tokens are not retained in the repository or this report.

| Sample | Note ID | Result | Images | Title clue |
| --- | --- | --- | ---: | --- |
| A | `6a0d9ad20000000006037a46` | qualified | 4 | `Darling darling, I love you my baby` |
| B | `6a0f05c9000000000803155e` | qualified | 5 | `像你这么有趣的人，我还是第一次遇见` |
| C | `69fc7373000000001a0367a1` | qualified | 2 | `于是拍出了游戏封面的样子` |

The titles, counts, and resource fingerprints were distinct. The previously
suspected same-content mapping was not reproduced, so no XHS source-identity
behavior was changed.

## Browser CORS Capability

```text
XHS_BROWSER_FETCH_CORS: PASS
tested media host(s): ci.xiaohongshu.com
browser origin: http://127.0.0.1:8000
response readable: YES
Content-Length exposed: YES
response type: cors
```

Chrome read all 11 qualified download candidates with `GET`, omitted
credentials, disabled cache, and no referrer. Every response was HTTP 200 with
a readable body. Upstream declarations included `image/jpeg` and
`application/octet-stream`; every real candidate in this sample set had JPEG
magic. A controlled extensionless octet-stream PNG therefore supplies the PNG
filesystem-name proof rather than being presented as a real XHS PNG sample.

## Format Contract

Python and JavaScript use the same ordered resolution contract:

1. bounded magic bytes;
2. a specific trusted image MIME type;
3. a safe path suffix (`jpg`, `jpeg`, `png`, `gif`, `webp`, or `avif`);
4. `IMAGE_FORMAT_UNDETERMINED`.

JPEG, PNG, GIF87a/GIF89a, WebP, and AVIF/AVIS signatures are supported.
Generic binary MIME types are not image evidence. HTML, JSON, XML, and SVG
prefixes are rejected. Magic wins over conflicting metadata and records a
URL-free `image_type_metadata_mismatch` warning. Unknown bytes never fall back
to `.jpg`.

Server original-image archives inspect no more than 4 KiB from each bounded
temporary file. Sync archives, async archives, original image saves, and video
cover saves share the resolver. A failed item is listed by safe index and error
class in `failed.txt`; an archive with no successful media is rejected.

## Browser Data Paths

```text
SINGLE_IMAGE_PATH:
Image CDN -> Browser fetch -> Blob -> Browser Download Manager -> Disk

PURE_IMAGE_MULTI_PATH:
Image CDN -> Browser fetch -> STORE ZIP -> Browser Download Manager -> Disk

SERVER_FALLBACK_PATH:
Image CDN -> AnimeImages ZIP generator -> Browser -> Disk
  (only after an explicit "使用服务器打包" action)

ORDINARY_VIDEO_PATH:
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk
```

The shared Browser Photo module is used by Single, Batch Download All, Batch
per-image actions, and the Batch Lightbox current-image action. It never calls
AnimeImages media endpoints, does not reparse, and does not persist URLs or
results in browser storage. Pure original-image selections use this path only
when no item requires cookies or server-side merging. Mixed media, conversion,
merge, and other generated artifacts retain their existing server-required
path.

Real Chrome acceptance saved Sample A image 1 as a `.jpg` with exact source
and filesystem byte hashes. A controlled extensionless octet-stream PNG saved
as `.png` with exact bytes. Sample B produced one five-entry browser ZIP in
source order; each entry had JPEG magic, the expected `.jpg` suffix, valid CRC,
and a source-matching hash. These successful paths made five CDN requests and
zero AnimeImages media requests.

## ZIP Implementation And Memory

```text
ZIP_IMPLEMENTATION: STORE_ONLY_INTERNAL
compression method: 0 (STORE)
fetch concurrency: 3
per-item media cap: 50 MiB
aggregate media cap: 128 MiB
Zip64: not required
```

The local writer emits UTF-8 ZIP32 local headers, central-directory records,
EOCD, and incremental CRC32 values. Already-compressed image payloads are not
deflated. Image chunks are retained as Blob parts and are not copied into a
second archive-sized `Uint8Array`. Browser Blob internals remain
implementation-defined, so the 128 MiB aggregate boundary is deliberately
conservative.

Known aggregate `Content-Length` values are checked before bodies are read.
Unknown totals are checked while streaming. A unified abort controller stops
active readers and prevents pending work from starting. Each fetch and read
also has a 30-second idle boundary. Object URLs are revoked after a delayed
handoff, not synchronously.

## Progress And Failure Semantics

The shared transfer meter reports item count and received bytes for every
archive. It uses a rolling 2.5-second byte window for speed. Percent is shown
only when every outstanding resource has a reliable total; ETA additionally
requires at least 1.5 seconds of measurement and a positive measured rate.
Unknown totals remain indeterminate rather than showing invented percentages.

The stages are connecting, fetching, assembling, saving, and completed.
In-page browser assembly is page-owned, so the UI warns that refresh or close
interrupts it and provides Cancel. Cancellation does not trigger a server
request.

A CORS or unreadable-body failure before any browser media byte stops the
browser archive and reveals an explicit `使用服务器打包` action. Post-start
fatal failures use the same explicit recovery choice. No server fallback is
automatic. A controlled CORS failure generated zero server requests before
the click and exactly one intercepted server request after the explicit click.

For the explicit server path, an artifact no larger than 128 MiB can be read as
a bounded browser Blob with real server-to-browser byte, speed, and ETA
progress. A known larger artifact is handed to the Download Manager directly
and the page states that it no longer measures transfer speed.

## Gallery And Permissions

The PR #44 Gallery contract remains intact: all parsed images render in source
order, card previews use the 960px candidate, the row-scoped Lightbox uses the
1600px candidate on demand, the first image is eager/high priority, remaining
images are lazy, and all previews keep asynchronous decoding and contained
intrinsic ratios. Controlled 3-, 5-, and 10-image cases and 1920px/390px
layouts remained usable. Retry replacement reads the new card DOM instead of
retaining an old URL array.

Allowed public Batch users retain Gallery, photo download, current-row browser
archive, and copy-all. Raw item actions, `links.txt`, `aria2.txt`, and strategy
details remain staff-only. Unknown image extensions are omitted from staff
aria2 filenames instead of being invented as `.jpg`.

## Unchanged Boundaries

- Ordinary video Service Worker Streaming is unchanged and does not reuse the
  photo Blob implementation.
- Provider order, parsing, retry, Batch deadlines, runtime settings, SSRF and
  Provider-manifest validation are unchanged.
- Server fallback count, byte, archive, hostname, and filename protections are
  retained.
- No endpoint, dependency, model, or migration was added.
- `PHOTO_DOWNLOAD_VARIANT_FIDELITY_BACKLOG` remains open: exact downloaded
  bytes are proven against the current candidate, not against every possible
  XHS original-quality variant.

## Verification Scope

Automated tests use controlled bytes and do not call a real Provider or public
network. Real CORS, filesystem filename, exact-byte, five-image ZIP, Single,
Batch, Lightbox, cancellation, explicit fallback, and desktop/mobile checks
were performed separately in local Chrome. No signed media URL or source token
is retained in test fixtures or this report.

```text
Image format tests:       11 passed
Focused Django regressions: 299 passed
Full xhs_download suite: 1120 passed
Full Node suite:          145 passed
Django check:             passed
Migration dry-run:        no changes detected
JavaScript syntax:        passed
git diff --check:         passed
```
