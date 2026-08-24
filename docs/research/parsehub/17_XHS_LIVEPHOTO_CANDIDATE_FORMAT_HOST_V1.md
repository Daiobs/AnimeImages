# XHS LivePhoto Candidate Selection And Format/Host Evidence v1

## Status

```text
PR68_RELATIONSHIP_MAPPER = MERGED
XHS_LIVEPHOTO_CANDIDATE_LOCATOR = IMPLEMENTED_IN_DRAFT
XHS_LIVEPHOTO_CANDIDATE_SELECTION_READY = YES
XHS_LIVEPHOTO_STILL_SELECTION = PASS
XHS_LIVEPHOTO_MOTION_SELECTION = PASS
XHS_LIVEPHOTO_STILL_FORMAT_READY = YES
XHS_LIVEPHOTO_MOTION_FORMAT_READY = YES
XHS_LIVEPHOTO_HOST_REVIEW = PARTIAL_BROWSER_CORS_NOT_PROVEN
XHS_LIVEPHOTO_ASSET_SPEC_MAPPER_READY = NO
XHS_LIVEPHOTO_PROVIDER_INTEGRATION_READY = NO
CURRENT_XHS_CANDIDATE_LOCATOR_CONSUMERS = 0
CURRENT_XHS_PROVIDER_RICH_MEDIA_EMISSION = 0
DEPLOYED = NO
```

This phase closes the deterministic still and motion candidate-selection gap
left by the relationship mapper merged in PR #68. It also records bounded,
per-candidate format and host evidence. It does not grant URL admission,
download authority, Provider emission, or browser-direct readiness.

## Boundaries

Five previously supplied public samples were inspected in memory: two
LivePhoto notes, one ordinary gallery, one ordinary video, and one
invalid/deleted control. Exact target-note identity remained mandatory before
an image list could enter this review.

The collection retained no source link, note identity, title, author, media
candidate, query, request header, or raw payload. Media probes read at most
4,096 bytes and closed the response; no complete media was saved. The
repository artifact contains only opaque sample names, counts, closed enums,
host-family labels, and fixed outcomes.

The invalid sample never produced an exact note. It therefore produced no
candidate locator and no media probe.

## Relationship Baseline

PR #68 maps only direct same-item relationship evidence:

```text
same item
  + exact boolean LivePhoto marker
  + still field
  + motion field
  = LivePhoto relationship
```

The source ordinal preserves order and lets a later step re-read the same
already-verified item. It is not relationship or pairing authority. Candidate
selection continues to reject cross-item, equal-position, filename, URL-shape,
and `firstNoteId` inference.

## Sample Matrix

| Sample | Exact identity | Items | LivePhoto | Regular images | Candidate result |
| --- | --- | ---: | ---: | ---: | --- |
| `xhs-livephoto-01` | yes | 14 | 14 | 0 | 14 still + 14 motion |
| `xhs-livephoto-02` | yes | 17 | 12 | 5 | 17 still + 12 motion |
| `xhs-gallery-01` | yes | 4 | 0 | 4 | 4 still only |
| `xhs-video-01` | yes | 1 cover | 0 | not mapped | no candidate map |
| `xhs-invalid-01` | no | 0 | 0 | 0 | fail-closed; no probe |

The mixed sample is the ordering control: five regular images remain in their
original positions while twelve LivePhoto items receive both member locators.

## Still Candidate Evidence

The selection population contains 35 image-note items across the two positive
samples and gallery control.

| Signal | Result |
| --- | ---: |
| `urlDefault` present | 35 / 35 |
| `infoList` present | 35 / 35 |
| observed scenes | `WB_DFT`, `WB_PRV` |
| `urlDefault` equals `WB_DFT` value | 35 / 35 |
| `urlDefault` total size greater than `WB_PRV` | 35 / 35 |
| selected still magic proven | 35 / 35 |

Each item exposed three field references but two unique candidate values:
`urlDefault` and the `WB_DFT` entry were identical; `WB_PRV` was a distinct,
smaller preview candidate. This gives a deterministic policy without treating
the first info-list entry as authoritative.

Those 35 image-note items account for 70 unique still probes. Two additional
still probes came from the ordinary-video control's cover fields, bringing the
format-review total to 72. The video-control probes were classification
controls only: they never produced a still locator or entered the LivePhoto
candidate map.

