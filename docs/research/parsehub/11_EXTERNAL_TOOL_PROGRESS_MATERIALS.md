# External Tool Progress And Temporary Materials

## Status

`ADOPT_NOW` for bounded yt-dlp progress parsing and private temporary output.

`REFERENCE_ONLY` for temporary info JSON materialization.

`REJECT_DIRECT` for Cookie materialization because no current AnimeImages caller
accepts Cookie text.

This document describes a Draft PR. It has not been deployed.

## Provenance

The capability review used `z-mio/ParseHub` v2.2.0 at the immutable commit
`144260f4349c5bb57415f7555a572fcd48c3b8ba` under its MIT license. The reviewed
implementation is `src/parsehub/parsers/base/ytdlp.py`. ParseHub was inspected
statically; it is not installed, imported, or executed by AnimeImages.

## Existing Caller Evidence

AnimeImages already has two server-generated-output callers that accept the
same `callback(completed, total)` contract:

- YouTube high-quality video/audio merge in
  `xhs_download/services/youtube_merge.py`.
- Generic yt-dlp video/audio merge in
  `xhs_download/services/platforms/ytdlp_merge.py`.

Both callers already report an initial estimate, directory-size progress, and
a final output-size event. The existing directory scan is a useful fallback,
but it cannot distinguish downloaded bytes from merge artifacts and does not
expose fragment progress. This is sufficient current demand to adopt structured
yt-dlp progress without changing public UI or download behavior.

The generic JSON parser, gallery-dl parser, external version probes, and
ordinary browser-streaming paths do not currently consume download progress.
They remain unchanged.

## AnimeImages-Owned Design

AnimeImages defines its own reserved progress prefix and a closed six-field
yt-dlp progress template:

- status;
- downloaded bytes;
- exact total bytes;
- estimated total bytes;
- fragment index;
- fragment count.

The parser accepts only `downloading` and `finished`, bounds line length, byte
counts, and fragment counts, and ignores malformed values. It maps exact bytes,
estimated bytes, or fragment position into the existing caller estimate while
keeping callback byte counts monotonic. Structured download progress is capped
below final completion until the output file has been validated.

The existing process runner reads progress lines from its bounded temporary
stdout/stderr files with positional reads on POSIX. Positional reads do not
change the child process file offset. On systems without positional reads,
events are replayed after process completion; lifecycle and output bounds remain
the same. Reserved progress lines are removed from returned stdout/stderr so
they cannot enter caller error text. Non-progress output remains available
under the existing live output cap.

If no valid structured event arrives, the existing directory-size callback
continues to operate. Once a valid structured event is observed, directory
progress is suppressed to avoid mixing two byte sources. Callback exceptions
continue through the established process-group cleanup path.

## Temporary Material Decision

The runner's existing anonymous stdout and stderr files are explicitly hardened
to mode `0600`. They remain bounded, close on success and failure, and are
removed with the temporary execution context. The existing isolated temporary
HOME/cwd, minimal environment, `YTDLP_NO_PLUGINS`, `PYTHONNOUSERSITE`, no-shell
execution, output cap, and TERM-then-KILL cleanup are unchanged.

ParseHub also materializes Cookie text and yt-dlp info JSON into private files.
AnimeImages has no current caller that supplies Cookie text or needs
`--load-info-json`, so those facilities are not adopted. This PR does not read
browser Cookies, add account configuration, introduce credential storage, or
create new temporary content files.

## Safety And Compatibility

- No Provider request is added.
- Provider order, timeout, cache, cooldown, and rate limits are unchanged.
- Format selection, merge output, Provider manifests, and downloads are
  unchanged.
- No UI, template, static asset, service worker, or public API changes.
- No setting default, dependency, model, migration, or database change.
- No ParseHub source or runtime dependency is copied into AnimeImages.
- Tests use fictional command output and local child processes only; they do
  not access a Provider or the public internet.
- Progress records contain numeric state only and are excluded from returned
  error tails.

## Verification Contract

Focused tests cover parser bounds, exact and estimated totals, fragments,
monotonic mapping, fallback behavior, structured/raw output separation,
private stream modes, invalid configuration, callback failure, process-group
cleanup, and both current merge callers. Existing external-tool lifecycle tests
continue to prove timeout, output-limit, callback-error, descendant cleanup,
temporary directory removal, environment isolation, and no-shell execution.

Production access and deployment are outside this PR.
