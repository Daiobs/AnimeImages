# ParseHub Absorption Policy

## Relationship

AnimeImages is the product mainline, governance authority and final delivery
system. ParseHub is an external capability supply library and research source.

ParseHub-derived work may enter AnimeImages only as:

- an AnimeImages Provider or adapter implemented under AnimeImages contracts; or
- development/offline tooling.

It may not become:

- a view-layer return object;
- a public runtime registry;
- the ordinary-media download architecture;
- the permission system; or
- the source of security policy.

## Mandatory adoption gate

Every future absorption task must:

1. Pin the upstream repository and exact commit.
2. Record source files and license.
3. Remove embedded credentials, session material and examples.
4. Replace upstream redirects with the AnimeImages safe resolver.
5. Map results into AnimeImages-owned candidate, `MediaAsset` and `ParseResult`
   contracts.
6. Add `ProviderOutcome`, one request deadline and per-call remaining-time
   enforcement.
7. Apply page/media host admission and blocked hostname/IP checks after every
   redirect.
8. Add sanitized logging that never records source/media URLs, query strings,
   credentials or raw Provider payloads.
9. Add offline/focused/full regression tests.
10. Run a Staff-only smoke with explicit capability and runtime gates.
11. Complete controlled production acceptance.
12. Require manual approval before any Public or public-gray rollout.

Supplier breadth never grants public availability by itself.

## Classification meanings

- `ADOPT_CANDIDATE`: eligible for a separate implementation/proof task.
- `REFERENCE_ONLY`: concepts, fields or algorithms may inform local design.
- `DEMAND_DRIVEN`: start only after repeated real user demand.
- `ALREADY_STRONGER_IN_ANIMEIMAGES`: preserve the current local capability.
- `SECURITY_REVIEW_REQUIRED`: credentials, signing, redirects, login state or
  host risk block implementation.
- `REJECT_DIRECT`: do not integrate the upstream component or architecture.

These are the only allowed matrix classifications.

## Capability provenance contract

The following docs-first contract is proposed for future derived capability
records. It is not a model and creates no migration.

```json
{
  "implementation_origin": "parsehub_derived",
  "upstream_project": "z-mio/ParseHub",
  "upstream_commit": "<exact-commit>",
  "upstream_files": ["<reviewed-path>"],
  "local_implementation_version": "<animeimages-version>",
  "local_patch_summary": "<sanitized-summary>",
  "license": "MIT",
  "review_status": "reviewed"
}
```

Allowed `implementation_origin` values:

- `animeimages_native`
- `parsehub_derived`
- `external_provider`
- `yt_dlp`
- `gallery_dl`

### Visibility

| Field | Visibility | Runtime candidate? |
| --- | --- | --- |
| `implementation_origin` | Staff-only diagnostics; docs | Yes, if operationally useful |
| `upstream_project` | Staff-only diagnostics; docs | Optional |
| `upstream_commit` | Staff-only diagnostics; docs | Optional |
| `upstream_files` | Docs-only | No |
| `local_implementation_version` | Staff-only diagnostics; docs | Yes |
| `local_patch_summary` | Docs-only | No |
| `license` | Docs and notices | No public result metadata |
| `review_status` | Staff-only diagnostics; docs | Yes |

No provenance field should be displayed to public parse/download users. Runtime
metadata remains a future design candidate and must not contain paths, URLs,
credentials or Provider payloads.

## Permanent boundaries

The explicit registry, ProviderOutcome model, host admission, manifests,
Public/Staff gates, Browser Streaming and zero-relay ordinary-media contract
remain AnimeImages-owned and authoritative.
