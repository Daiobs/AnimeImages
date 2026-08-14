# Service Worker UPSTREAM_FETCH_FAILED Forensics v1

Date: 2026-08-14

This report is intentionally redacted. It contains no source URL, signed CDN
URL, query value, Provider credential, cookie, manifest token, or user input.
Real resource URLs existed only in process or browser memory and were not
persisted.

## Status

```text
SW_UPSTREAM_FAILURE_ROOT_CAUSE_NARROWED_NOT_PROVEN
```

The evidence excludes a changed Service Worker or stale production static
artifact as the cause of the canonical-main convergence failure. It also
excludes persistent DNS, TLS, redirect, and CORS failure for the two fresh
TianTian resources inspected after rollback. The failure remains narrowed to a
transient upstream/browser fetch condition or a browser-profile/request-context
condition. The exact rejected-fetch reason was not captured at the failure
instant, so selecting one of those causes as proven would overstate the data.

## Baseline

| Item | Result |
| --- | --- |
| Production SHA | `87afba321a2dc459b7daebcd1cc9eea6735b60ba` |
| Canonical main SHA | `11642b0194b052f9368bf0024900e845fc6609c7` |
| Production service | `active/running`, `www-data`, no restart performed |
| Production readiness/homepage | HTTP 200 / HTTP 200 |
| Production checkout | Clean; branch is two commits behind its upstream, as before this audit |
| Research branch | `research/sw-upstream-fetch-failure-v1` |
| Production changed by this audit | No |

## Error Semantics

`UPSTREAM_FETCH_FAILED` has one admission point in
`ordinary_media_download_sw.js`: the `fetch()` promise rejects or throws before
an HTTP response is acquired. It does not represent the later checks below.

| Failure phase | Worker code |
| --- | --- |
| Descriptor expired before start | `DESCRIPTOR_EXPIRED` |
| `fetch()` rejected | `UPSTREAM_FETCH_FAILED` |
| HTTP, body, MIME, or source URL invalid | `UPSTREAM_MEDIA_INVALID` |
| Declared or received size too large | `UPSTREAM_SIZE_LIMIT_EXCEEDED` |
| Declared and received sizes differ | `UPSTREAM_SIZE_MISMATCH` |
| Reader failed after response acquisition | `UPSTREAM_STREAM_FAILED` |
| User or Download Manager cancellation | Dedicated cancellation code |

Consequently, the convergence symptom was `FETCH_REJECTED`, not an HTTP-status,
MIME, size, descriptor-expiry, or stream-completion failure.

## Source And Served Identity

The following files are byte-identical at production `87afba3` and canonical
main `11642b0`.

| File | Git blob | SHA-256 |
| --- | --- | --- |
| `ordinary_media_download_sw.js` | `6db0ae804523acdda62c34a68c51f6072a39651e` | `627bf83213616d5898dcc028b43feb751a4ac839801bec21a8254803af551e67` |
| `ordinary_media_streaming.js` | `04af7c50ec407c169ad708777958efee9b5cb264` | `e351fb9d1e88e39d2c9210f7adda1dbed20d6df25f614c1279d86597a3dd9905` |
| `ordinary_media_streaming_protocol.js` | `3fbfee5f8d482e54678e7ef972995816089b2762` | `26fc9e4935e24ebe7680bd7a9a727d581beafbb873c6c5993992abbec19e1000` |
| `ordinary_media_download.js` | `cd0f0783dc7ede457655a6f9ec5f3ab74002bf18` | `a57bdd7caac105684c2cbdbf6f57fe59425dbccaec50d6a3ddaa57d67dabaf62` |
| `xhs_download.js` | `d0d10b9c547f14ddee876bd98b3f1c144d6ef24f` | `5ffc954d6ef65a03b74ec2afb79ad1c0e340421a4ee658e33c43df1950fd933d` |
| `xhs_download.html` | `505833b1099c78b2a64902ac0282b83459e64038` | `ace506f820bd76bef991fffe7831582ad8177a00f9b52537d06f7d4d89b7d3b3` |
| `result_url_safety.js` | `5faf877ba6acfe5d0bbf1e00f13fadcac15804f7` | `eec5c7a17d0b2d4c2f72eeee73a82da44a8ab3ae66ed38ad1f3134371cf8b729` |
| `xhs_results_region.html` | `41f299c69ee998d9683d941b0aa36cf89a5c72d6` | `31a8f017096567d370b1d8aa33309c2452f1f789e820ce2fd3ef76f65e43bf43` |

