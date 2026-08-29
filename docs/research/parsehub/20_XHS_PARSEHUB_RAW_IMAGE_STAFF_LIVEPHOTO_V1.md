# XHS ParseHub Raw Images And Staff LivePhoto v1

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

Anonymous Single XHS requests do not start the Live Photo delivery builder.
They use the ordinary flat-image result, grid, preview, lightbox, selection, and
original-image download behavior. They receive no Live Photo badge, motion URL,
video element, pair token, pair descriptor, playback script, pair-download
script, or Staff diagnostic. Batch follows the same flat-image boundary.

Only an authenticated, active Staff request may build and render exact-note
Live Photo groups. Staff uses the same ParseHub-enhanced still assets, source
order, formats, and filenames as the flat route, then adds Native relationship
and motion evidence, one-shot playback, URL-free diagnostics, original-image
download, and a browser-generated pair ZIP. The pair descriptor is POST- and
CSRF-protected, bound to the issuing Staff user and browser identity, and is not
usable by anonymous, non-Staff, inactive, or different Staff sessions.

The pair ZIP contains the original still in its admitted format plus one MP4
motion item. Both bodies travel directly from their admitted CDN hosts to the
browser; AnimeImages issues only the opaque control-plane descriptor and never
relays the media or ZIP bytes.

The discovered `motion_zl` candidate class remains outside the browser-verified
subset. Current acceptance selected the already-reviewed `motion_versioned`
master for every Primary item; same-item `motion_backup` candidates were also
present and admitted. No wildcard XHS CDN rule or new motion host class was
needed.

## Development Acceptance

Local Chromium acceptance used transient samples that are not recorded in the
repository. An ordinary four-image note preserved Native order across Single
and Batch. Single-file download produced a complete magic-valid JPEG, and both
Single and Batch browser ZIPs contained four ordered entries with valid CRCs;
the first ZIP entry was byte-identical to the separately downloaded image.
These browser-owned transfers made no prepare, stream, or status request to
AnimeImages.

Anonymous acceptance of the Primary three-item Live Photo note produced three
ordinary image cards in Native order with the normal image controls. It exposed
no special Live Photo DOM, motion request, pair request, or Staff script. The
flat layout had no horizontal overflow at the 390px, 820px, and 1440px gates.

With the exact ParseHub pin enabled in an isolated development environment,
five consecutive Staff parses each produced three delivery-ready Live Photo
cards. All three motions played once, restored the still after natural `ended`,
unloaded their source, and remained replayable while keeping at most one active
item. All three pair ZIP actions completed in the browser. Every archive had
exactly two CRC-valid entries: one original JPEG or HEIC still and one MP4
motion item. No prepare, stream, status, server ZIP, TianTian image, TianTian
Live Photo, or ParseHub downloader path was used.

The initial disabled Staff controls were not caused by rejected motion. Native
motion selection had already admitted a reviewed master and same-item backups;
the actual fixed reason was pair still authority rejection when the isolated
ParseHub runtime was disabled. Staff diagnostics now preserve that fixed,
URL-free stage distinction instead of reporting every pair-token failure as a
motion-admission failure.

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
