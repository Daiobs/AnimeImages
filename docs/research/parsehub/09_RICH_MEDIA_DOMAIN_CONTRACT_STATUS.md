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

The first selected adoption candidate, AnimeImages-owned external-tool process
lifecycle hardening, is complete. The next research gate is now represented by:

- `docs/research/rich_media/01_CURRENT_MODEL_AUDIT.md`
- `docs/research/rich_media/02_DOMAIN_CONTRACT_V1.md`
- `docs/research/rich_media/03_COMPATIBILITY_MATRIX.md`
- `docs/research/rich_media/04_SECURITY_AND_VALIDATION.md`
- `docs/research/rich_media/05_FIRST_CONSUMER_PLAN.md`
- `tools/rich_media_contract/`

This is an offline contract baseline, not runtime adoption. It does not change
AnimeImages parsing, Provider behavior, downloads, database, dependencies or
production.

## Next candidate

After contract review, the first proposed consumer remains Bilibili
Dynamic/LivePhoto under separate research, staff-only integration,
presentation/download and production-acceptance PRs. No public capability is
implied by this status update.
