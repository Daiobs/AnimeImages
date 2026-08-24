# Changelog

## Unreleased

- Completed a fail-closed, URL-free evidence review of two real XHS LivePhoto
  notes plus gallery, video, and invalid controls. Both positive payloads expose
  direct same-item marker/still/motion ownership; the negative controls produce
  no false LivePhoto pairs and the invalid target emits no media. This permits
  a later offline mapper implementation, but adds no Provider wiring, registry,
  host admission, public UI, download capability, migration, or deployment.
- Split ordinary-media URL trust into strict initial admission and
  source-bound final redirect policy. A reviewed strict `*.bdcgslb.com`
  subdomain is now accepted only after an admitted `*.365yg.com` source
  redirects there; direct descriptors, apex/lookalike hosts, unsafe URL forms,
  and unknown final hosts remain rejected. Failure telemetry records only
  sanitized hostname-level facts, and Single/Batch now leave stale transfer
  progress for an explicit retryable terminal state. Provider behavior,
  response media gates, timeouts, and the zero-Relay data path are unchanged.
- Added a dormant, URL-free XHS rich-media normalizer behind exact requested
  and payload note identity. It preserves ordered image/video intermediate
  specs and creates Live Photo relationships only from explicit same-item pair
  and owner evidence that a future AnimeImages adapter must construct and
  prove; unknown extensions and HEIC fail closed. It does not create empty-URL
  `MediaAsset` values. The current XHS adapter, Provider registry, hosts,
  requests, manifests, downloads, and runtime rich-media emission remain
  unchanged.
- Added a frozen, network-free Source URL Lifecycle v1 foundation and an
  offline Provider Lab. The lifecycle separates input, parse, resolver-output,
  and post-parse URLs and uses closed parse-only, identity, safe-passthrough,
  and drop parameter actions while requiring platform-supplied canonical
  identities. URL-bearing internal representations are redacted, and safe
  diagnostics expose only structural fields and parameter names. Existing
  Providers do not consume the contract yet, so routing, output, deadlines,
  cache, downloads, and production behavior remain unchanged.
- Added a closed, typed, URL-free Rich Media Domain Contract v1 runtime model
  as an optional final field on `ParseResult`. Legacy results continue to use
  `rich_media=None`; current Providers emit no rich-media data, and existing
  views, downloads, Provider manifests, serializers, settings, and database
  behavior are unchanged. The Draft-only runtime contract validates source
  tuple order, physical asset identity/type/extension, Live Photo pairing,
  animation formats, and inert rich-text references before any future consumer
  can observe the value. Bilibili Dynamic/LivePhoto implementation has not
  started.
- Added offline Bilibili Dynamic/Opus research primitives: a source-shape
  classifier with separate identities and a dormant URL-free intermediate-spec
  normalizer that preserves source order and accepts Live Photo relationships
  only from explicit pair and owner evidence a future AnimeImages adapter must
  construct and prove. Generic numeric Bilibili paths remain unverified and do
  not receive a guessed identity. Final `MediaAsset` construction, cross-form
  identity mapping, runtime payload evidence, media-host admission,
  registration, and deployment remain blocked; the existing Bilibili video
  adapter is unchanged.
- Hardened AnimeImages-owned external-tool execution with one bounded `Popen`
  lifecycle for progress and non-progress calls. POSIX commands now run in an
  isolated session/process group; timeout, live stdout/stderr overflow,
  callback errors, runner exceptions, and normal parent exit all trigger
  TERM-then-KILL cleanup of remaining same-group descendants. Existing
  `CompletedProcess`, timeout, output-limit, Provider, cache, and temporary
  environment contracts remain unchanged, with a conservative direct-process
  fallback on non-POSIX systems. No ParseHub code or dependency was imported.
- Closed the Production Promotion gate at application SHA
  `766cecfd239f594e791c556b4caa69aebf2d1f9f` and immutable checkpoint
  `checkpoint/production-promotion-ready-v1`. Production code, desktop browser,
  exact-size, zero-Relay, and real-device mobile gates passed. On the tested
  devices Android completed into Gallery, while iOS completed into Files and
  did not automatically enter Photos; that destination difference is accepted
  and non-blocking. AnimeImages is now promotion-ready for real-user
  introduction, with future work driven by Production Feedback, repeated user
  reports, real failure logs, measurable reliability issues, or demonstrated
  interaction friction.
