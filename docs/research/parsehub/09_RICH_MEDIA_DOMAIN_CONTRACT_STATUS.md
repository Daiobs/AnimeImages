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

- `OFFLINE_CONTRACT = MERGED`
- `RUNTIME_CONTRACT = MERGED_DORMANT`
- `CURRENT_PROVIDER_EMISSION = 0`
- `CURRENT_RUNTIME_CONSUMERS = 0`
- `XHS_RELATIONSHIP_MAPPER = MERGED`
- `XHS_CANDIDATE_SELECTION = MERGED`
- `XHS_VERIFIED_BROWSER_HOST_SUBSET = MERGED`
- `XHS_FULL_BROWSER_HOST_COVERAGE = NO`
- `XHS_ASSET_SPEC_MAPPER = IMPLEMENTED_AFTER_CORRECTION`
- `XHS_HOST_CLASS_TRANSPORT_POLICY = VERIFIED_SUBSET`
- `XHS_FORMAT_PROOF_SCOPE = EXACT_CANDIDATE`
- `XHS_CANDIDATE_SNAPSHOT_BINDING = EXACT_CANDIDATE`
- `XHS_EXACT_CANDIDATE_BROWSER_FETCH = NOT_REQUIRED_BY_ASSET_SPEC_MAPPER`
- `XHS_RICH_MEDIA_BRIDGE = COMPATIBILITY_PROVEN_NOT_WIRED`
- `PLATFORM_RUNTIME_INTEGRATION = NOT_STARTED`

The first selected adoption candidate, AnimeImages-owned external-tool process
lifecycle hardening, is complete. The merged Rich Media contract baseline is
documented by:

- `docs/research/rich_media/01_CURRENT_MODEL_AUDIT.md`
- `docs/research/rich_media/02_DOMAIN_CONTRACT_V1.md`
- `docs/research/rich_media/03_COMPATIBILITY_MATRIX.md`
- `docs/research/rich_media/04_SECURITY_AND_VALIDATION.md`
- `docs/research/rich_media/05_FIRST_CONSUMER_PLAN.md`
- `tools/rich_media_contract/`

The runtime contract merged in PR #60 as an independent AnimeImages-owned typed
implementation. It does not import ParseHub or `tools/`, and current Providers
continue to emit no rich-media value. The dormant XHS normalizer merged in PR
#63 also has no runtime consumer.

The XHS research sequence now includes exact-note relationship mapping,
candidate location and magic format evidence, and the browser-verified host
subset merged in PR #71. The Asset Spec mapper described in
`19_XHS_LIVEPHOTO_ASSET_SPEC_MAPPER_V1.md` keeps that class-level transport
policy separate from factory-only magic format proof bound to one exact
normalized candidate. Mapping then revalidates the same candidate against the
current selected snapshot. The dormant compatibility layer rejects the
discovered but unverified `motion_zl` class, closes source-item and group
cardinality, and does not construct `MediaAsset`, Provider results, manifests,
or downloads.

## Next Gate

No first runtime consumer has been approved. XHS and Bilibili remain separate
dormant research tracks. Any platform integration still requires its own
Provider provenance, manifest, adapter, registry, UI/download, security, and
production-acceptance work. No platform or public capability is implied by this
status update.
