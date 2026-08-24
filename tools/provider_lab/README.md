# AnimeImages Provider Lab v1

Provider Lab is an offline, AnimeImages-owned fixture surface for reviewing
source URL lifecycle and normalized rich-media contracts. It does not load or
execute ParseHub, resolve redirects, call Providers, or read production
configuration.

Run commands from the repository root:

```bash
python -m tools.provider_lab inventory
python -m tools.provider_lab validate-source-fixture
python -m tools.provider_lab redact-source --help
python -m tools.provider_lab compare-provider-capability source_url_parameter_lifecycle
python -m tools.provider_lab validate-rich-media tools/rich_media_contract/fixtures/valid/live_photo.json
python -m tools.provider_lab summarize-delta
```

The `redact-source` command accepts a full URL only as an input to the local
process. Its output is a closed structural diagnostic: platform, input kind,
hostname, path class, canonical identity kind, duration, and parameter names.
It never emits the URL, canonical identity value, or query values.

Source fixtures use the closed `parse_only`, `identity`, `safe_passthrough`,
and `drop` action vocabulary. The lifecycle keeps `input_url`, `parse_url`,
the resolver's unmodified `resolved_url`, and cleaned `post_parse_url` as four
separate internal stages. None of those URL-bearing objects is a diagnostic.

Fixtures under `fixtures/` are fictional and use reserved example domains.
Failures return fixed codes without echoing rejected input.
