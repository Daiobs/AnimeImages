# Upstream Provider Lab v1

## Status

```text
UPSTREAM_PROVIDER_LAB = STAFF_LOCAL_LAB_ONLY
WHOLE_REPOSITORY_PROVIDERS = PARSEHUB + VIDEODL
PUBLIC_ROUTE_CHANGE = NO
PLATFORM_ROUTE_CHANGE = NO
AUTO_ROUTING = NO
RESULT_PERSISTENCE = NONE
PROVIDER_DOWNLOADERS = DISABLED
PRODUCTION_DEPLOYMENT = NO
FIRST_BILIBILI_REAL_COMPARISON_AT_INITIAL_V1 = PENDING_USER_INPUT
FIRST_BILIBILI_REAL_COMPARISON_FINAL = COMPLETE_3_SAMPLES
```

Provider Lab is an explicit comparison surface for Staff research. A Staff
member supplies one public page URL or share text, chooses one or more known
providers, and sees each provider finish independently. AnimeImages owns the
safe presentation contract, but the Lab does not choose a winner or alter any
production platform route.

## Access And Flags

The only page is:

```text
/admin/provider-lab/
```

The page and both JSON endpoints require an authenticated, active Staff user.
They return `404` when the main feature flag is disabled or the caller is not
authorized. Responses are CSRF-protected, `no-store`, and `nosniff`.

```text
UPSTREAM_PROVIDER_LAB_ENABLED=false
VIDEODL_PROVIDER_LAB_ENABLED=false
```

Both defaults are fail-closed. videodl additionally requires its own explicit
local enablement because its pinned license does not authorize a public or
production route. The Admin navigation entry follows the main flag.

## Explicit Provider Registry

| Provider ID | Source | Version / pin | Scope | Default |
| --- | --- | --- | --- | --- |
| `animeimages_current` | current `parse_media_page()` result | current checkout | runtime comparison | selected |
| `parsehub_repo` | z-mio/ParseHub | `144260f4349c5bb57415f7555a572fcd48c3b8ba` | Lab only; MIT production candidate | selected |
| `videodl_repo` | CharlesPikachu/videodl | `6cd1de0fe1209380b183b654c65e98a629637873` | noncommercial local Staff Lab only | selected when enabled |
| `yt_dlp_existing` | existing AnimeImages yt-dlp adapter | installed runtime version | metadata/parse only | optional |
| `tiantian_existing` | existing TianTian API adapter | configured runtime | parse only | optional |

The registry is a fixed mapping. There is no entry-point discovery, plugin
loader, planner, scoring system, DAG, or platform-specific ParseHub/videodl
registration. KuKuTool is reserved for a later decision and is not in v1.

Listing the page performs local configuration checks only. It does not probe
yt-dlp versions or call the TianTian account/status endpoint.

## Repository Pins And Licenses

ParseHub is installed from its exact Git commit and verified as package version
`2.2.0` with matching `direct_url.json` VCS metadata. Its worker calls only:

```python
ParseHub().parse_sync(source, cookie=None, proxy=None)
```

Its MIT license is recorded as `lab_only_production_candidate`. The Lab does
not call `download()`, `download_sync()`, or any ParseHub downloader.

videodl is installed from its exact Git commit and verified as distribution
version `0.9.1`, package license `PolyForm-Noncommercial-1.0.0`, and matching
VCS metadata. Its worker calls only:

```python
VideoClient(...).parsefromurl(source)
```

The client disables proxy discovery, session maintenance, random user-agent
updates, cookies, and printing. `VideoClient.download()` and all provider
download methods are outside the Lab. The UI and exported result always label
this provider `noncommercial_lab_only`.

## Isolated Worker Model

ParseHub and videodl have separate virtual environments and separate worker
scripts. Every execution creates one child process and exchanges one bounded
JSON request and response over stdin/stdout.

```text
source length             <= 4096 characters
assets                    <= 64
relationships             <= 64
stdout                    <= 1 MiB
stderr                    <= 128 KiB
worker timeout            <= 60 seconds hard cap
raw shape                 <= 256 KiB
```

Each request receives temporary `HOME`, `XDG_CONFIG_HOME`, `XDG_CACHE_HOME`,
and working directories. The child inherits only the minimum runtime values:
`PATH`, locale, and optional TLS certificate paths. Proxy, Cookie,
Authorization, token, password, browser-profile, and download-directory
environment variables are not inherited.

The runtime reuses AnimeImages' hardened external-process lifecycle: a new
process session/group, live stdout/stderr limits, bounded timeout, TERM grace,
KILL fallback, descendant cleanup, process reaping, and temporary-directory
cleanup. Python provider packages are never imported into the Django process.

Bootstrap examples:

```bash
python tools/provider_lab/runtime/bootstrap_provider.py \
  --provider parsehub \
  --target /tmp/animeimages-provider-lab/parsehub

python tools/provider_lab/runtime/bootstrap_provider.py \
  --provider videodl \
  --target /tmp/animeimages-provider-lab/videodl
```

