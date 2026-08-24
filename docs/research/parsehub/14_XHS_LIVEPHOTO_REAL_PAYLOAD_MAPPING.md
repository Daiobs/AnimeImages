# XHS LivePhoto Real Payload Mapping Research

## Status

```text
XHS_LIVEPHOTO_REAL_PAYLOAD_MAPPING = BLOCKED
XHS_LIVEPHOTO_REAL_MAPPING_BLOCKED_PAIR_EVIDENCE = OPEN
XHS_LIVEPHOTO_REAL_MAPPING_BLOCKED_FORMAT_EVIDENCE = OPEN
XHS_PROVIDER_WIRING = NOT_STARTED
XHS_MEDIA_HOST_REVIEW = REQUIRED
XHS_PUBLIC_SUPPORT = NO
REGISTERED = NO
DEPLOYED = NO
```

This research does not add a real-payload mapper. The evidence available in
this review establishes a credible field-path hypothesis, but it does not prove
that the current public XHS page payload still exposes that shape for the exact
requested note. Inventing a "real-shape" fixture from old source code would
turn an open runtime question into a false implementation claim.

## Scope And Safety

The review was limited to:

- the current AnimeImages XHS HTML and initial-state reader;
- the current requested-note identity and recommendation-suppression gates;
- the dormant URL-free XHS rich-media normalizer;
- static field-path evidence from `z-mio/ParseHub` at immutable commit
  `144260f4349c5bb57415f7555a572fcd48c3b8ba`;
- one historical public mixed LivePhoto report; and
- bounded attempts to obtain a current public page without credentials.

No Cookie, account credential, raw page payload, complete source URL, XHS
token, signed media URL, or user identity is stored in this repository. No
Provider request path, registry entry, host allowlist, manifest, download path,
UI, setting, model, migration, or dependency changes in this research.

## Existing AnimeImages Runtime Evidence

The current parser provides the stronger source-identity boundary and must
remain authoritative:

1. `extract_xhs_note_id()` derives a 24-character requested note ID only from
   reviewed note URL forms.
2. `_select_current_note()` accepts a structured candidate only when its note
   identity exactly equals the requested note identity.
3. Feed and recommendation candidates receive no authority when a requested
   note identity is available.
4. A missing exact target fails closed into the existing fallback policy; the
   parser does not adopt `firstNoteId` as an unconditional target.
5. Terminal current-note states precede generic media fallback.

The current `XhsPageState` is intentionally flattened. It retains ordered image
URLs and note-level video candidates, but it does not retain the selected raw
note mapping, individual `imageList` item records, an item-local LivePhoto flag,
or an item-local motion stream. Consequently, the current adapter cannot prove
a still/motion relationship after `extract_xhs_page_state()` returns.

This is a data-preservation gap, not permission to pair the flattened image and
video collections by index.

## Pinned Static Field-Path Evidence

The pinned ParseHub source reads:

```text
note.imageList[]
  livePhoto
  urlDefault
  stream.<codec-family>[0].masterUrl
```

Its parser treats one image-list item as LivePhoto only when that same item has
the LivePhoto marker and an item-local selected motion stream. The still comes
from that item's image field and the motion member comes from that item's
stream. This is valid static evidence for the relationship shape.

The same upstream implementation also:

- chooses `note.firstNoteId` without AnimeImages' exact requested-note proof;
- performs per-image network metadata requests to infer extensions; and
- coerces an unknown image extension to JPEG.

Those behaviors are explicitly not adopted. Static source evidence cannot
prove that the current XHS web payload still contains the same fields, that the
selected payload belongs to the requested note, or that every supported image
has a Contract v1 extension.

## Live Sample Result

```text
QUALIFIED_CURRENT_LIVEPHOTO_SAMPLES = 0
QUALIFIED_CURRENT_GALLERY_SAMPLES = 0
QUALIFIED_CURRENT_VIDEO_SAMPLES = 0
QUALIFIED_CURRENT_INVALID_SAMPLES = 0
```

The bounded public search attempt reached the current XHS site but search
results required login. No login, QR scan, Cookie reuse, or account material
was introduced. A historical public mixed-LivePhoto short link no longer
resolved to an identity-bearing note page, so it was not admitted as current
payload evidence.

