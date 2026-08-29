# ParseHub XHS image provider

This helper owns the optional ParseHub runtime boundary for XHS image parsing.
It installs the exact upstream commit into a separate virtual environment and
runs one anonymous, parse-only request per subprocess. It never imports
ParseHub into Django, uses cookies or proxies, or invokes ParseHub's downloader.

```bash
python tools/parsehub_provider/bootstrap_parsehub_provider.py /path/to/venv
python tools/parsehub_provider/bootstrap_parsehub_provider.py \
  --check-only /path/to/venv
```

Configure Django with the isolated interpreter path, not the venv directory:

```text
PARSEHUB_PROVIDER_ENABLED=true
PARSEHUB_PROVIDER_PYTHON=/path/to/venv/bin/python
PARSEHUB_PROVIDER_TIMEOUT_SECONDS=6
PARSEHUB_PROVIDER_MAX_OUTPUT_BYTES=524288
PARSEHUB_PROVIDER_CACHE_TTL_SECONDS=120
PARSEHUB_PROVIDER_FAILURE_COOLDOWN_SECONDS=8
```

The worker exchanges one bounded JSON document on stdin/stdout. AnimeImages
retains exact-note identity and source-order authority and rejects the complete
enhancement if item fingerprints do not correspond.

The optional provider fails closed to the existing Native image result. Runtime
invocation uses AnimeImages' hardened external-process runner with an isolated
process group, bounded stdout/stderr, a maximum eight-second timeout, and
TERM-then-KILL cleanup. The helper never downloads full media; candidate
admission reads at most 4096 bytes from each reviewed raw-image URL.
