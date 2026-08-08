# Changelog

## Unreleased

- Added a bounded, cache-only continuation for single-link Douyin ultra parsing so an already-running Provider can finish without requiring a second user click; signed tokens are client-bound, expire after 15 seconds, and permit at most five polls without starting another Provider call.
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
