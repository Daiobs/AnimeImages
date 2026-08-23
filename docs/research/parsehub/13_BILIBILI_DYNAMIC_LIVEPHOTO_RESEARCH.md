# Bilibili Dynamic And Live Photo Native Research

## Status

```text
STATIC_RESEARCH = COMPLETE
DORMANT_INTERMEDIATE_NORMALIZER = READY_FOR_REVIEW
REGISTERED = NO
PUBLIC = NO
DEPLOYED = NO

BILIBILI_INTERMEDIATE_CONTRACT = READY_FOR_REVIEW
BILIBILI_REAL_PAYLOAD_MAPPING = NOT_STARTED
BILIBILI_PROVIDER_WIRING = NOT_STARTED
BILIBILI_MEDIA_HOST_REVIEW = NOT_STARTED
BILIBILI_PUBLIC_SUPPORT = NO
BILIBILI_DYNAMIC_BLOCKED_IDENTITY_MAPPING = OPEN
BILIBILI_DYNAMIC_LIVEPHOTO_BLOCKED_PAIR_EVIDENCE = OPEN
RICH_TEXT_PRESENTATION = DEFERRED
```

The open blocks apply to activation, not to the URL-free structural contract in
this Draft PR. The current Bilibili video adapter remains the only registered
Bilibili runtime path.

## Provenance

The review used `z-mio/ParseHub` v2.2.0 at immutable commit
`144260f4349c5bb57415f7555a572fcd48c3b8ba`, under its MIT license. The static
review covered:

- `src/parsehub/parsers/parser/bilibili.py`;
- `src/parsehub/provider_api/bilibili.py`;
- the pinned README references.

ParseHub was not installed, imported, or executed by AnimeImages. No source was
copied. The classifier and normalizer are independently written, network-free
AnimeImages code using the existing Source Lifecycle and Rich Media contracts.

## Existing AnimeImages Authority

AnimeImages already owns and retains its Bilibili video path:

- BV/AV and `p` page selection;
- bounded page and API requests;
- `view` and `playurl` handling;
- ordered DURL and DASH candidates;
- size metadata, request headers, and existing media-host admission;
- Provider manifests, download routing, safe errors, and deadline propagation.

This PR does not import its dormant module from the adapter or registry. It does
not alter the current video URL matcher, page resolver, API requests, Provider
policy, host suffixes, media candidates, downloads, or user-visible output.

## Source URL Forms

The offline classifier distinguishes the following without resolving a URL:

| Form | Classification | Identity decision |
| --- | --- | --- |
| `/video/BV...` or `/video/av...` | existing video | current adapter remains authoritative |
| `t.bilibili.com/<18-19 digits>` | Dynamic | `bilibili_dynamic:<id>` |
| `bilibili.com/opus/<18-19 digits>` | Opus | `bilibili_opus:<id>` |
| `*.bilibili.com/<18-19 digits>` | numeric unverified | no identity; resolution required |
| `b23.tv/...` | short link | unresolved; no identity is guessed |
| share text containing one of the above | share text plus target shape | target identity only when direct numeric evidence exists |
| other Bilibili page | unknown | no video or Dynamic identity is guessed |

`bili2233.cn` is recorded as a ParseHub-discovered, unverified short-link host.
This PR does not add it to the current production page-host suffixes.

## Identity Decision

Dynamic and Opus use separate identity namespaces. The pinned source extracts
the numeric identifier found in a URL and sends it to the Dynamic detail API,
but it does not prove that a Dynamic ID and an Opus ID are interchangeable,
one-to-one, or algorithmically convertible.

The dormant normalizer therefore requires both a typed requested identity and a
typed payload identity. Kind and numeric value must match exactly. Even equal
digits fail closed when one side is `bilibili_dynamic` and the other is
`bilibili_opus`.

No cross-form canonicalization is implemented. A generic numeric root path is
not sufficient evidence of either namespace: it is classified
`numeric_unverified`, has no identity, requires resolution, and is not routed
or registered. Proving a stable mapping remains
`BILIBILI_DYNAMIC_BLOCKED_IDENTITY_MAPPING`.

## Payload And Pair Evidence

The pinned ParseHub source reads an ordered Opus `pics` collection. A single
picture item may contain both its image URL and `live_url`; ParseHub turns that
same-item combination into a still/motion object. This is sufficient evidence
for a dormant same-item relationship shape.

The pinned source does not establish all activation requirements:

- it does not validate a returned payload identity against the requested ID;
- it does not expose a stable pair ID or owner identity to its result model;
- it does not prove a closed image, motion-video, or cover hostname set;
- it optionally accepts Cookie input and reports risk-control behavior.

