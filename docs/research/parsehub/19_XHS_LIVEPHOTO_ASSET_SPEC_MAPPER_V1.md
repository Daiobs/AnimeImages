# XHS LivePhoto Asset Spec Mapper v1

## Status

```text
RELATIONSHIP_MAPPER = MERGED
CANDIDATE_SELECTION = MERGED
VERIFIED_BROWSER_HOST_SUBSET = MERGED
FULL_HOST_COVERAGE = NO
ASSET_IDENTITY = IMPLEMENTED_IN_DRAFT
ASSET_SPEC_MAPPER = IMPLEMENTED_IN_DRAFT
RICH_MEDIA_CONTRACT_BRIDGE = COMPATIBILITY_PROVEN_NOT_WIRED
MEDIAASSET_BUILDER = NOT_STARTED
PROVIDER_INTEGRATION = NOT_STARTED
CURRENT_PROVIDER_EMISSION = 0
PUBLIC_SUPPORT = NO
PRODUCTION_DEPLOYMENT = NO
```

This phase consumes the browser-verified subset established by PR #71 and
builds a dormant, offline, URL-free Asset Spec map. It does not add a runtime
consumer. The current XHS adapter, Provider registry, Provider result,
manifest, UI, downloads, settings, database, and production runtime are
unchanged.

## Input Boundary

The mapper accepts only the already-selected exact-note artifacts:

- exact and equal requested and payload note identities;
- one `XhsLivePhotoRelationshipMapV1`;
- one `XhsLivePhotoCandidateMapV1`;
- the exact `imageList` snapshot used by those maps; and
- one private member proof for every selected still or motion locator.

It does not select a note from a page, consult `firstNoteId`, search for a URL,
pair arrays by position, or infer relationships from similar candidate text.
The relationship mapper remains the only source of item-local pairing evidence.
The source ordinal is used only after that relationship is proven, to preserve
result identity and physical order.

## Verified Host Subset

| Member | Host class | Mapper policy |
| --- | --- | --- |
| still | `still_exact` | allowed after browser and format proof |
| motion | `motion_versioned` | allowed after browser and format proof |
| motion | `motion_backup` | allowed after browser and format proof |
| motion | `motion_zl` | discovered, unverified, rejected |
| either | unknown or lookalike | rejected |

Discovery is not admission. Any item containing `motion_zl` causes the entire
mapping operation to fail with the fixed code
`XHS_LIVEPHOTO_ASSET_SPEC_HOST_UNVERIFIED`; the item is not silently omitted and
evidence from a sibling host is not borrowed.

## Private Member Proof

`XhsLivePhotoPrivateMemberProofV1` is frozen and binds four independent facts:

1. the selected member locator and role;
2. a browser-verified closed host class;
3. a magic-derived closed format class and extension; and
4. a SHA-256 binding over the exact normalized private HTTPS candidate value.

The candidate binding field is excluded from repr, comparison, hashing, and
structural output. It is never used in an asset ID, group ID, cache key, log,
error, Provider resource ID, or public object.

At mapping time the locator rereads the candidate from the supplied
`imageList`, reruns the reviewed candidate selection policy, normalizes the
candidate through browser admission, hashes the exact normalized value again,
and compares the two bindings in constant-time. Cross-item proofs, stale
snapshots, master-to-backup drift, and changed signed candidates fail with
`XHS_LIVEPHOTO_ASSET_SPEC_CANDIDATE_BINDING_MISMATCH`. Errors disclose only a
fixed code.

## Format Policy

Only bounded magic evidence authorizes an extension:

| Member | Magic class | Extension |
| --- | --- | --- |
| still | `jpeg` | `jpg` |
| still | `webp` | `webp` |
| motion | `mp4` | `mp4` |

MIME-only evidence, URL suffixes, host names, field names, unknown formats,
and historical representative observations cannot authorize an extension.

## Asset Identity

The AnimeImages-owned scope key is:

```text
first 24 lowercase hex characters of
SHA-256("xhs-livephoto-asset-spec-v1:" + exact payload note identity)
```

Asset IDs are:

```text
xhs:<scope_key>:i<source_item_ordinal>:still
xhs:<scope_key>:i<source_item_ordinal>:motion
```

Each LivePhoto group ID is:

```text
xhs:<scope_key>:i<source_item_ordinal>:live
```

No ID contains a raw note identity, URL, path, query, candidate binding, title,
author, or research fingerprint. The stability boundary is deliberately:

```text
ASSET_ID_SCOPE = RESULT_AND_ORDER_SCOPED
```

The same note identity, schema version, source ordinal, and member role produce
the same ID. A platform reorder changes ordinals and therefore IDs. These IDs
are not permanent public IDs, database keys, cache keys, Provider resource IDs,
or manifest identities.

## Output Model

The dormant module is:

```text
xhs_download/services/platforms/xhs_livephoto_asset_spec_mapper.py
```

It exposes frozen, URL-free structures:

- `XhsLivePhotoAssetBindingV1`, containing an `XhsRichMediaAssetSpec`, locator,
  source ordinal, closed host class, closed format class, and optional group ID;
- `XhsLivePhotoGroupSpecV1`, containing one still and one motion asset ID; and
- `XhsLivePhotoAssetSpecMapV1`, containing schema versions, post type, ordered
  assets, and groups.

The output intentionally contains no candidate value, binding, note identity,
hostname, title, author, Cookie, header, Provider, manifest, or request data.

## Order, Roles, And Groups

Source items retain their order. Physical expansion is per item:

```text
regular image -> still
LivePhoto     -> still, motion
```

Assets are not regrouped by media type, host, format, or identifier.

| Source shape | Post type | Roles |
| --- | --- | --- |
| one regular image | `image` | `primary` |
| multiple regular images | `gallery` | `gallery` |
| all LivePhoto items | `live_photo` | `still`, `motion` |
| regular and LivePhoto items | `multimedia` | `inline`, `still`, `motion` |

Every LivePhoto source item creates exactly one group. Its still immediately
precedes its motion. A regular image cannot enter a group, a group cannot miss
a member, one asset cannot belong to multiple groups, and groups cannot span
source items.

## Rich Media Compatibility

Tests convert the Asset Spec map into a completely synthetic, URL-free payload
and pass it to `normalize_xhs_rich_media_payload()`. The existing Rich Media
contract accepts the post type, physical asset order, IDs, extensions, roles,
and group membership without adjustment.

This bridge exists only in tests:

```text
RICH_MEDIA_CONTRACT_BRIDGE = COMPATIBILITY_PROVEN_NOT_WIRED
```

The mapper does not return `RichMediaContractV1`, construct `MediaAsset`, build
a Provider manifest, or emit `ParseResult.rich_media`.

## Verification Boundary

The focused suite is fully offline and uses synthetic values on reviewed host
families plus bounded synthetic magic prefixes. It covers deterministic and
opaque IDs, 14-LivePhoto and mixed 17-item shapes, exact physical order,
same-candidate binding, host and format mismatch rejection, group invariants,
sanitized output, Rich Media compatibility, and static proof that current
runtime consumers remain zero.

No test accesses XHS, a media CDN, ParseHub, or production. No real source URL,
candidate, note identity, candidate digest, Cookie, token, or response body is
stored in the repository.

## Remaining Gates

Before any product support, separate reviewed work is still required for:

- full host coverage or an explicit product policy for unverified classes;
- runtime `MediaAsset` construction and provenance;
- Provider result and manifest authorization;
- current XHS adapter integration;
- registry and policy behavior;
- staff diagnostics and public disclosure boundaries;
- Browser transport and download behavior; and
- production acceptance.

None of those gates is started by this Draft implementation.
