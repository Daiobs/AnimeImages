# Offline Parser Lab Contract v1

## Purpose

Parser Lab v1 compares two already-generated, sanitized normalized result files.
It does not run AnimeImages or supplier parsers and cannot establish live
reliability. Its purpose is to make result-shape gaps reviewable before any
runtime integration.

Implementation:

- `tools/parsehub_lab/schema_v1.json`
- `tools/parsehub_lab/compare_results.py`
- `tools/parsehub_lab/fixtures/`
- `tools/parsehub_lab/tests/`

## Input

Each input contains schema/case/source identifiers, platform and content type,
presence flags for title/description/author, ordered media records, elapsed and
outcome classes, credential/network evidence and sanitized notes.

Media records contain only normalized structural fields: index, media type,
role, pairing group, extension, dimensions, duration, size and estimate flag,
quality label, bare hostname, thumbnail presence and video-pair presence.

The schema deliberately excludes source URLs, media URLs, URL paths/queries,
Cookies, authorization, tokens, passwords, phone numbers, Provider payloads,
headers and raw exceptions.

## Redaction gate

The comparator validates an exact allowlist of keys and fails closed on unknown
fields or invalid field types. Text containing a full HTTP(S) URL, query-like
assignment, credential assignment, Basic/Bearer material, JWT, opaque token or
phone-like value is rejected. Hostnames are lowercased, syntax checked and must
not be IP addresses.

Rejected input produces only:

- classification `INVALID_INPUT`;
- a fixed case identifier;
- a fixed safe summary; and
- an empty difference list.

Rejected values and unknown key names are never echoed. Malformed enum values
such as arrays or objects return the same fixed result instead of a traceback.

## Comparison classes

- `MATCH`: sanitized structures match.
- `ANIMEIMAGES_RICHER`: AnimeImages has additional normalized fields/media.
- `SUPPLIER_RICHER`: supplier has additional normalized fields/media.
- `STRUCTURAL_MISMATCH`: platform, content, ordering, pairing or authoritative
  non-empty values conflict.
- `SECURITY_REVIEW_REQUIRED`: credentials are required or admitted media hosts
  differ.
- `INVALID_INPUT`: schema or redaction validation failed.

The comparator checks platform/content/outcome/error class, media count and
order, type/role/pairing, title/description/author presence, dimensions,
duration, size, extension, host, thumbnail, LivePhoto pairing and rich-media
shape. It reports only allowlisted normalized values.

## Fixture policy

All fixtures are fictional and use reserved non-routable hostnames. The suite
covers single video, gallery, LivePhoto, multimedia, rich text, structural
mismatch and malicious/sensitive rejection. No fixture is evidence of upstream
runtime success.

## Explicit non-capabilities

Parser Lab v1 does not import ParseHub, import Django, call a Provider, access a
database, read environment variables, fetch a URL, download media, compare raw
payloads or decide public rollout. A future execution lab would require a new
security design and is outside this contract.