The AnimeImages normalizer consequently accepts only a new sanitized schema in
which a future AnimeImages Provider Builder has already supplied exact payload
identity, pair ID, owner identity, media kind, domain asset ID, and closed
Contract v1 extension. Requested/payload identity, pair ID, and owner identity
are AnimeImages adapter-required evidence, not claimed upstream-native fields.
No current adapter supplies that schema. Future runtime wiring must first prove
and construct those fields from an approved real payload mapping.

## Dormant Normalization Contract

The payload contains exactly:

- `requested_identity` and `payload_identity`;
- `target_state`;
- safe title and author candidates;
- ordered `items`.

Terminal `deleted`, `private`, and `not_found` states fail before media is read,
so recommendation items cannot replace the target. An available target must
have at least one bounded item.

Regular image and video items contain only kind, domain asset ID, and extension.
A Live Photo item must contain an explicit pair ID, an owner identity equal to
the exact payload identity, one still member, and one motion member. These
pair/owner values are future AnimeImages adapter evidence, not names asserted to
exist in ParseHub or Bilibili payloads. Missing, duplicate, or mismatched
evidence fails with a fixed URL-free error code.

The normalizer never infers relationships from adjacency, array index, filename,
URL path, dimensions, or matching numeric fragments.

## Content Mapping And Ordering

The mapping uses the existing `RichMediaContractV1`:

- one image -> `image`;
- multiple still images -> `gallery`;
- video-only media -> `video`;
- one or more complete pair-only items -> `live_photo`;
- images plus a pair or video -> `multimedia`.

Each regular item stays at its payload position. Each Live Photo item contributes
its still followed immediately by its motion at that position. Intermediate
`BilibiliDynamicAssetSpec` values are never regrouped by media type and contain
only asset ID, media type, extension, and structural role.

Title and author are bounded metadata candidates only. Dynamic text does not
create a Rich Text block, so `RICH_TEXT_PRESENTATION = DEFERRED`. No candidate is
promoted to cover. URL-free relationship metadata grants no Provider manifest
or download authority.

A future Provider Builder, not this dormant normalizer, owns URL admission,
host review, format verification, size, Provider provenance, manifest identity,
and final `MediaAsset` construction. Intermediate specs contain no URL, preview,
cover, headers, Provider, manifest, or download method.

## Host Matrix

The machine-readable matrix is
`docs/research/parsehub/bilibili_dynamic_host_matrix.json`.

- page and API hosts directly present in source are `HOST_DISCOVERED`;
- the additional ParseHub short-link host is `HOST_UNVERIFIED`;
- image, motion-video, and cover hosts are `HOST_REVIEW_REQUIRED` because the
  pinned code proves no closed set;
- no host is marked admitted.

No production allowlist, media suffix, redirect rule, or CORS policy changes in
this PR.

## Live Evidence

No live network request was performed. The task had no repository-owned public
sample that could prove identity, payload shape, and media host without Cookie,
signature, or user material. Live activation evidence remains a separate,
bounded staff-only review.

## Verification

Fictional source and URL-free payload fixtures cover:

- video, Dynamic, Opus, numeric-unverified, short-link, share-text, and unknown
  source shapes;
- distinct Dynamic and Opus identities, including equal-digit mismatch;
- one image, gallery, explicit Live Photo, and ordered multimedia;
- title/author candidates without Rich Text blocks;
- requested/payload/owner mismatch;
- terminal and empty-target false-positive rejection;
- missing members, duplicate members, pair IDs, and asset IDs;
- unknown extension, unsafe metadata, and unexpected URL/cover/text fields;
- exact intermediate-spec fields with no URL, cover, headers, Provider,
  manifest, download method, or registered runtime import;
- host statuses with no admitted Dynamic media host.

Tests use fictional IDs and make no Provider or public-network request.

## Non-Goals

- Dynamic or Opus public/staff routing.
- Replacing or extending the current Bilibili video adapter.
- Resolving short links or converting Dynamic IDs to Opus IDs.
- API clients, Cookie support, WBI/signing, anti-abuse bypass, or live probing.
- Host admission, Provider registration, Runtime Settings, admin/UI, manifests,
  downloads, Batch behavior, Browser Streaming, or deployment.

## Next Human Review Required

Before any activation proposal, reviewers must require exact requested and
returned identity evidence, stable pair/owner fields, closed media-host evidence,
terminal-state fixtures, bounded live proof, and a separate security/runtime
acceptance plan. The dormant contract must not be treated as proof that those
upstream fields are currently available.
