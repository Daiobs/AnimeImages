# External Tool Subprocess Lifecycle

## Status and provenance

This implementation is owned by AnimeImages. The generic ideas of an isolated
process group, bounded TERM/KILL escalation, and output limits were informed by
the static ParseHub review at commit
`144260f4349c5bb57415f7555a572fcd48c3b8ba`. No ParseHub source was copied,
installed, imported, or executed, and no dependency or third-party notice was
added.

## Caller map

| Caller | Provider/use | Progress | Timeout source | Return and exception contract | Output/cache behavior |
| --- | --- | --- | --- | --- | --- |
| `run_ytdlp_json()` | `yt_dlp_generic` parsing | No | Existing `YTDLP_TIMEOUT_SECONDS`, capped by the frontend budget | Parses `CompletedProcess.stdout` as a dict; preserves `TimeoutExpired` for conversion to the existing provider timeout and preserves missing-executable/nonzero errors | Reads JSON from stdout and a bounded error tail; provider payload/failure caches remain above the runner |
| `run_gallerydl_json()` | `gallery_dl_generic` parsing | No | Existing `GALLERYDL_TIMEOUT_SECONDS`, capped by the frontend budget | Parses one JSON document or JSON lines; preserves the existing timeout, missing-executable and nonzero contracts | Reads stdout and a bounded error tail; provider payload/failure caches remain above the runner |
| `probe_external_command_version()` | External-tool health/version probe | No | Existing short probe timeout, default three seconds | Returns the existing result dictionary and converts timeout/output-limit/OS failures to its existing safe error values | Uses the existing short cache key and TTL; only the first bounded output line is retained |
| `download_youtube_merge_to_temp_path()` | YouTube server-generated merge output | Yes when size/progress are available | Existing `YOUTUBE_MERGE_TIMEOUT_SECONDS` | Consumes `CompletedProcess`; nonzero exit remains an `XhsDownloadError` | stdout/stderr remain bounded; generated output stays in the existing outer temporary directory; no payload cache change |
| `ytdlp_merge.download_to_temp_path()` | Generic yt-dlp server-generated merge output | Yes when size/progress are available | Existing `YTDLP_MERGE_TIMEOUT_SECONDS` | Consumes `CompletedProcess`; nonzero exit remains an `XhsDownloadError` | stdout/stderr remain bounded; generated output and manifest behavior are unchanged |

`external_command_health()` only resolves an executable and does not launch a
process. Executor `Future` timeouts remain a caller scheduling boundary, not an
OS-process cancellation signal.

## Previous gap

An offline probe using only the active Python interpreter and fictional
child/grandchild scripts established the old behavior:

- both non-progress `subprocess.run()` and progress `Popen` timeout paths killed
  the direct process while a grandchild could remain alive;
- a parent could exit successfully while a same-group child remained alive;
- a progress callback exception could leave the process tree running; and
- stdout/stderr limits were checked only after process exit, so a command that
  kept writing could reach timeout before the overflow was detected.

The probe cleaned every process it created. Temporary PIDs and paths are not
retained in this document.

## Lifecycle contract

Progress and non-progress commands now share one `Popen` monitor:

1. create the existing private temporary cwd/HOME and temporary output files;
2. on POSIX, launch with `start_new_session=True`, making the child the leader
   of a new session and process group;
3. monitor the direct process, timeout, progress, and each output stream on a
   bounded interval;
4. on timeout, output overflow, callback error, runner exception, or
   `BaseException`, signal the process group and finish cleanup before
   preserving the original exception;
5. on any parent exit, check for remaining same-group descendants and clean
   them before returning the parent's `CompletedProcess`; and
6. remove temporary files and directories through the existing context
   managers on every path.

Cleanup sends `SIGTERM`, waits for a fixed 0.5-second grace, then sends
`SIGKILL` if the group remains. The grace is internal rather than a runtime
setting and is never paid by a normal command with no remaining descendants.
Cleanup failures are reduced to a sanitized executable basename and error type;
they never replace the primary timeout, callback, output-limit, or OS error.

On non-POSIX systems, the runner conservatively terminates, waits for, kills,
and reaps only the direct process. Therefore:

```text
PROCESS_GROUP_DESCENDANT_GUARANTEE = POSIX_ONLY
```

The POSIX guarantee covers descendants that remain in the inherited process
group. A child that deliberately creates a new session escapes OS process-group
control and is outside this primitive's guarantee.

## Output and memory behavior

`THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES` remains a per-stream limit. The runner
checks temporary stdout and stderr file sizes while the command is running. If
either exceeds the limit, the whole controlled group is cleaned before
`ExternalCommandOutputTooLarge` is raised. Oversized content is not loaded into
Python memory. The post-exit read check remains as a final race-safe guard.

## Cancellation audit

```text
COOPERATIVE_CANCEL_WIRING = DEFERRED_NO_EXISTING_CALLER_SIGNAL
```

- a `Future` cancelled before execution prevents its callback from starting;
- `Future.cancel()` after execution starts does not stop the OS process;
- current request/client cancellation does not provide a cooperative event to
  external-tool callers; and
- runner-owned timeout, overflow, callback error, internal exception, and
  `BaseException` paths are fully connected to process cleanup.

No optional event was added because no current product caller can truthfully
drive it. This document does not claim that Public or Staff users can cancel an
already-running external tool.

## Preserved boundaries

- command arguments remain a list; no shell or unsafe `preexec_fn` is used;
- the minimal environment, `PYTHONNOUSERSITE=1`, `YTDLP_NO_PLUGINS=1`, private
  cwd/HOME, and executable resolution contracts are unchanged;
- Provider order, timeout values, retries, cache keys, failure TTLs, parsing,
  user-visible errors, downloads, Browser Streaming, and zero-relay behavior
  are unchanged; and
- no production system, runtime setting, database, migration, or dependency is
  touched by this implementation PR.
