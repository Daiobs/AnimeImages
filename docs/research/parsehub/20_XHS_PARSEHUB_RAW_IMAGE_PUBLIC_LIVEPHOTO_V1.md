# XHS ParseHub Raw Image And Public LivePhoto v1

## Status

This document records a development-only implementation. It has not been
deployed to production.

```text
ROUTE_DECISION_AUTHORITY = USER
ROUTE_MODE = FIXED_PLATFORM_POLICY
PROVIDER_PLANNER = NO
PARSEHUB_PIN = 144260f4349c5bb57415f7555a572fcd48c3b8ba
```

## Fixed Routes

Ordinary XHS image notes use Native exact-note identity and source order,
ParseHub raw images as the preferred candidate, and the current Native image as
a per-item fallback. A mismatch in note identity, item count, source kind,
fingerprint, or order rejects the complete ParseHub enhancement.

Live Photo notes retain Native authority for identity, order, item-local
relationships, and motion. An admitted ParseHub raw still is preferred when it
is browser-compatible; otherwise the compatible Native still is retained.
HEIC remains available as an ordinary-image original, but is not placed in a
browser Live Photo still element.

XHS ordinary-video routing remains Native metadata to TianTian Ultra with the
existing Native fallback. Images and Live Photo never call TianTian.

## Isolation Contract

ParseHub is not an AnimeImages dependency and is never imported into Django or
Gunicorn. `tools/parsehub_provider/` installs the exact upstream commit into a
separate virtual environment. Runtime starts one anonymous, parse-only worker
per request using bounded JSON on stdin/stdout. The worker uses `cookie=None`
and `proxy=None`, does not expose raw payloads, and never imports or calls the
ParseHub downloader.

AnimeImages uses its hardened external-process lifecycle: a separate process
group, bounded stdout and stderr, an eight-second maximum timeout,
TERM-then-KILL cleanup, and no retained temporary home or working directory.
Successful results are cached by `xiaohongshu:<exact-note-id>` for 120 seconds;
equivalent short, share, and canonical forms share that cache and single-flight.
Failures use an eight-second cooldown.

For a Live Photo request, Native relationship/motion evidence and ParseHub
still-image evidence are collected concurrently inside the same request
deadline. This prevents either evidence chain from consuming the other chain's
budget. The deadline is not extended: an incomplete ParseHub chain falls back
to Native stills, while incomplete or unverified Native motion evidence keeps
the complete rich-media enhancement fail-closed.

## Candidate Admission

Only an exact HTTPS URL on `sns-img-hw.xhscdn.com` is accepted. Userinfo,
custom ports, IP literals, trailing dots, queries, fragments, empty paths,
traversal-like resource keys, and unknown redirect targets fail closed.

Each candidate is read only through a bounded 4096-byte prefix probe. Admission
requires a 200 or 206 response, a reviewed final host, non-markup content, image
magic, and positive dimensions. The original extension and MIME are derived
from magic for JPEG, PNG, GIF, WebP, AVIF, and HEIC. Content-Type, URL suffix,
and Provider declarations are not format authority.

## Presentation And Download

`MediaAsset.url` is the selected original candidate. `preview_url` is a
separate browser-compatible optimized preview targeting 1280px. It is valid for
preview and original formats to differ, including a Native JPEG preview for a
raw HEIC download.

Single and Batch ordinary-image downloads use the same selected original,
extension, order, and signed provider-manifest policy. Browser code validates
image magic before saving. XHS image items are not eligible for server ZIP,
server-single, prepare, stream, or status fallback. If the original expires,
the user is asked to parse again.

```text
IMAGE_DATA_PLANE = CDN -> Browser -> User disk
SERVER_MEDIA_RELAY = NO
PARSEHUB_DOWNLOADER = DISABLED
```

## Live Photo Boundaries

Anonymous Single XHS image pages may render complete, exact-note Live Photo
groups. Motion URLs are assigned only after a user click. Playback is one-shot,
restores the still image after `ended`, unloads the motion source, and allows at
most one active card. Public presentation contains no Staff diagnostics.

Live Photo download authority remains disabled. Cards expose no still download,
motion download, ZIP, manifest, or download token. If rich-media enhancement
cannot produce all groups safely, the entire enhancement falls back to the
ordered flat image result. Batch continues to render flat images while receiving
the same raw-image fidelity route.

The discovered `motion_zl` candidate class remains outside the browser-verified
subset. A payload that selects it is intentionally rendered as the complete
ordered flat-image fallback; this implementation does not widen motion host or
format admission to turn an intermittent upstream shape into a false success.

## Development Acceptance

Local Chromium acceptance used transient samples that are not recorded in the
repository. An ordinary four-image note preserved Native order across Single
and Batch. Single-file download produced a complete magic-valid JPEG, and both
Single and Batch browser ZIPs contained four ordered entries with valid CRCs;
the first ZIP entry was byte-identical to the separately downloaded image.
These browser-owned transfers made no prepare, stream, or status request to
AnimeImages.

Anonymous Live Photo acceptance covered a complete fourteen-item note and a
mixed seventeen-item note containing twelve Live Photos plus five regular
images. Five sampled motions from each successful result played once, restored
the still image after natural `ended`, unloaded the motion source, and remained
replayable. No motion request was issued before click, regular images had no
playback control, and Live Photo results exposed no download control. Staff
used the same playback behavior with its additional safe validation summary.

Batch kept the mixed note as seventeen flat images with no motion item or
playback control. Thirteen admitted raw stills were selected and four items
used the per-item Native fallback; observed original extensions included JPEG
and PNG. The invalid control produced no image, video, or Live Photo result.
The ordinary-video control retained the existing Ultra attempt and Native
fallback route.

The in-app browser stayed at a 1280 CSS-pixel viewport when asked to emulate
390px and 820px widths. Responsive behavior remains covered by deterministic
CSS/DOM tests and is listed for final human viewport confirmation rather than
being reported as a completed real-device gate.

## Runtime Configuration

The feature is disabled unless both the enable flag and isolated interpreter
are configured:

```text
PARSEHUB_PROVIDER_ENABLED=true
PARSEHUB_PROVIDER_PYTHON=/path/to/isolated-venv/bin/python
PARSEHUB_PROVIDER_TIMEOUT_SECONDS=6
PARSEHUB_PROVIDER_MAX_OUTPUT_BYTES=524288
PARSEHUB_PROVIDER_CACHE_TTL_SECONDS=120
PARSEHUB_PROVIDER_FAILURE_COOLDOWN_SECONDS=8
```

No database migration, model change, application dependency, production
configuration, or production deployment is part of this implementation.