- Restored Douyin video size metadata by keeping the canonical
  `douyin:<aweme_id>` work identity while sending TianTianJieXi the richer,
  aweme-bound modal request representation. The Provider still receives one
  request per fresh attempt, payload cache/single-flight/failure cooldown keys
  remain unchanged, and no source-page or media-size probe was added. Ultra
  timing, Provider order, Browser Streaming, and zero-Relay behavior are
  unchanged. PR #55 was deployed and production-accepted; three of three
  TianTianJieXi samples restored size metadata, and the accepted
  79,910,405-byte browser save exactly matched the parsed size.
- Unified Douyin URL shapes around the resolved `aweme_id`, so short links,
  canonical video links, modal links, share links, and copied share text reuse
  the same TianTianJieXi payload cache, single-flight, and failure cooldown.
  Short-link resolution remains bounded separately, while the unchanged Ultra
  Provider window starts after identity resolution and still respects an
  explicit Batch outer deadline. This change adds no migration, does not alter
  Provider order or attempt accounting, and admits only the reviewed exact
  `v3-dy-a-x.ixigua.com` media host. PR #54 was deployed and
  production-accepted with Browser Streaming and zero ordinary-media server
  bytes preserved.
- Closed Production Polish across PR #47 through #52 and production-accepted
  exact application SHA `a87ec4751c67208031e2f296507b304c7c5d890c` at
  immutable checkpoint `checkpoint/production-polish-v1`. The release includes
  the hardened Admin login shell, public diagnostics boundary, consolidated
  project settings, lightweight feedback workflow, browser identity, XHS
  source-identity protection, zero-media Batch correctness, centered responsive
  Gallery previews, and Douyin title/cover completion. Migration
  `0024_userfeedback` was already applied; PR #52 added no migration. Production
  browser acceptance passed invalid and valid XHS, Gallery/Lightbox, browser
  photo actions, an exact 58,154,674-byte ordinary-video Streaming download
  with no Server Relay, core UI smoke, health, and zero unexpected 5xx.
- Added one shared browser-first photo path for Single, Batch Download All,
  per-image actions, and Lightbox download. It reads CDN bytes in the browser,
  detects JPEG/PNG/GIF/WebP/AVIF from bounded magic, saves single images with
  reliable extensions, and builds ordered STORE-only ZIP32 archives with
  incremental CRC32, real byte/speed progress, conditional ETA, cancellation,
  concurrency 3, a 50 MiB item cap, and a 128 MiB archive cap. Successful pure
  image downloads make zero AnimeImages media requests; server ZIP is an
  explicit compatibility action rather than a silent fallback.
- Production-accepted the Browser Photo Archive at exact SHA
  `e70848c8b89225beba0838e9b681aaf5b7fdf789` with immutable checkpoint
  `checkpoint/production-browser-photo-archive-v1`. Production-origin
  Playwright verified XHS CORS, real filesystem filenames, exact single-photo
  bytes, a valid five-entry STORE ZIP with 5/5 source hashes, real transfer
  progress, Gallery/Lightbox behavior, public boundaries, and zero AnimeImages
  photo-media endpoint requests. The XHS original-variant fidelity backlog
  remains open.
- Fixed server image archives and saves so suffixless generic binary responses
  use bounded magic before MIME or path suffix, metadata conflicts prefer the
  bytes, unknown content never becomes `.jpg`, partial failures use
  `failed.txt`, and sync/async artifact sizes and formats remain consistent.
  The full PR #44 Gallery and Lightbox UX is retained, but its first production
  acceptance was rolled back because the download contract was blocked. The
  replacement was subsequently deployed and accepted, adds no migration, and
  leaves Provider and ordinary-video Service Worker Streaming behavior
  unchanged.
- Removed the redundant global active-download panel from Single and Batch
  product pages while retaining Service Worker state, Browser Download Manager
  cancellation, refresh continuation, and card-local progress. Allowed public
  Batch users can now copy all successful video and image resource links;
  staff-only `links.txt` and `aria2.txt` exports use the same stable, deduplicated
  mixed-media sequence, while per-item raw tools remain staff-only. Batch
  previews now sit beside their media actions without changing the ordinary
  zero-server-media download path. PR #42 was production accepted at exact SHA
  `9ba5702e9a90b9179e21d53393d8e27c39c51833` with immutable checkpoint
  `checkpoint/production-download-ui-batch-actions-v1`.