The historical mixed-content report remains useful only as a negative design
lesson: separate compacted image and motion arrays can silently associate the
wrong motion member when a normal image appears between LivePhoto items. It
does not satisfy the current real-payload gate.

## Pairing Decision

The future mapper may create a pair only after all of the following are proven
from one current target-note payload:

1. requested note ID equals payload note ID;
2. one ordered item explicitly owns both the still field and motion stream;
3. the LivePhoto marker applies to that same item;
4. neither member comes from a recommendation or adjacent note; and
5. both members survive format and host review independently.

Array adjacency, parallel-list index, filename similarity, URL path similarity,
matching dimensions, or matching order are not pair evidence.

After the relationship itself is proven, an AnimeImages builder may generate
URL-free identities such as a note-scoped ordered item identity, plus explicit
`still` and `motion` member suffixes. The ordinal may name an already-proven
same-item relationship; it may never be used to infer that relationship.

Because no qualifying current payload was available, the item-local
relationship and its stable naming inputs remain unverified. The mapper is
therefore blocked rather than implemented from a fictional approximation.

## Format Boundary

The still-image URL fields shown by the pinned implementation do not guarantee
a reliable filename extension. ParseHub's HEAD request plus unknown-to-JPEG
fallback is incompatible with the AnimeImages fail-closed contract.

A future implementation must prove one of these before creating
`XhsRichMediaAssetSpec`:

- a trusted item-local format field in the current payload; or
- a separately reviewed, bounded Provider-builder content-signature step.

Unknown formats remain rejected. HEIC remains deferred by Contract v1. No HEAD
or GET request is added by this research.

## Host Evidence

Only hostname families are recorded. No media path, query, token, or user data
is retained.

| Media role | Evidence age | Hostname family | Status |
| --- | --- | --- | --- |
| Still preview/content | Historical public report | `sns-webpic-qc.xhscdn.com` | `HOST_REVIEW_REQUIRED` |
| Motion stream | Historical public report | `sns-video-hs.xhscdn.com` | `HOST_REVIEW_REQUIRED` |
| Current still | No qualifying sample | Unknown | `HOST_REVIEW_REQUIRED` |
| Current motion | No qualifying sample | Unknown | `HOST_REVIEW_REQUIRED` |

Redirect behavior and browser CORS/readability were not measured because no
qualifying current sample passed identity admission. The existing broad XHS
media-host suffix is not changed or treated as approval for a future LivePhoto
builder.

## Next Evidence Protocol

The next review needs at most three public LivePhoto notes plus one gallery,
one video, and one invalid/deleted control. For each sample it must:

1. resolve the share URL only within the current XHS page boundary;
2. redact the complete URL and all token values before any durable output;
3. select the target note using requested/payload identity equality, never
   `firstNoteId`;
4. emit only field names, booleans, item counts, codec-family names, extension
   evidence, and hostnames;
5. verify that every LivePhoto still and motion member is present in one item;
6. prove that recommendation-note media is excluded;
7. record redirect and CORS outcomes using fixed statuses; and
8. construct a fictional sanitized fixture only after the real field shape is
   independently confirmed.

If that evidence passes, the mapper can be added as AnimeImages-owned,
network-free code that produces sanitized identity evidence, ordered asset
specs, relationship evidence, and `RichMediaContractV1`. Final `MediaAsset`,
manifest, token, Provider registration, UI, and downloads remain later PRs.

## Verification And Unchanged Runtime

This branch intentionally adds no mapper test because there is no implemented
mapper and no qualifying real-shape fixture. Existing XHS normalizer and media
fidelity suites remain the regression authority for the dormant contract and
current parser behavior.

```text
CURRENT_XHS_PROVIDER_RICH_MEDIA_EMISSION = 0
CURRENT_XHS_PROVIDER_IMPORTS_DORMANT_NORMALIZER = NO
REGISTRY_DIFF = 0
MIGRATION = NONE
DEPENDENCY_DIFF = 0
PUBLIC_UI_DOWNLOAD_DIFF = 0
```

## Exit Status

```text
XHS_LIVEPHOTO_REAL_PAYLOAD_MAPPING_RESEARCH_BLOCKED
XHS_LIVEPHOTO_REAL_MAPPING_BLOCKED_PAIR_EVIDENCE
```
