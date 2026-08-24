# XHS LivePhoto Browser Transport Evidence v1

## Status

```text
PR70_CANDIDATE_LOCATOR = MERGED
XHS_LIVEPHOTO_RELATIONSHIP_MAPPER_READY = YES
XHS_LIVEPHOTO_CANDIDATE_SELECTION_READY = YES
XHS_LIVEPHOTO_STILL_FORMAT_READY = YES
XHS_LIVEPHOTO_MOTION_FORMAT_READY = YES
XHS_LIVEPHOTO_HTTPS_NORMALIZATION_READY = YES
XHS_LIVEPHOTO_HTTP_HTTPS_EQUIVALENCE_READY = NO
XHS_LIVEPHOTO_BROWSER_CORS_READY = NO
XHS_LIVEPHOTO_HOST_REVIEW_READY = NO
XHS_LIVEPHOTO_ASSET_SPEC_MAPPER_READY = NO
XHS_LIVEPHOTO_PROVIDER_INTEGRATION_READY = NO
CURRENT_XHS_BROWSER_ADMISSION_CONSUMERS = 0
CURRENT_XHS_PROVIDER_RICH_MEDIA_EMISSION = 0
RAW_HTTP_BROWSER_READY = NO
PRODUCTION_DEPLOYMENT = NO
```

The fixed blocker is:

```text
XHS_BROWSER_TRANSPORT_BLOCKED_BROWSER_EVIDENCE_UNAVAILABLE
required_host_class = motion_zl
```

This is a fail-closed research result. Three reviewed host classes passed
HTTPS equivalence and real Chromium body-readability checks. The required
`motion_zl` class appeared in transient exact-note payloads early in the
review, but no candidate value was retained. Later bounded fresh snapshots did
not supply that class again, so no HTTP/HTTPS or browser request was made for
it. Results from other classes are not used as a substitute.

## Scope And Boundaries

This phase builds on the candidate locator merged in PR #70. That baseline
already provides:

- exact-note relationship selection;
- deterministic still and motion candidate selection;
- `urlDefault` / `WB_DFT` drift rejection;
- structurally absent versus present-invalid `masterUrl` handling;
- same-selected-payload-snapshot locator scope; and
- per-candidate image/video magic evidence.

This phase adds only a dormant AnimeImages-owned HTTPS admission helper,
sanitized browser transport evidence, offline tests, and documentation. It does
not build an Asset Spec, `MediaAsset`, rich-media contract, Provider manifest,
adapter wiring, registry entry, route, UI, or download behavior.

Five previously reviewed public controls were used transiently: two LivePhoto
notes, one ordinary gallery, one ordinary video, and one invalid/deleted note.
No source link, note identity, title, author, raw payload, candidate URL, path,
signed parameter value, Cookie, request header, or response header is retained.
No complete media was saved.

## HTTPS Admission Helper

The dormant module is:

```text
xhs_download/services/platforms/xhs_livephoto_browser_admission.py
```

Its entry point accepts only `XhsPrivateMediaCandidateV1` values produced after
candidate selection:

```text
normalize_xhs_livephoto_candidate_https(private_candidate, member_role=...)
```

The returned `XhsPrivateHttpsMediaCandidateV1` excludes its value from repr,
comparison, hashing, and structural output. Structural evidence contains only
schema version, closed member role, reviewed host class, and whether an HTTP
upgrade was applied.

### Reviewed classes

| Role | Closed class | Classifier |
| --- | --- | --- |
| still | `still_exact` | exact reviewed still hostname |
| motion | `motion_versioned` | reviewed numeric version label |
| motion | `motion_zl` | exact reviewed alternate hostname |
| motion | `motion_backup` | reviewed numeric backup label |

These labels are research classifications, not production wildcard rules.

### Normalization rules

- HTTP is upgraded only for a reviewed class and only on no port or port 80.
- Explicit HTTP port 80 is removed so the output uses default HTTPS transport.
- HTTPS is accepted only on no port or port 443.
- Hostname, resource path, and signed parameter bytes remain unchanged.
- The client-only fragment is removed.
- Protocol-relative and non-HTTP schemes fail closed.
- Userinfo, passwords, IP literals, localhost, trailing dots, empty paths,
  backslash paths, non-default ports, lookalikes, and unrelated CDN hosts fail
  before network access.

Unknown-host negative controls produced zero network requests.

## Real Sample Identity And Candidate Baseline

| Sample | Exact identity | Items | LivePhoto | Regular images | Candidate result |
| --- | --- | ---: | ---: | ---: | --- |
| `xhs-livephoto-01` | yes | 14 | 14 | 0 | 14 still + 14 selected motion |
| `xhs-livephoto-02` | yes | 17 | 12 | 5 | 17 still + 12 selected motion |
| `xhs-gallery-01` | yes | 4 | 0 | 4 | four still, zero motion |
| `xhs-video-01` | yes | one cover | 0 | not mapped | cover transport control only |
| `xhs-invalid-01` | no | 0 | 0 | 0 | fail closed, zero media request |

Relationship and candidate selection always ran against the same exact-note
`imageList` snapshot. Candidate locators were never reused across a refresh.
Cross-snapshot aggregation retained only private candidates long enough for the
bounded transport check; no locator or value was persisted.

## HTTP / HTTPS Equivalence

Each retained representative used an HTTP and normalized HTTPS request with a
4,096-byte Range. If the still server ignored Range and returned status 200,
the client inspected only the first bounded chunk and closed the response.