- Hardened TianTianJieXi success-cache admission with platform-aware structural
  validation, added URL-free Provider outcome and cache-age diagnostics across
  TianTianJieXi/KuKuTool/Native, and split browser invalid-response telemetry
  into fixed status/body/content-type/final-host reasons while preserving old
  telemetry clients, keeping failed download navigation inside an isolated
  same-origin frame, and preserving the zero-server-media Streaming contract.
- Added URL-free, token-free Service Worker failure diagnostics with fixed
  fetch classes/stages, hostname-only metadata, bounded timings, one
  best-effort terminal event per download, and a CSRF-protected, rate-limited
  structured-log endpoint. Ordinary media remains browser-only with zero
  AnimeImages media bytes.
- Added a non-UI bounded Page fetch probe and a production Streaming acceptance
  gate that distinguishes candidate regressions from suspected upstream or
  browser transients while preserving all service, static, controlled-stream,
  integrity, security, and no-Relay rollback gates.
- Reconstructed the production-stable Service Worker Streaming and Douyin
  Ultra retry/continuation behavior on top of main, while retaining the audited
  Batch access and Runtime Admin changes from PR #26/#28. Ordinary Provider
  media remains browser-only with zero AnimeImages media bytes; the historical
  native-open, ordinary Relay, and fixed-at-one TianTianJieXi lease paths are
  superseded.
- Added a production behavior manifest, source-of-truth and production-drift
  rules, main-protection audit, branch cleanup inventory, and immutable
  checkpoint workflow. This reconciliation does not deploy production and adds
  no migration.