The root Worker endpoint reads
`BASE_DIR/static/js/ordinary_media_download_sw.js` directly, not
`STATIC_ROOT`. Request-factory checks at both refs produced the same 14,519-byte
body and headers. Production served that exact body with:

- HTTP 200
- `Content-Type: text/javascript; charset=utf-8`
- `Content-Length: 14519`
- `Cache-Control: no-cache, no-store, must-revalidate`
- `Service-Worker-Allowed: /`
- `X-Content-Type-Options: nosniff`
- `Cross-Origin-Opener-Policy: same-origin`

The production source tree and collected static root also have matching hashes
for every core ordinary-media script. `SERVED_SOURCE_IDENTITY = PASS`.

## Static Reconstruction

Clean isolated collection produced 148 files for `87afba3` and 150 files for
`11642b0`. The normalized manifest hashes were:

- `87afba3`: `7289b6affd0cf6a2310f2f11842b2196eb2f4099f35e2c85712eaeea1d31ca5c`
- `11642b0`: `367853a5b865ca1070c5a16a74cf71d5afd197cfbe3868dabdc9840f73276144`

Exactly three static paths differ:

1. `static/js/batch_parse.js` changed.
2. `static/css/batch_runtime_admin.css` was added.
3. `static/js/batch_runtime_admin.js` was added.

This explains the convergence deployment's three copied files. On rollback,
only `batch_parse.js` requires content replacement; the two main-only Admin
files may remain as unreferenced collected artifacts. `collectstatic` copy
counts themselves are mtime-sensitive, so content manifests, not a local copy
count, are the authoritative reconstruction. No Streaming dependency was
copied or changed.

## Known-Good Retest And Browser Constraint

The original Chrome profile still contained a current-production result card
showing a completed 89.4 MiB Streaming transfer at 100 percent and a successful
browser-save handoff. This is supporting evidence that `87afba3` and the same
core Worker bytes can complete; it was not treated as a newly initiated clean
profile test.

A fresh Chrome window was opened through the supported Chrome control
integration; a verifiable isolated-profile run was not completed. Tab discovery
and claim succeeded, but CDP `Network.enable`,
`Target.setAutoAttach`, Runtime evaluation, and DOM operations repeatedly
timed out, including after the extension/native-host diagnostics passed and a
fresh window was created. Chrome reported a running framework revision older
than the installed application revision, which may indicate a pending browser
update, but this was not sufficient evidence of causality.

Because the supported CDP path could not attach reliably, this audit did not
capture the required Service Worker target fields (`Network.loadingFailed`,
`errorText`, `blockedReason`, `corsErrorStatus`) or current registration/cache
state. It also did not claim clean-old, clean-main, or old-to-main browser A/B
results. No unsupported browser substitute and no disabled-web-security mode
was used.

## Provider And CDN Evidence

Two real fresh TianTian upstream calls were used, below the task limit of three.
All resource identity remained memory-only.

### Sample 1

| Field | Result |
| --- | --- |
| Provider | `tiantianjiexi`; real upstream call |
| Provider selection / parse | About 1,408 ms / 2,441 ms |
| Media host | `v26-default.365yg.com` |
| Expected bytes | 93,733,891 |
| Product capability | Browser Streaming safe; CORS expected |
| First bounded GET/Range probe | Read timeout at 12 seconds |
| Cached-payload retry | Same resource; no new Provider call |
| DNS | Pass; IPv4 and IPv6 answers |
| TLS | Pass; TLS 1.3, about 67 ms |
| Range with production Origin | HTTP 206, no redirect, 1,048,576 bytes, about 2.0 s |
| Range without Origin | HTTP 206, no redirect, 1,048,576 bytes, about 2.2 s |
| CORS / type | `Access-Control-Allow-Origin: *`; `video/mp4` |
| Content-Range total | 93,733,891; MP4 signature valid |

The timeout followed by success against the identical cached resource is a
positive reproduction of a transient fetch-layer condition. It is not a
browser Service Worker error proof.

### Sample 2

| Field | Result |
| --- | --- |
| Provider | `tiantianjiexi`; real upstream call |
| Provider selection / parse | About 1,601 ms / 1,605 ms |
| Media host | `v6-default.365yg.com` |
| Expected bytes | 110,691,390 |
| Bounded GET with production Origin | HTTP 200, no redirect, first chunk in about 2.3 s |
| Range with production Origin | HTTP 206, no redirect, 1,048,576 bytes in about 0.8 s |
| CORS / type | `Access-Control-Allow-Origin: *`; `video/mp4` |
| Content-Range total | 110,691,390; MP4 signature valid |

