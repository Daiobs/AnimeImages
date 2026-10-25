# Provider adapter foundation

This package implements P2 of [step 2](../../../docs/goals/unified-pipeline-step2.md),
under [the contract](../../../docs/architecture/unified-pipeline.md) sections 2.1,
2.2 and 9. It is not connected to routing, requests, health tests or downloads.

- `RawMapper`: `name`, `platforms`, and pure `map(raw, source_url) -> Work | Failure`.
- `ProviderAdapter` also provides `fetch(source_url, budget)` and `parse`.
- `Adapter` composes one fetch and one map. A fetch failure skips map. It does not
  retry, cache, schedule, select providers, issue grants or decide delivery.
- Use the existing kernel `invoke(adapter, source_url, budget)` to contain
  unexpected exceptions. Each family maps its expected errors to a kernel
  `Failure`, using `failure_for_http` or its explicit `failure_for_code` table.
  HTTP helpers consume a final response status, not intermediate redirects;
  a successful HTTP status does not mean the payload is a valid Work.
- Fetch must reuse an existing bounded client and respect the supplied budget.
  Parse checks the total elapsed budget after fetch and after map; it does not
  interrupt an uncooperative client or claim to stop remote processing.
  This foundation does not introduce another timeout/executor framework.

For raw data available only inside a subprocess, P3-P10 may provide a RawMapper
and fixture tests only. Do not invent a fetch that returns old candidates or
re-executes an upstream for shadow use. Raw transport/capture belongs to P12.
videodl is excluded from this step until production reachability is confirmed.

Map receives only upstream facts, never ParseResult, MediaAsset, candidates,
manifests or tokens. It must not use network, database, cache or time, nor mutate
the input. The kernel fills routed platform/provider and owns validation,
default selection, filenames, delivery and fact-gap reporting. Use its
`height_from_label` and `fact_gaps`; do not duplicate those rules. Unknown tracks
remain unknown, while explicitly known tracks must be preserved.

Offline tests in `xhs_download/tests_provider_adapters.py` enforce the forbidden
direct-import boundary across all adapter modules and provide `deny_map_io()`
for later family fixture tests. Each family must run its actual map with that
guard; foundation fixture tests alone do not verify a later adapter.
The tests are discovered by the existing Django CI entry, without workflow edits.

The [raw fixture format and local tool](../../fixtures/provider_raw/README.md)
preserve upstream structure while redacting sensitive values. These are test
recordings, not a second application result model. Shadow cache hits with no raw
will count `SHADOW_NO_RAW` in P12, without refetching.
