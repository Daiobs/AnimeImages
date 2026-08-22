# Rich Media Domain Contract Status

## Provenance

The ParseHub capability-supply baseline remains pinned to:

- project: `z-mio/ParseHub`
- commit: `144260f4349c5bb57415f7555a572fcd48c3b8ba`
- version: `2.2.0`
- license: MIT

ParseHub is a concept and static platform-knowledge reference only. No upstream
source, package, dependency, parser or runtime object is imported by the rich
media baseline.

## Current status

- `OFFLINE_CONTRACT = COMPLETE`
- `RUNTIME_CONTRACT = IMPLEMENTED_IN_DRAFT`
- `FIRST_CONSUMER = BILIBILI_DYNAMIC_LIVEPHOTO`
- `PLATFORM_IMPLEMENTATION = NOT_STARTED`

The first selected adoption candidate, AnimeImages-owned external-tool process
lifecycle hardening, is complete. The next research gate is now represented by:

- `docs/research/rich_media/01_CURRENT_MODEL_AUDIT.md`
- `docs/research/rich_media/02_DOMAIN_CONTRACT_V1.md`
- `docs/research/rich_media/03_COMPATIBILITY_MATRIX.md`
- `docs/research/rich_media/04_SECURITY_AND_VALIDATION.md`
- `docs/research/rich_media/05_FIRST_CONSUMER_PLAN.md`
- `tools/rich_media_contract/`

The runtime Draft is an independent AnimeImages-owned typed implementation. It
does not import ParseHub or `tools/`, and current Providers continue to emit no
rich-media value. Parsing, registry behavior, views, downloads, database,
dependencies and production remain unchanged.

## Next candidate

After runtime-contract review, the first proposed consumer remains Bilibili
Dynamic/LivePhoto under separate research, staff-only integration,
presentation/download and production-acceptance PRs. No platform or public
capability is implied by this status update.
