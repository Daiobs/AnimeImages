# Dormant XHS Native Rich-Media Normalization

## Status

`ADOPT_DORMANT`

`XHS_HEIC_CONTRACT_GAP = DEFERRED`

The normalizer is not imported by the current XHS adapter or Provider registry,
and no current Provider emits `RichMediaContractV1` through this module. This
Draft PR has not been deployed.

## Provenance And Static Evidence

The review used `z-mio/ParseHub` v2.2.0 at immutable commit
`144260f4349c5bb57415f7555a572fcd48c3b8ba` under its MIT license. The relevant
files were inspected statically:

- `src/parsehub/provider_api/xhs.py`;
- `src/parsehub/parsers/parser/xhs.py`.

ParseHub's XHS payload handling identifies a Live Photo when one image-list
item contains both its still-image field and an item-local motion stream. That
is useful relationship-shape evidence. ParseHub also selects the first note in
initial state, issues per-image HEAD requests for extensions, and converts an
unknown extension to JPEG. Those broader behaviors are not adopted.

ParseHub is not installed, imported, or executed. The implementation in this
PR is an AnimeImages-owned, network-free normalizer over a new sanitized input
contract.

## Existing AnimeImages Authority

Current AnimeImages XHS parsing remains authoritative and already provides
stronger production boundaries:

- requested-note identity and payload-note matching;
- generic-page and recommendation false-positive suppression;
- ordered current-note media extraction;
- bounded preview admission and raster signature validation;
- Browser Photo Archive and Browser ZIP;
- Production-tested CORS and zero-relay behavior.

This PR does not replace, call, or modify the existing XHS adapter. It adds no
network request, media host, fallback, Provider registration, manifest field,
or download path.

## Sanitized Input Contract

An absent payload returns no assets and no rich-media contract. A present
payload must contain exactly:

- `requested_note_id`;
- `payload_note_id`;
- ordered `items`.

The two note IDs must match exactly before any item is normalized. Regular
items contain only a media kind, domain asset ID, and extension. They cannot
contain URLs, covers, headers, tokens, or Provider data.

A Live Photo item must contain all of the following in one record:

- a stable domain `pair_id`;
- an `owner_note_id` equal to the exact payload note ID;
- one explicit still member;
- one explicit motion member.

The normalizer never creates a pair from adjacent array entries, equal indexes,
similar names, URL paths, or media order. Missing members, duplicate members,
duplicate pair IDs, duplicate asset IDs, and ownership mismatches fail with
fixed URL-free error codes.

The production XHS adapter does not yet supply this sanitized schema. A future
adapter change would need separate payload-field evidence, host review, and
runtime acceptance before this dormant module could be wired.

## Ordered Output

Each regular item contributes one `MediaAsset`. Each Live Photo contributes its
still followed by its motion at the same ordered item position. The output is:

- ordered `MediaAsset` candidates with empty URLs and no Provider authority;
- an optional existing `RichMediaContractV1`;
- explicit still/motion descriptors and `live_photo` groups when pair evidence
  is complete.

Single images, galleries, videos, Live Photos, multiple Live Photos, and mixed
image/Live Photo posts map to existing Contract v1 post types. No cover is
promoted and no relationship grants download authority. The contract is
structural metadata only.

## Format Boundary

Image and video extensions must already belong to the closed Rich Media
Contract v1 sets. Unknown binary data is rejected instead of being labeled
JPEG. HEIC is also rejected with the fixed `XHS_HEIC_CONTRACT_GAP` code because
Contract v1 does not admit HEIC. This batch does not expand the Contract v1
enum, perform a media HEAD/GET, trust a declared MIME, or transcode content.

## Verification Matrix

Fictional URL-free fixtures and offline tests cover:

- single image, gallery, and video;
- one and multiple explicit Live Photo pairs;
- mixed regular images and Live Photo;
- exact identity and owner mismatch;
- missing still or motion;
- duplicate member, pair, or asset identity;
- unknown extension and deferred HEIC;
- generic-page recommendation rejection;
- flattened order and still-before-motion order;
- no cover promotion;
- relationship metadata without download authority;
- existing `ParseResult`/`RichMediaContractV1` compatibility;
- bounded item count, input immutability, and dormant registry status.

Automated tests perform no XHS request and use no real user or signed media URL.

## Non-Goals

- XHS URL extraction, HTTP, host admission, or note resolution.
- Existing XHS parser or fallback changes.
- Rich Media Contract v1 enum changes.
- HEIC conversion or extension probing.
- Provider registration, runtime emission, download, view, UI, or deployment.