### Still policy

1. Select a non-empty `urlDefault`.
2. If that field is absent, select exactly one non-empty `WB_DFT`
   `infoList[].url`.
3. Do not use `WB_PRV` as a full still fallback.
4. Multiple eligible `WB_DFT` fallbacks are ambiguous and fail closed.
5. Cross-check the total still-candidate count against the PR #68 relationship
   item before returning a locator.

This policy does not compare URLs at runtime and does not infer format from a
field name.

## Motion Candidate Evidence

All 26 LivePhoto items exposed the same closed codec-key set:

```text
av1
h264
h265
h266
```

Only `h264` contained candidates. The other reviewed codec keys were empty.
The `h264` key appeared at ordinals 0, 1, 2, and 3 across the samples, proving
that dictionary position is not a stable codec identity.

Every LivePhoto item contained:

- one viable `h264` variant;
- one `masterUrl` candidate; and
- two `backupUrls` candidates.

All 26 masters and 52 backups had MP4 magic. For every backup, the bounded
prefix, reported total size, and format matched its master. This is strong
bounded equivalence evidence, not a claim of full-byte equality.

### Motion policy

1. Resolve only the exact reviewed key `h264` to the closed class `avc`.
2. Record both the current codec ordinal and the closed codec class.
3. Require exactly one viable `h264` variant.
4. Select its non-empty `masterUrl`.
5. If the master field is absent, select the first non-empty backup in source
   order.
6. A non-empty unreviewed codec family or multiple viable `h264` variants is
   ambiguous and fails closed.
7. Re-reading checks that the recorded ordinal still points to `h264`; a
   reordered mapping invalidates the locator instead of silently selecting a
   different codec.

The backup rule covers a structurally absent master. It is not runtime network
failover and does not authorize a download retry policy.

## URL-Free Candidate Locator

The dormant module is:

```text
xhs_download/services/platforms/xhs_livephoto_candidate_locator.py
```

It defines frozen schema `1.0` types:

- `XhsLivePhotoMemberLocatorV1`
- `XhsLivePhotoItemCandidateMapV1`
- `XhsLivePhotoCandidateMapV1`
- `XhsPrivateMediaCandidateV1`

Supported source kinds are closed:

```text
url_default
info_list_url
master_url
backup_url
```

A locator stores source item ordinal, member role, source kind, bounded field
indexes, and the closed `avc` codec class where applicable. It never stores the
candidate value, hostname, extension, Provider, manifest, note identity, or
fingerprint.

`locate_xhs_livephoto_candidates()` cross-checks item count, source ordinals,
item kinds, and relationship candidate counts. `read_candidate_from_locator()`
returns the value only inside `XhsPrivateMediaCandidateV1`. The private value is
excluded from repr, comparison, hashing, and structural output. Fixed errors
contain only closed codes and safe paths.

```text
CANDIDATE_LOCATOR_IS_NOT_DOWNLOAD_AUTHORITY = YES
```

## Format Evidence

The review made 150 unique bounded candidate requests:

| Role | Probes | Magic | Declared type | Unknown | Invalid |
| --- | ---: | --- | --- | ---: | ---: |
| still (70 image-note candidates + 2 video-cover controls) | 72 | WebP | WebP image | 0 | 0 |
| motion | 78 | MP4 / ISO BMFF | MP4 video | 0 | 0 |

The probes ran in two bounded batches because one positive note did not expose
an exact current-note payload during the first request and was recovered by a
later identity-checked request. Their sanitized transport totals combine as:

| Signal | Result |
| --- | --- |
| HTTP status | 72 x `200`; 78 x `206` |
| response total observed | 150 / 150 |
| total source | 72 Content-Length; 78 Content-Range |
| first-byte latency buckets | 116 x 250-999 ms; 34 x 1-3 s |
| bounded body readable | 150 / 150 |
| server-observed wildcard CORS header | 150 / 150 |
| redirects | 0 |

The first batch retained 80 `lt_1_mib` size buckets. The follow-up batch used
the response totals to prove all still size relations and all master/backup
equivalence checks, but its 70 individual size buckets were not retained in
the sanitized aggregate. They are recorded as `not_retained`; they are not
guessed, and the candidates were not re-requested after this review. Exact
sizes are not committed.

