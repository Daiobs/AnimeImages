# Changelog

## Unreleased

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