`--check-only` verifies an existing environment. The videodl bootstrap prints
the noncommercial research warning. No provider environment is committed and
AnimeImages application dependencies are unchanged.

## LabProviderResultV1

`LabProviderResultV1` is a Lab presentation contract, not `ParseResult`,
`MediaAsset`, a production provider envelope, or download authority. It keeps:

- provider identity, exact version, scope, status, and elapsed time;
- claimed platform and selected upstream backend when actually returned;
- result kind and bounded title/author/content metadata;
- asset descriptors without media URLs;
- explicit relationships such as same-object Live Photo still/motion pairs;
- fixed warnings and error codes; and
- a structurally sanitized raw shape.

Unknown values remain unknown. The normalizer does not infer dimensions,
sizes, codecs, merge requirements, or relationships that the upstream did not
provide.

## Raw Shape Sanitization

The sanitized shape preserves class names, field names, primitive types,
counts, nesting, fixed booleans/enums, safe numbers, and bounded ordinary text.
It redacts URL-like values and fields associated with URLs, queries, Cookies,
Authorization, tokens, secrets, headers, proxies, file paths, raw bodies, and
payload hashes.

```text
max depth                 6
max keys per mapping      128
max inspected list items  64
max safe string           512 characters
max encoded shape         256 KiB
```

Only this sanitized result can be copied or downloaded. Complete upstream
payloads are never offered for export.

## Resource Tokens And Browser Inspection

An admitted asset stores its exact URL only in the server cache and exposes a
random opaque token in the Lab result. The token is bound to:

- active Staff user ID;
- request ID;
- provider ID;
- asset ID;
- exact URL and media type; and
- header/cookie presence flags.

The token expires at a fixed point 300 seconds after issuance and permits at
most 10 descriptor requests. It contains no URL or credential. There is no
database record.

Only a Staff click requests `/admin/provider-lab/resource/`. The endpoint
returns a descriptor, never a media body. Resources that require headers or
cookies remain non-inspectable and do not reveal their URL. Otherwise the
browser may load image metadata or video metadata and perform one credential-
free Range probe capped at 4096 bytes. Initial HTML, card JSON, copied JSON,
downloaded JSON, errors, and application logs contain no media URL.

## UI Execution And Retention

The browser sends one `/admin/provider-lab/run/` request per selected provider
and runs at most three requests concurrently. Cards update as soon as their own
request finishes; one failure cannot block another card. The server never
fans out or picks a provider automatically.

Input is limited to 4096 characters, the explicit registry contains five
providers, each Staff user receives 20 provider executions per minute, and the
same Staff/source/provider tuple has a 30-second single-flight guard. Existing
TianTian limits, cooldown, and circuit behavior remain authoritative when that
provider is explicitly selected.

Results live only in the current DOM and short-lived cache. There is no model,
migration, history page, operation log, result file, localStorage,
sessionStorage, or IndexedDB persistence. Refreshing the page clears results.

## Product Boundaries

Provider Lab does not change XHS, Bilibili, Douyin, YouTube, Batch, public
download, provider order, provider timeout, manifest, or browser handoff
behavior. It does not expose a public route and does not transfer full media
through AnimeImages. The separate formal ParseHub XHS Runtime isolation is not
refactored by this work.

Bilibili was the intended first manual comparison platform, but no Bilibili-
specific provider route is part of v1. The initial research snapshot was
pending user-supplied transient samples; the final PR #76 comparison completed
three real video samples:

```text
FIRST_BILIBILI_REAL_COMPARISON = COMPLETE_3_SAMPLES
ANIMEIMAGES_NATIVE_BYTES = OFFICIAL_DURL_BYTES
TIANTIAN_QUALITY_DELTA = NONE_PROVEN
VIDEODL_QUALITY_DELTA = NONE_PROVEN
PARSEHUB_DELIVERABLE_MEDIA_DELTA = NONE_PROVEN
YT_DLP_STATUS = PROVIDER_PROCESS_FAILED
BILIBILI_ROUTE_DECISION = KEEP_CURRENT_NATIVE
```

Native, TianTian, videodl, and official DURL produced byte-equivalent complete
media for all three samples. ParseHub exposed additional format metadata but
did not yield an admitted deliverable media candidate. The yt-dlp failure was
isolated and recorded as a Provider-specific nonblocking limitation. The Lab
therefore supported a fixed-route decision without changing the formal
Bilibili adapter.

## Adding Another Whole-Repository Provider

A future whole-repository upstream requires exactly these steps:

1. record the repository, immutable commit, version, and license scope;
2. add a parse-only isolated worker with the bounded JSON protocol;
3. normalize evidence into `LabProviderResultV1` without inventing fields;
4. add one explicit provider ID to the fixed registry;
5. add lifecycle, normalization, security, and UI tests; and
6. expose the provider in the Staff Lab.

It does not require one AnimeImages adapter per platform. Platform routing is a
separate, later product decision based on reviewed Lab evidence.
