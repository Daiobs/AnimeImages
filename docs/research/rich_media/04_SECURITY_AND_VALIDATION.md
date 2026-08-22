# Rich Media Security and Validation

## Trust boundary

A parser result is untrusted until AnimeImages validation succeeds. A rich-media
document is semantic metadata only. It must not carry locations, request
context, Provider payloads or delivery authority.

The offline validator runs security rejection before structural interpretation.
This prevents an unknown field from hiding a location, token or executable
payload behind an otherwise plausible document.

## Forbidden data

The contract rejects fields or text that represent:

- source/media URLs, URIs, links, paths or queries;
- headers, Cookies or authorization values;
- tokens, credentials, passwords, signatures or manifests;
- raw Provider payloads or errors;
- HTML, script, style or Markdown link/image content; and
- executable or network schemes in text.

The contract also has no hostname field. Host admission belongs to the existing
runtime asset and Provider layers, not this semantic model.

## Fixed result categories

The validator returns exactly one category:

- `VALID`
- `INVALID_SCHEMA`
- `INVALID_ASSET`
- `INVALID_GROUP`
- `INVALID_BLOCK`
- `SECURITY_REJECTED`

Output includes only a fixed error code and a structural path. It never echoes
an input value, unknown field name, source text, location, token, exception or
traceback. CLI input filenames are not printed.

## Fail-closed validation order

1. Apply document node/depth limits.
2. Reject forbidden key classes and unsafe text.
3. Require the exact top-level field set and schema version.
4. Validate closed post, physical type, role and extension enums.
5. Validate bounded collections and unique identifiers.
6. Resolve all group members to canonical assets.
7. Enforce exact still/motion Live Photo pairing.
8. Resolve all rich-text media references.
9. Enforce post-shape and complete rich-text references.

No correction, best-effort coercion or unknown-field retention is allowed.

## Identifier rules

`case_id` is a bounded lowercase offline identifier. Asset/group IDs are
bounded colon-separated domain IDs. They are document-local opaque references,
not source post IDs, cache keys, Provider IDs or manifest identities.

Duplicates fail closed. An asset can belong to at most one Live Photo group.

## Live Photo safety

The validator requires one admitted semantic image member and one admitted
semantic video member. It rejects two stills, two motions, missing members,
unknown references, duplicate members and cross-group member reuse.

This validates structure only. Runtime code must still independently admit both
under current download rules.

`RICH_MEDIA_RELATION_IS_NOT_DOWNLOAD_AUTHORITY`

## Rich-text safety

Text is inert plain text. A future renderer must use normal template escaping
and must not reinterpret it as HTML or Markdown. Media blocks reference only
canonical IDs; they cannot embed source or media locations.

Every rich-text asset must be referenced, preventing hidden orphan assets from
entering a document outside presentation order. Live Photo members must appear
together in one media block.

## Cover safety

A cover field in a supplier result is not mapped into `assets`. Silent cover
promotion could bypass canonical source order and download admission. The
offline mapper rejects it for explicit review.

## Offline tooling boundary

`tools/rich_media_contract/`:

- uses only the Python standard library;
- reads explicit local JSON inputs only;
- does not import Django or ParseHub;
- does not read environment variables or a database;
- does not execute parsers, Providers or subprocesses;
- does not use the network; and
- does not write into application state.

The optional mapper imports only AnimeImages' existing sanitized Parser Lab
validator. It does not import the supplier package or run supplier code.

## Fixture coverage

The fictional suite covers legacy-shaped image/video, gallery, image/video
animation, valid Live Photo, multimedia, text-only rich text, rich text with
media, rich text with Live Photo, missing references, duplicate IDs, malformed
pairing, cross-group reuse, raw HTML, forbidden location/token fields,
oversized text, unknown post/relationship values and shape conflicts.

No fixture is evidence of live platform support.