The probes read only bounded data and did not transfer a full media body
through AnimeImages. They establish a negative control against persistent
CORS, DNS, TLS, redirect, MIME, or resource-signature failure.

## Service Worker And A/B Evidence

| Requested observation | Result |
| --- | --- |
| Page-context fetch | Not captured; supported CDP Runtime path timed out |
| Service Worker fetch | Convergence symptom was `UPSTREAM_FETCH_FAILED`; exact network error not captured |
| Bounded Blob fetch | Not captured; it uses the same browser fetch capability and was not guessed from HTTP probes |
| Worker scope/script/controller/active/waiting | Not captured in the failing profile |
| Worker protocol version | Source identity verified; runtime value not captured |
| Cache Storage signed URL | No persistence API exists in the implementation and prior runtime security checks were clean; current profile enumeration was blocked |
| `87afba3` controlled source | Not rerun in this audit |
| `11642b0` controlled source | Not rerun in this audit |
| Clean `87afba3` | Not completed because browser control failed |
| `87afba3` to `11642b0` upgrade profile | Not completed because browser control failed |
| Clean `11642b0` | Not completed because browser control failed |

The prior productionization report records exact completion for a real
110,691,390-byte Single and Batch resource and controlled 300/500 MiB streams.
The four core Streaming blobs at that proven production SHA are byte-identical
to both refs audited here. This is useful negative-control evidence, but it is
not relabeled as a new A/B run.

## Root-Cause Assessment

Primary classification: `UPSTREAM_RESOURCE_SPECIFIC`, most likely a transient
CDN-to-browser network rejection or edge condition.

Confidence: medium. The assessment is supported by the first sample's timeout
followed by success on the identical resource, current healthy CORS/Range
behavior across two CDN hosts, and exact application/static identity. It is not
high confidence because the browser's rejected-promise reason was not captured.

Excluded or strongly disfavored:

- changed Service Worker or ordinary-media client source;
- stale or mixed production Streaming static bytes;
- wrong root Worker body or cache headers;
- persistent CDN CORS denial for both inspected resources;
- persistent DNS or TLS failure;
- cross-host redirect failure in the inspected probes;
- descriptor expiry, invalid HTTP/MIME, size mismatch, or post-response reader
  failure as the meaning of the observed Worker code;
- Server Relay involvement.

Remaining uncertainty:

1. a transient browser/CDN network or edge rejection at the exact failure time;
2. browser-profile or Service Worker request-context state;
3. a Service Worker lifecycle condition that is not represented by changed
   source bytes.

This is not evidence for a canonical-main code/static regression. A full
browser lifecycle regression is not declared impossible because its runtime
state could not be captured.

## Security And Data Plane

- Signed URL persisted: **NO**.
- Credential, cookie, token, or query leak: **NO**.
- Full media transfer through AnimeImages: **NO**.
- Ordinary Server Relay enabled by this audit: **NO**.
- Production code, configuration, Runtime Settings, database, Redis, Nginx,
  systemd, and Service Worker registration changed: **NO**.

## Offline Verification

- `manage.py check`: pass.
- `manage.py makemigrations --check --dry-run`: pass; no changes detected.
- Provider Strategy: 119 tests passed.
- Full `xhs_download`: 1,047 tests passed.
- Ordinary-media Node tests: 45 passed.
- JavaScript syntax checks for the Worker and four client dependencies: pass.
- `git diff --check`: pass.
- Automated tests made no real Provider or production-media request.

## Acceptance-Gate Recommendation

The rollback decision was conservative and correct with the evidence available
at deployment time. Future deployment acceptance should separately report:

1. application identity: target SHA, served Worker hash, and client dependency
   hashes;
2. browser lifecycle: controller/script/scope/protocol and sanitized Worker
   target errors;
3. upstream health: at least one bounded fresh-resource capability probe and a
   resource-independent controlled origin;
4. product result: Browser Download Manager creation, exact bytes, and zero
   AnimeImages ordinary-media bytes.

A single `UPSTREAM_FETCH_FAILED` should still fail that smoke attempt, but it
should not by itself classify a deployment as an application regression when
application identity is unchanged and an upstream transient is independently
observed.

## Exact Next Task

`Service Worker Fetch Failure Telemetry and Acceptance Gate v1`

The next task should add a research-gated, URL-redacted diagnostic channel for
`error.name` and a fixed safe error category, capture Service Worker
`Network.loadingFailed` in a supported Chrome session, and rerun the same-origin
clean-old / upgrade / clean-main matrix against both a controlled CORS origin
and one bounded fresh Provider resource. It must not add ordinary Server Relay
fallback and must not deploy a behavioral fix until one remaining cause is
proven.
