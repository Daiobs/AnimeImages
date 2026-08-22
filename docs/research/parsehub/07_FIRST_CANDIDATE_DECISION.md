# First Adoption Candidate Decision

## Decision

```text
FIRST_ADOPTION_CANDIDATE = yt-dlp subprocess lifecycle hardening
CLASSIFICATION = ADOPT_CANDIDATE
```

The candidate is lifecycle behavior, not ParseHub installation or source
vendoring.

Implementation status: completed as an AnimeImages-owned design on the
`feat/external-tool-subprocess-lifecycle-v1` task branch. The original decision
and non-goals below remain the provenance record.

## Comparative score

Scores are relative, 1 (low) to 5 (high). For risk/blast radius, lower is better.

| Candidate | User value | Current gap | Implementation risk | Security risk | Dependency impact | Blast radius | Offline testability | Zero-relay fit | Time to value |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: |
| yt-dlp subprocess lifecycle | 4 | 3 | 2 | 2 | 1 | 2 | 5 | 5 | 5 |
| Rich media domain contract | 4 | 4 | 3 | 2 | 1 | 3 | 5 | 5 | 3 |
| Bilibili Dynamic/LivePhoto | 4 | 4 | 3 | 3 | 2 | 3 | 4 | 4 | 3 |
| Douyin Story/mobile fallback | 3 | 2 | 5 | 5 | 3 | 5 | 2 | 4 | 1 |
| Demand-driven new platform (Kuaishou example) | 2 | 3 | 4 | 5 | 3 | 4 | 2 | 3 | 2 |

## Why selected

- **SOURCE-DERIVED FACT:** the upstream lifecycle uses a process group,
  TERM-then-KILL escalation, cancellation cleanup, bounded output tails and
  private temporary Cookie/info files.
- **ANIMEIMAGES FACT:** AnimeImages already has bounded command output,
  isolated temporary cwd/HOME, a minimal subprocess environment, provider
  deadlines, executor limits and extensive yt-dlp tests.
- **INFERENCE:** adding an AnimeImages-owned process-group lifecycle closes a
  specific cancellation gap without expanding platform access or changing the
  ordinary-media Browser Streaming data plane.
- **RECOMMENDATION:** implement only generic subprocess lifecycle behavior;
  preserve existing command allowlists, output limits, ProviderOutcome, host
  admission and sanitized user errors.

## Why the others are deferred

- **Rich media domain contract:** high-value and fully offline-testable, but it
  affects `MediaAsset`, templates and download behavior across platforms. It
  should follow a separate domain-design review after lifecycle hardening.
- **Bilibili Dynamic/LivePhoto:** meaningful user-visible gap, but native API,
  dynamic identity, media-host discovery and live staff evidence are required.
- **Douyin Story/mobile fallback:** AnimeImages is already stronger for accepted
  Douyin flows. Device registration, signatures and account/anti-abuse risk make
  this unsuitable as the first supplier-derived task.
- **New platform:** no repeated demand has been recorded. Kuaishou additionally
  has embedded session-material and credential/host risks.

## Required next task

Implementation PR: `Harden external-tool subprocess lifecycle`.

Expected scope:

1. Add a shared AnimeImages-owned process-group launcher/terminator for POSIX,
   with a conservative non-POSIX path.
2. Preserve current environment, cwd/HOME isolation, command parsing, output
   file limits and provider timeouts.
3. On timeout/cancellation, signal the group, wait a short bounded interval,
   then force terminate if needed.
4. Sanitize stderr/stdout before logging or user-visible errors.
5. Add fake child/grandchild process tests; no real yt-dlp or network.
6. Run focused external-tool, provider and full repository tests.

Required proof is entirely offline first: no child or grandchild survives
timeout/cancellation, output limits still work, temporary files are removed and
existing provider semantics remain unchanged. A later Staff-only smoke may use
an approved lightweight source, but no Public rollout is implied.

## Explicit non-goals

- No ParseHub dependency or import.
- No platform parser adoption.
- No Bilibili Dynamic implementation.
- No Cookie support expansion.
- No Provider-order, timeout or runtime-gate change.
- No ordinary-media Server Relay.
- No production deployment in the research or first implementation PR.
