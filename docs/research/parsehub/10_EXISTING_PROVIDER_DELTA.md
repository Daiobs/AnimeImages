# Existing Provider Enhancement Delta

## Review baseline

This review keeps AnimeImages as the product and runtime owner. ParseHub is a
capability supplier examined by static review only:

- repository: `z-mio/ParseHub`
- commit: `144260f4349c5bb57415f7555a572fcd48c3b8ba`
- version: `2.2.0`
- license: MIT
- AnimeImages baseline: `2373bdf996ccd0f2596eb081a5ea781b7c268a6a`

The pinned source was not installed, imported, or executed. No ParseHub
Provider, adapter, platform, credential store, Cookie behavior, downloader, or
runtime dependency is adopted. Detailed machine-readable findings live in
`existing_provider_delta.json`.

## Decision summary

| Area | Existing AnimeImages position | Selected supplier idea | Decision | Delivery |
| --- | --- | --- | --- | --- |
| Source URL lifecycle | Platform-specific cleanup and identity | Closed parameter lifecycle declarations | `ADOPT_NOW` | PR-A, dormant typed foundation |
| Internal tooling | Separate parser/rich-media fixtures | One offline capability lab | `ADOPT_NOW` | PR-A |
| XHS Native | Ordered current-note media, no rich-media emission | Item-local still/motion relationship | `ADOPT_DORMANT` | PR-C |
| XHS extension probing | Bounded, admitted media behavior | Per-image HEAD and unknown-to-JPEG coercion | `REJECT_DIRECT` | None |
| Douyin Native/Ultra | Production-governed Provider strategy | Mobile/Story API with device/signing state | `SECURITY_REVIEW_REQUIRED` | Audit only |
| Bilibili video | Existing governed video adapter | Overlapping video parser | `ALREADY_STRONGER` | Preserve current adapter |
| Bilibili Dynamic/Opus | No active post-family consumer | Dynamic/Opus and LivePhoto payload evidence | `ADOPT_DORMANT` | PR-D research, identity gated |
| yt-dlp merge | Estimated callback progress | Structured byte/fragment progress | `ADOPT_NOW` | PR-B |
| gallery-dl generic | Bounded shared external runtime | No current distinct capability | `REFERENCE_ONLY` | None |
| External process lifecycle | Production-accepted process-group containment | Overlapping lifecycle ideas | `ALREADY_STRONGER` | Preserve in PR-B |
| Rich Media runtime | URL-free frozen runtime contract | Platform pairing evidence | `ALREADY_STRONGER` | Consume only through PR-C/PR-D |

## Source URL lifecycle

PR-A adds AnimeImages-owned, frozen, network-free runtime types:

- `SourceParameterPolicy`
- `SourceResolutionV1`
- `SafeSourceDiagnostic`

The closed parameter actions are:

- `parse_only`: available in `parse_url`, then removed from `post_parse_url`;
- `identity`: retained through `post_parse_url` and available to an adapter's
  explicit identity projection;
- `safe_passthrough`: retained through `post_parse_url` without becoming
  identity material; and
- `drop`: removed before parsing. Unknown parameters also fail closed to this
  action.

XHS `xsec_token` is represented as parse-only, Bilibili `p` as page/identity
relevant, Douyin `modal_id`/`aweme_id` as identity relevant, and tracking
parameters as dropped in fictional fixtures. Existing adapters do not migrate
in this PR, so Provider output, order, deadlines, cache and fallback behavior
do not change.

The four URL stages are deliberately separate: `input_url` is extracted from
the user input, `parse_url` applies the parse-stage policy, `resolved_url`
preserves the exact output supplied by an existing safe resolver, and
`post_parse_url` applies post-parse cleanup to that output. Without a resolver,
`resolved_url` equals `parse_url`. `resolved_url` is internal data, not a safe
diagnostic.