- Changed Douyin ultra first-result semantics so TianTianJieXi and KuKuTool race for the first visible result for up to six seconds while Native is prepared only as a hidden fallback. Already-started external work may continue within the bounded 15-second window and automatically replace the fallback; a two-second cache-only result grace prevents an edge success from being missed without extending Provider execution.
- Added request-scoped Ultra attempt accounting: one fresh TianTianJieXi/KuKuTool race consumes one user attempt, while payload/failure caches, single-flight waiters, continuation attach/poll, Native fallback, and automatic upgrades consume none. User-facing Ultra and generic third-party limits default to 10 per minute; transient TianTianJieXi/KuKuTool retry cooldowns default to eight seconds with a local countdown.
- Productized Service Worker streaming as the default ordinary-video transport for Single and Batch: Provider/CDN bytes now stream through the user's browser Download Manager to disk, while AnimeImages remains a zero-media-byte control plane. Blob is restricted to explicit pre-start capability failure, post-start failures never duplicate the transfer, and Server Relay remains limited to genuinely server-generated outputs.
- Added refresh reattachment, URL-free active status/cancel messages, Download Manager cancellation propagation, real shared progress/speed/ETA, opaque download IDs, and a no-`skipWaiting()` update lifecycle. Production Chrome acceptance saved exact 110,691,390-byte TianTianJieXi files in Single and Batch with zero ordinary media requests through AnimeImages.
- Completed controlled stabilization for independent concurrent downloads, three refreshes of one 128 MiB stream, tab-close survival, a waiting-worker update during an active 96 MiB stream, and exact 300/500 MiB streaming without media-sized JavaScript buffering. Safari also completed an exact 16 MiB controlled Streaming smoke; Firefox and Edge were unavailable on the test machine.
- The following Blob progress and page-lifetime entries remain as the historical recovery milestone and current pre-start compatibility fallback; they no longer describe the default ordinary-video transport.
- Restored one shared Single/Batch browser-side download path for ordinary TianTianJieXi and KuKuTool videos: the browser fetches the Provider CDN with credentials and Referer omitted, validates the complete bounded response, aborts after 30 seconds without network progress, then saves it through a Blob object URL without calling AnimeImages prepare, stream, or status endpoints.
- Added one shared real-byte progress contract for ordinary Single and Batch downloads, including smoothed browser receive speed, ETA for reliable HTTP totals, indeterminate unknown-size behavior, and explicit Blob save-handoff states without restoring a Batch-only media reader.
- Added page-lifetime protection for active Blob downloads: a native leave warning, a URL-free session marker, and a safe reload notice explain that an interrupted page-owned transfer must be retried. Browser failures still stop with retry/reparse guidance instead of entering Server Relay.
- Measured the Blob recovery boundary with exact 100, 300, and 500 MiB browser files. Approximately 300 MiB is the recommended validated range; 500 MiB completed with degraded memory behavior, while the existing 1 GiB code limit remains unvalidated.
- Added a bounded, cache-only continuation for single-link Douyin ultra parsing so an already-running Provider can finish without requiring a second user click; signed tokens are client-bound, the Provider window remains 15 seconds, and bounded client polling plus a two-second result grace can retrieve a completion without starting another Provider call.
- Kept the first usable Douyin result visible while that continuation finishes, prevented native fallbacks from being mislabeled as external 4K winners, and skipped out-of-budget display-size probes so a running high-quality Provider can upgrade the same request instead of requiring repeated clicks.
- Stopped a valid TianTianJieXi media result from spending its remaining parse deadline on an extra native metadata request when the Provider already supplied a usable title and cover; cached/direct native metadata and missing-metadata fallback remain supported.
- Preserved a Batch row that has fully completed successfully within the existing bounded response-completion grace, avoiding false timeouts when a cooperative native fallback returns just after the 12-second Provider deadline; incomplete work and results outside the grace remain timeouts.
- Replaced unobservable iframe POST fallback for Provider videos with a local-only JSON prepare step, separate client-bound stream/status tickets, and bounded status polling. Normal server attachment now opens one upstream media connection, keeps that connection through the streaming response, and releases its concurrency slot when the stream ends.
- Made confirmed download startup sticky across later stream failures and treated an exhausted startup-poll window as an unconfirmed handoff rather than a false red failure; genuine failures before the first streamed byte remain errors.
- Kept the single-file progress bar in its connecting state until download status confirms actual stream iteration, then completed the browser handoff at 100%; the browser download shelf remains responsible for real file progress. Added URL-free timing fields for each upstream connection attempt, Provider refresh, and first byte.
- Bounded the complete server-stream startup phase to 50 seconds across the first media connection, one allowed Provider refresh, the recovery connection, and the first non-empty chunk; startup exhaustion now stops recovery with `DOWNLOAD_STREAM_START_TIMEOUT`, closes the upstream, and releases the concurrency slot without limiting transfer after streaming begins.
- Moved `stream_started` to the first actual stream iteration and reduced private handoff cache records to typed, length-bounded download fields. A manifest-validated minimal media URL remains in the server-only 60-second cache because the initial media GET cannot be recovered reliably from Provider identity alone.
- Added one controlled TianTianJieXi resource refresh and one download retry for recoverable signed-resource failures, capping recovery at two upstream media GETs while preserving SSL verification, media allowlists, Provider order, and existing timeout ceilings.
- Added bounded, non-executing Xiaohongshu page-state extraction for current-note title, author, note ID, ordered galleries, video covers, and explicit availability states, with deterministic meta/DOM fallbacks and no login-cookie or third-party Provider path.
- Defined Xiaohongshu video size as exact-known-or-unknown from validated structured byte fields and stopped missing-size display from adding media probes; single-link and Batch cards now share the same normalized XHS title, author, cover, and size semantics.
- Separated unrelated Xiaohongshu JSON payloads from note candidates, centralized fallback authority, suppressed broad recommendation scans when only another note is present, and made explicit terminal states precede generic media fallbacks unless verified public media belongs to the matched current note.
- Shared the Douyin original/ultra and YouTube highest-quality merge preferences between single-link and Batch pages through a versioned, strictly validated browser storage schema, with deterministic migration from both legacy settings stores.
- Split Batch link drafts into a links-only v2 store and moved runtime budgets from the editable settings modal into a read-only, collapsed `批量处理限制` section without changing Provider order or timeouts.
- Reworked Batch Parse into a bounded scheduler that separates the batch scheduling window from per-item execution deadlines, distinguishes timeout from skipped work, and reports wall-clock duration separately from row-duration totals.
- Enforced the displayed Batch response boundary in the scheduler: cooperative workers still finish before return, while an uncooperative worker is reported as timed out, logged without source URLs, and cannot hold the HTTP request in an unbounded wait.
- Added progressive Batch execution: one CSRF-protected start request reserves the existing whole-batch rate limit, then short-lived client-bound item tokens drive server-ordered, bounded requests and one visible result card at a time without persisting parse results.
- Serialized interactive Batch parsing and Batch-only TianTianJieXi fallback scheduling so multi-link ultra-HD parsing follows the upstream one-at-a-time behavior instead of tripping its concurrency limiter; the single-link Provider Strategy remains unchanged.
- Added CSRF-protected, single-card Batch retry that preserves the original input and all other results, plus moved Batch-only browser behavior from `static/js/app.js` to `static/js/batch_parse.js`.
- Removed optional missing-size media probes from the Batch display path so completed parses are not delayed by extra CDN requests; single-link display behavior is unchanged.
- Distinguished exact, estimated, and unknown resource sizes across Batch and single-result cards; yt-dlp approximate and audio/video merge totals are now labeled as estimates without changing download selection.
- Prioritized only the first preview image in each newly displayed Batch card while keeping the remaining previews lazy-loaded.
- Added Release Management Executor Wiring v1 with an exact `sudo -n` submit-helper allowlist, root-owned helper/systemd/sudoers templates, static validation, failure sanitization, and production runbooks; production remains record-only until a separate approved installation.
- Synchronized README, handoff, current-version, changelog, deployment, and PR-template guidance after the Release Management rollout.
- Added a dedicated Release Management architecture and operations runbook.

