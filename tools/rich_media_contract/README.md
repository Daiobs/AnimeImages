# Rich Media Domain Contract v1 Offline Tooling

This directory validates the proposed AnimeImages-owned rich-media domain
contract without importing Django, executing a parser, reading runtime settings
or using the network. It is research tooling, not a runtime package.

## Contract boundary

The document contains only structural data:

- a closed post-type enum;
- ordered image/video asset descriptors;
- Live Photo relationship groups; and
- inert rich-text or asset-reference blocks.

It contains no source/media location, host, path, query, header, Cookie,
credential, Provider payload, manifest, signature or download authorization.
All fixtures are fictional.

`schema_v1.json` is the machine-readable shape. `validate_contract.py` is the
authoritative standard-library semantic and security validator. It also checks
cross-reference, pairing, role/type, animation-format and post-shape rules that
cannot be expressed clearly by the shape alone.

## Validate fixtures

```bash
python3 tools/rich_media_contract/validate_contract.py \
  tools/rich_media_contract/fixtures/valid/live_photo.json
```

The output is restricted to one fixed status category, one fixed error code
and a structural path. Rejected input values are never echoed.

## Parser Lab mapper

`map_parsehub_result.py` maps an already-sanitized Parser Lab record into this
contract. It does not import or execute ParseHub. The mapper:

- preserves the sanitized media array order;
- maps paired image/video records to a `live_photo` group;
- maps heterogeneous media to ordered `multimedia` assets;
- maps rich-text presence to inert text/media blocks; and
- rejects cover records because a cover is presentation metadata, not a
  canonical downloadable asset.

The fixed text marker records only that sanitized text existed. It is not an
attempt to reconstruct supplier content.

## Tests

```bash
python3 -m unittest discover tools/rich_media_contract/tests
```

The tests are fully offline and use only the Python standard library.
