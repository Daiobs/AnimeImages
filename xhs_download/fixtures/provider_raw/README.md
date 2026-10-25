# Provider raw fixtures

Store complete, sanitized upstream observations at `<provider>/<case>.json`.
These are adapter inputs, not another result model. Do not store ParseResult,
MediaAsset, candidates, manifests, or delivery decisions here. P2 adds no real
recordings. Never label synthetic or reduced historical data as a real capture.

The envelope has exactly these fields (example only, not a recorded response):

```json
{
  "schema_version": 1,
  "provider": "synthetic_provider",
  "platform": "synthetic",
  "source_url": "https://source.example/work/1",
  "provenance": {
    "kind": "synthetic",
    "raw_kind": "json",
    "complete": true
  },
  "raw": {"url": "https://cdn.example/media.mp4?sig=REDACTED"}
}
```

`kind` is `recorded` or `synthetic`; `raw_kind` is `json` or `html`. HTML is a
JSON string in `raw`. JSON raw may be any JSON value. Keep upstream keys,
containers, ordering, missing fields, non-secret values, and unknown facts.
`complete` must be exactly `true`: do not use a truncated census/raw_shape
export or claim that removed facts can be reconstructed. Sensitive scalar
values become `REDACTED`; dictionaries and arrays are not collapsed.

## Local tools

Use the existing Python environment; the module uses only the standard library
and never calls upstreams, loads application settings, or downloads resources.
Input is an envelope JSON file, not a URL or a bare HTML file.

```sh
python -B -m xhs_download.services.provider_adapters.recordings sanitize /private/input.json --output /private/clean.json
python -B -m xhs_download.services.provider_adapters.recordings check /private/clean.json
python -B -m unittest xhs_download.tests_provider_adapter_recordings
```

`sanitize` validates and sanitizes before writing the output; it does not change
the input unless the same output path was explicitly supplied. `check` accepts
one or more files and never writes. Stdout contains only an OK summary; failures
exit nonzero with a constant diagnostic code on stderr, without input values,
filenames, or parser messages. No original payload is printed.

Both operations reject duplicate JSON keys, non-finite numbers (including float
overflow), non-JSON objects, incomplete envelopes, and malformed input. Nothing
is silently truncated; no object properties or custom iterators are evaluated.

Sensitive field values, Cookie/Set-Cookie, named cookie values, tokens, signatures,
userinfo, query parameters (including repeated/encoded keys and nested URLs),
and sensitive fragments are sanitized. Header and structured Cookie names remain.
URLs embedded in JSON strings or HTML are handled too. URL host/path identity is
retained; credentials detected there cause rejection, not a rewritten resource.
Known secret shapes include JWT, private keys, Basic/Bearer credentials, and
common prefixed API keys. This is not proof that arbitrary unlabelled strings
cannot be secrets: review the sanitized diff and provenance before staging.

JSON strings and JSON object/array script bodies are parsed as strict JSON before
redaction, rejecting duplicate keys, NaN, Infinity, and numeric overflow. Embedded
double-quoted values use JSON escape rules. Header arrays of `[name, value]` pairs retain their names,
order, duplicates, and array structure; only sensitive values are masked.
Nested URL values are decoded and re-encoded one layer at a time, retaining
encoded path separators. Canonically entity-escaped attribute values keep their
HTML entity layer rather than inserting literal quotes into the attribute.

Unsupported forms are rejected, not partially sanitized: quoted or backslash-
escaped Cookie/Set-Cookie text, non-JSON or malformed quoted values, and escaped
single-quoted values return `UNSUPPORTED_QUOTING`. Encoding layers that need
redaction but cannot round-trip exactly return `UNSUPPORTED_ENCODING`; malformed
header pair arrays return `UNSUPPORTED_HEADER_SHAPE`. These constant errors never
include the original text. This tool is not a general JavaScript or HTML parser.

## Before committing

Run `check` on the sanitized files and inspect the staged diff. The ordinary
`unittest.TestCase` directory gate is discovered by `python manage.py test` in
the existing CI workflow: every `provider_raw/**/*.json` is strictly loaded,
checked independently for unredacted fields/secret shapes, and required to live
one directory below this folder with a matching `provider`. The checker does
not merely compare a second sanitizer pass. No CI or pre-commit configuration
changes are needed. Do not commit original inputs, credentials, or private
capture sidecars; retain real-capture provenance separately through the existing
evidence process.