## 2026-07-14 Production Releases

- **PR #13 - Batch Parse service refactor:** extracted runtime, result normalization, and view-context logic from `views.py` into `xhs_download/services/batch_parse/` while preserving existing behavior.
- **PR #14 - Production deploy hardening:** replaced automatic recursive ownership repair with explicit validation, hardened readiness checks, and prepared documentation for a future systemd-managed Unix-socket topology; production currently remains on local TCP.
- **PR #15 - Provider stability fixes:** improved batch Douyin provider budgeting and fallback behavior, strengthened Douyin metadata handling, and improved Bilibili parsing reliability.
- **PR #16 - Production follow-up labels:** documented the observed `mobile_feed` fallback and mapped that internal provider value to the user-facing `本地解析` label without changing provider order.
- **PR #17 - Admin Release Management v1:** added audited, exact-SHA `main` release jobs, a non-blocking external executor boundary, single-active-job protection, deployment verification, and locally vendored Bootstrap Icons.
- **PR #18 - Read-only Git state production fix:** removed Web-triggered remote refresh, restricted Web Git access to local read-only refs, added actionable dubious-ownership handling, and documented the exact-path `safe.directory` boundary.

## 2026-07-07 Current Main

- Merged Provider Strategy v1 with TianTianJieXi as the preferred Douyin ultra provider and KuKuTool as fallback/race backup.
- Fixed Douyin ultra race budgeting so TianTianJieXi is not penalized by KuKuTool fallback grace in race mode.
- Added batch parsing runtime settings with limited parallel workers and a longer total budget.
- Kept `/batch/` Douyin original/ultra checked by default when runtime allows it.
- Added/retained admin surfaces for Provider settings, Provider health tests, logs center, policy center, download runtime settings, and batch parse runtime settings.
- Clarified that provider capability gates live in `.env`, while daily exposure is controlled in admin runtime settings.

## Historical

- Upgraded runtime baseline to Python 3.14.5 and Django 5.2.x.
- Added production-oriented environment variable settings.
- Added development, deployment, architecture, and image library documentation.
- Added image library initialization command for generated `padded` assets.
- Refactored Xiaohongshu parsing/download helpers into services.
- Changed Xiaohongshu downloads to browser ZIP responses instead of arbitrary server save paths.
- Added environment-gated local server save mode for Xiaohongshu downloads.
- Changed Xiaohongshu image downloads to preserve original files by default, with image conversion as an explicit option.
- Added final redirect host validation and temporary-file ZIP creation for Xiaohongshu downloads.
- Hid image format and quality controls from the Xiaohongshu download UI so normal downloads always use original files.
- Added a platform adapter registry and migrated Xiaohongshu parsing behind the adapter interface.
- Added an initial Douyin adapter for page URL recognition and embedded JSON image/video extraction.
- Moved the image library to `/library/` and added SnapAny-style site navigation for home, Xiaohongshu, and Douyin.
- Simplified the header to site name plus settings, kept primary actions blue, and scoped platform colors to parsed results.
- Added Douyin mobile share-page fallback and public play URL variants for more reliable baseline video parsing.
- Added a baseline Bilibili adapter for BV/AV links, public mp4 video URLs, and cover downloads.
- Added video size display and browser ZIP percent progress through lightweight download tasks.
- Added a Douyin mobile feed candidate source and 365yg media-domain allowance for higher baseline video downloads.
- Added shared media candidate ranking/deduplication and refactored Douyin fallback parsing into a provider flow.