Still servers ignored Range and returned status 200, so the client consumed
only the first bounded chunk and closed the response. Motion servers honored
the range and returned partial content.

The earlier PR #67 representative probe observed JPEG on a different reviewed
path. The current direct candidates returned WebP. This difference reinforces
the rule that format must come from each selected response, not from a global
XHS assumption or a synthetic JPEG conversion URL.

The implementation reuses `resolve_image_format()` for still evidence. A
research-only motion resolver recognizes reviewed ISO BMFF MP4, QuickTime MOV,
and WebM magic and rejects HTML, JSON, XML, SVG, unknown binary, and MIME-only
claims. URL suffix is never format proof, and magic wins over conflicting
declared MIME.

## Host, TLS, Range, And CORS Review

Observed host families were:

| Role | Reviewed family |
| --- | --- |
| still | exact `sns-webpic-qc.xhscdn.com` |
| motion master | strict numeric `sns-video-vN.xhscdn.com` |
| motion master alternate | exact `sns-video-zl.xhscdn.com` |
| motion backup | strict numeric `sns-bak-vN.xhscdn.com` |

The family labels above are research notation, not production glob rules.
Exact observed hosts resolved only to public addresses, validated TLS, used
default ports, contained no userinfo, and returned non-empty resource paths.
Lookalike and suffix-confusion hosts are rejected by the research classifier.

The payload candidates used the `http` scheme. Representative same-host HTTPS
probes succeeded for the still, numeric motion, and backup families and
returned a wildcard CORS response header. Raw payload values therefore require
an explicit later HTTPS-normalization and admission step before browser use.
The locator intentionally does not perform that step.

A real browser fetch was attempted, but the browser-control session timed out
during navigation before response/body evidence could be collected. Header
evidence from a server request is not equivalent to browser CORS proof.
Therefore:

```text
XHS_LIVEPHOTO_HOST_REVIEW = PARTIAL_BROWSER_CORS_NOT_PROVEN
BROWSER_DIRECT_DOWNLOAD_READY = NO
```

No host was added to a production allowlist.

## Negative Controls

### Ordinary gallery

All four items produce a still locator and no motion locator. LivePhoto false
positives remain zero.

### Ordinary video

The note-level video relationship is rejected by the candidate-map entry point
as an unsupported note kind. Its cover is not converted into a LivePhoto still
member and no LivePhoto locator is emitted.

### Invalid or deleted note

No exact target identity or current-note payload was admitted after three
bounded checks. No locator was created and no media candidate was probed.

## Readiness Decision

Candidate and per-selected-candidate format gates pass:

```text
XHS_LIVEPHOTO_CANDIDATE_SELECTION_READY = YES
XHS_LIVEPHOTO_STILL_FORMAT_READY = YES
XHS_LIVEPHOTO_MOTION_FORMAT_READY = YES
```

Asset Spec readiness remains closed because browser CORS/body readability and
the raw-HTTP-to-reviewed-HTTPS admission bridge are not proven:

```text
XHS_LIVEPHOTO_HOST_REVIEW_READY = NO
XHS_LIVEPHOTO_ASSET_SPEC_MAPPER_READY = NO
XHS_LIVEPHOTO_PROVIDER_INTEGRATION_READY = NO
```

A later, independent gate must prove browser behavior and define HTTPS
normalization/admission before constructing `XhsRichMediaAssetSpec`. Provider
integration would still require separate runtime, manifest, UI, and download
reviews after that.

## Runtime Impact

```text
CURRENT_XHS_CANDIDATE_LOCATOR_CONSUMERS = 0
CURRENT_XHS_PROVIDER_RICH_MEDIA_EMISSION = 0
XHS_ADAPTER_DIFF = 0
REGISTRY_DIFF = 0
PUBLIC_ROUTE_DIFF = 0
TEMPLATE_DIFF = 0
STATIC_DIFF = 0
DOWNLOAD_DIFF = 0
HOST_ALLOWLIST_DIFF = 0
SETTINGS_DIFF = 0
MIGRATION = NONE
DEPENDENCY_DIFF = 0
PRODUCTION_CHANGE = NO
```

The structured evidence artifact is:

```text
research/evidence/xhs_livephoto_candidate_format_host_v1.json
```
