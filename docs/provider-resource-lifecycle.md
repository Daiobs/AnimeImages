# Provider Resource Lifecycle

AnimeImages parses and authorizes media resources. Temporary Provider/CDN
resources may expire. This is a resource-lifecycle concern, not a separate
product mode or download strategy.

## Independent Clocks

### Provider Payload Cache

TianTianJieXi successful payloads use
`TIANTIANJIEXI_PAYLOAD_CACHE_TTL_SECONDS`, which defaults to 120 seconds. The
cached dictionary includes the media URL fields returned by the Provider.

The normal parse path checks this cache first. A hit returns the cached payload
without calling the TianTianJieXi API and without acquiring the single-flight
or global parse lease. A concurrent caller can also receive the payload written
by the request that currently owns the single-flight lease.

The failure cache is separate. It defaults to 30 seconds and can reject a
repeat attempt without an upstream request after a recent Provider failure.

Consequences:

- A parse or retry request can return a previously cached media URL.
- A successful parse request does not prove an upstream Provider call occurred.
- A successful parse request does not prove a different media URL was issued.
- Cache invalidation permits another upstream request; it does not control what
  URL the Provider returns.

### AnimeImages Provider Manifest

`PROVIDER_MANIFEST_MAX_AGE_SECONDS` defaults to 3600 seconds. The signed token
binds the AnimeImages asset ID, Provider identity, Provider resource ID,
Provider cache key, media URL, and media type when present.

Manifest validation has three results:

- `valid`: signature, signed identity, and token age are accepted.
- `expired`: the AnimeImages signing age was exceeded.
- `invalid`: the signature or signed identity does not match.

A valid manifest proves only the signed AnimeImages identity and token age. It
does not contact the CDN and does not prove that the upstream media URL remains
reachable.

### Provider/CDN Media URL

Media URL lifetime belongs to the upstream Provider/CDN contract. The current
TianTianJieXi integration does not receive, parse, or persist an authoritative
CDN expiry value, and the repository documentation does not establish one.

Current classification:

```text
CDN_URL_LIFETIME_UNKNOWN
PROVIDER_URL_CACHE_LIFETIME_RISK
```

Do not infer CDN lifetime from the 120-second payload cache or the 3600-second
manifest maximum age. Provider-specific evidence is required before changing
cache TTLs or expiry handling.

## Parse And Retry Semantics

User-facing parse and retry actions use the normal Provider path and may hit
the payload cache. They do not currently request a cache bypass.

Use these terms in tests and diagnostics only when proved:

- `uncached_upstream_parse`: the test verifies that the Provider was called
  rather than returning a payload-cache hit.
- `newly_issued_provider_result`: the test verifies that the Provider returned
  that result during the current upstream call.

Neither term is a product state, routing input, capability, runtime setting, or
architecture requirement.

## Historical Acceptance Wording

PR #31 and the Draft PR #32 used newly obtained TianTianJieXi samples as
controlled acceptance inputs. That isolated stale cache or manifest-expiry
interference for those experiments. It is not evidence for a separate product
mode and does not change the `valid` / `expired` / `invalid` manifest security
classification.
