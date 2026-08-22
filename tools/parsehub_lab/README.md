# ParseHub Offline Parser Lab

This directory contains an offline, supplier-neutral result comparator. It does
not import AnimeImages, Django, ParseHub, or any platform parser. It accepts two
already-sanitized JSON records and compares their normalized capabilities.

## Safety boundary

- No network access.
- No environment-variable or database access.
- No parser or Provider execution.
- No source URL, media URL, query string, Cookie, authorization value, Basic or
  Bearer credential, JWT, opaque token, password, phone number, Provider
  payload, or raw exception text.
- A hostname may be retained only as a bare normalized hostname.
- Unknown keys and malformed records fail closed.

## Usage

```bash
python tools/parsehub_lab/compare_results.py \
  tools/parsehub_lab/fixtures/single_video/animeimages.json \
  tools/parsehub_lab/fixtures/single_video/supplier.json \
  --output /tmp/comparison.json
```

The command prints a short human-readable summary. `--output` writes the same
safe comparison as JSON. Invalid or sensitive input returns exit code 2 and
does not echo rejected values.

## Tests

```bash
python -m unittest discover tools/parsehub_lab/tests
```

All fixtures are fictional. The supplier commit values are identifiers for the
fixture contract, not claims that upstream code was executed.