The comparison required:

- magic format;
- declared content class;
- reported response total;
- bounded-prefix SHA-256 equality;
- final reviewed host class; and
- redirect class.

| Host class | Representatives | Prefix hash | Reported total | Format | Redirect | Result |
| --- | ---: | ---: | ---: | --- | --- | --- |
| `still_exact` | 2 | 2/2 match | 2/2 match | JPEG | none | pass |
| `motion_versioned` | 2, two numeric labels | 2/2 match | 2/2 match | MP4 | none | pass |
| `motion_zl` | 0 fresh candidates | not run | not run | not run | not run | blocked |
| `motion_backup` | 2, two numeric labels | 2/2 match | 2/2 match | MP4 | none | pass |

Fresh still candidates returned JPEG magic in this phase, whereas the PR #70
batch observed WebP for its selected still candidates. This is not treated as
drift or inferred globally: admission always follows the current response
magic rather than a field name, MIME-only claim, or URL suffix.

Latency values were observed only while controlling request bounds and were
not retained in the sanitized artifact. They are explicitly `not_retained`,
not reconstructed.

## Browser Harness

The browser proof used:

```text
origin = local HTTP loopback
browser = Playwright Chromium
profile = ephemeral, no user profile
credentials = omit
referrerPolicy = no-referrer
cache = no-store
```

The browser received each private HTTPS candidate only as an in-memory
evaluation argument. The harness read one stream chunk, inspected at most
4,096 bytes, cancelled the reader, aborted the request, and closed the page.
It did not create a Blob, save a file, invoke an AnimeImages media endpoint, or
use Server Relay.

### Browser observations

| Host class | Normal GET | Range GET | Response type | Body | Magic | Final host |
| --- | ---: | ---: | --- | --- | --- | --- |
| `still_exact` | 2/2 | not required | `cors` | 2/2 readable | JPEG | reviewed same class |
| `motion_versioned` | 2/2 | 2/2, status 206 | `cors` | 2/2 readable | MP4 | reviewed same class |
| `motion_zl` | not run | not run | not observed | not observed | not observed | not observed |
| `motion_backup` | 2/2 | 2/2, status 206 | `cors` | 2/2 readable | MP4 | reviewed same class |

`Content-Range` was not exposed to JavaScript. That is recorded but is not a
CORS failure because the response was non-opaque, `response.body` was readable,
and the MP4 prefix was valid. Browser console errors were zero.

The aggregate `BROWSER_CORS_READY` remains false because the required
`motion_zl` row has no fresh browser evidence. A server-observed wildcard CORS
header and success on sibling host classes do not grant that missing proof.

## Negative Controls

### Ordinary gallery

One selected still candidate passed HTTPS browser fetch and image magic.
Motion fetch count remained zero.

### Ordinary video

The note-level cover passed as a transport-only still control. It never entered
the LivePhoto candidate map and LivePhoto fetch count remained zero.

### Invalid/deleted note

Exact identity was not confirmed. Candidate count and media request count were
both zero.

## Readiness Decision

```text
RELATIONSHIP_MAPPER_READY = YES
CANDIDATE_SELECTION_READY = YES
STILL_FORMAT_READY = YES
MOTION_FORMAT_READY = YES
HTTPS_NORMALIZATION_READY = YES
HTTP_HTTPS_EQUIVALENCE_READY = NO
BROWSER_CORS_READY = NO
BROWSER_BODY_READABLE_READY = NO
HOST_REVIEW_READY = NO
ASSET_SPEC_MAPPER_READY = NO
PROVIDER_INTEGRATION_READY = NO
```

The body-readability gate is globally false only because one required class was
not tested; all executed browser observations were readable and non-opaque.

The next admissible research action is narrow: obtain a fresh exact-note
`motion_zl` candidate, run the same two bounded HTTP/HTTPS comparisons and two
browser normal/Range reads, and update this artifact. Do not infer the result,
retain a stale signed candidate, synthesize a hostname, or use Server Relay.

Even if that gate later passes, Provider integration remains closed. Separate
work is still required for asset/group identity, `XhsRichMediaAssetSpec`,
`MediaAsset`, Provider provenance, manifest authorization, adapter wiring, UI,
and download product decisions.

## Runtime Impact

```text
CURRENT_XHS_ADAPTER_DIFF = 0
REGISTRY_DIFF = 0
CURRENT_PROVIDER_RICH_MEDIA_EMISSION = 0
MANIFEST_DIFF = 0
PUBLIC_ROUTE_DIFF = 0
TEMPLATE_DIFF = 0
STATIC_DIFF = 0
DOWNLOAD_DIFF = 0
HOST_ALLOWLIST_DIFF = 0
SETTINGS_DIFF = 0
MIGRATION = NONE
DEPENDENCY_DIFF = 0
PRODUCTION_ACCESS = NO
PRODUCTION_DEPLOYMENT = NO
```

The URL-free evidence artifact is:

```text
research/evidence/xhs_livephoto_browser_transport_v1.json
```

Related baselines:

- [Real payload evidence](15_XHS_LIVEPHOTO_REAL_PAYLOAD_EVIDENCE_V1.md)
- [Relationship mapper](16_XHS_LIVEPHOTO_RELATIONSHIP_MAPPER_V1.md)
- [Candidate and format evidence](17_XHS_LIVEPHOTO_CANDIDATE_FORMAT_HOST_V1.md)