The platform supplies `canonical_identity`. The lifecycle validates and
carries it but never derives identity from raw URL bytes or a URL hash. All four
URLs and the identity value are internal runtime values with redacted `repr`
behavior. Safe output derives hostname and path class from `post_parse_url` and
is limited to platform, input kind, hostname, path class, canonical identity
kind, duration, and parameter names. Query values, fragments, raw share text,
tokens, path resource IDs, and canonical identity values are excluded.

The primitive does not follow redirects or make any request. Existing platform
resolvers remain responsible for safe redirects, host allowlists, private and
loopback rejection, bounded redirect counts, and deadlines.

No current Provider or adapter consumes this lifecycle foundation
(`CURRENT_SOURCE_LIFECYCLE_CONSUMERS = 0`). Existing XHS, Douyin, Bilibili and
generic behavior remains unchanged.

## Provider Lab v1

`tools/provider_lab/` is an offline standard-library-first surface with six
commands:

1. `inventory`
2. `validate-source-fixture`
3. `redact-source`
4. `compare-provider-capability`
5. `validate-rich-media`
6. `summarize-delta`

It imports no ParseHub module, executes no Provider, enables no network, reads
no production environment, and stores no credentials. Source fixtures use
reserved example domains and fictional IDs. Rejected values are represented by
fixed error codes without echoing the rejected URL or parameter value.

## External-tool conditional decision

```text
EXTERNAL_TOOL_PROGRESS_MATERIALS = ADOPT_NOW
```

The conditional PR-B gate is satisfied because existing
`download_youtube_merge_to_temp_path()` and generic yt-dlp merge callers already
pass a progress callback. A fixed progress template can improve callback
precision using downloaded bytes, total/estimated bytes, and fragment counters
without a public UI change, new Provider request, dependency, Provider order,
timeout, cache, or download-format change.

PR-B must preserve the production-accepted shared process runner: process-group
cleanup, TERM/KILL escalation, bounded live output, private cwd/HOME, minimal
environment, `YTDLP_NO_PLUGINS`, `PYTHONNOUSERSITE`, and no shell. Progress
records must be separated from raw output and excluded from user errors.

Temporary Cookie material is not adopted:

```text
COOKIE_MATERIAL = REJECTED_NO_CURRENT_CALLER_REQUIREMENT
```

No current caller needs a new Cookie text input, and this batch may not add a
Cookie product, browser credential reader, account configuration, or session
store. Temporary info material is only appropriate when generated by an
existing caller and must be private and deleted on every exit path.

## Platform boundaries

### XHS

The supplier payload provides useful evidence only when a still image and
motion stream occur in the same image item. PR-C may normalize that relationship
into URL-free `RichMediaContractV1` values only after exact requested note ID
and payload note ID equality. Unknown extensions are rejected, HEIC is deferred,
and no media host or fallback is added. Current XHS parsing remains unchanged.

### Douyin

This batch preserves canonical aweme identity, TianTian/KuKuTool strategy, the
post-resolution Ultra window, payload cache, single-flight, cooldown,
continuation, exact ixigua admission, Browser Streaming, and zero relay.
ParseHub mobile/Story support depends on device state, signing, and session
material, so it remains `SECURITY_REVIEW_REQUIRED`. There is no mobile Provider,
device registration, signing implementation, Story routing, or runtime change.

### Bilibili

The existing video adapter remains authoritative. Numeric Dynamic and Opus
source shapes plus item-local `live_url` pairing are research evidence, not an
activation decision. Stable Dynamic/Opus identity mapping and media hosts must
be proven before routing. PR-D may add only documentation, fixtures, and a
dormant network-free normalizer if that evidence is sufficient.

## Unchanged product surface

This batch does not change models, migrations, settings defaults, dependencies,
templates, static assets, service workers, public/staff rollout, Provider
registry/order/deadlines/cache/rate limits, manifests, downloads, Browser
Streaming, ZIP, server relay, Nginx, systemd, checkpoints, or production. No
current Provider emits a new rich-media value as a consequence of PR-A.
