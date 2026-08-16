# Batch Photo Gallery And Download UX v1

Date: 2026-08-16

## Status

```text
MAIN_CANDIDATE_NOT_YET_PRODUCTION_ACCEPTED
Production SHA: 9ba5702e9a90b9179e21d53393d8e27c39c51833
Production checkpoint: checkpoint/production-download-ui-batch-actions-v1
Migration: none
```

This candidate was developed from `origin/main` at
`d85375b5b27a72a98e66e9c7b308c2bd9b55bb68`. It does not access or modify
production.

## Before

Batch image rows rendered at most four preview images through
`slice:":4"`. The fixed 220px two-by-two preview used `object-fit: cover`, so
portrait, landscape, and long images could be cropped. Download actions lived
in a separate text list, making image-to-action correspondence difficult.

## Candidate

- The rendered photo count equals the parsed `image_items` count and preserves
  parser order.
- Each image is a self-contained Gallery item with its sequence number and
  browser download action.
- CSS Grid uses auto-filled tracks on desktop and one column at 390px. Images
  retain intrinsic ratio with `height: auto` and `object-fit: contain`.
- The first image in a card is eager/high priority. Remaining images are lazy
  and every image keeps asynchronous decoding.
- XHS card previews use a deterministic 960px derivative. One global
  row-scoped lightbox loads a 1600px derivative only when opened; failure falls
  back to the card preview without navigating to the raw CDN URL.
- Lightbox navigation is bounded to one result row and supports previous,
  next, ArrowLeft, ArrowRight, Escape, and download-current actions.

## Download Contract

Preview and download candidates are separate. Neither the 960px card URL nor
the 1600px lightbox URL replaces the parsed download URL.

```text
Single image:
Image CDN -> Browser -> Disk

Ordinary video:
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk

Download All Images:
Image CDN -> AnimeImages ZIP generator -> Browser -> Disk
```

The ZIP path is an explicit `SERVER_REQUIRED_GENERATED_ARTIFACT`, not an
ordinary-media Relay fallback. The client posts only the current row's image
download candidates to the existing `/media/download-tasks/` flow when async
tasks are enabled, or `/media/download-images/` otherwise. It sends no preview
URLs and performs no Provider reparse.

The server revalidates item type, scheme, media host, Provider manifest, SSRF
boundary, count, and byte limits. Batch image archives preserve row order,
use zero-padded ordinals and response-derived extensions, sanitize archive
names, retain existing partial-success behavior, and never report an empty ZIP
as success. When async tasks are disabled, the synchronous browser receiver
enforces a 128 MiB memory boundary and cancels larger or unbounded responses;
users can still download the photos individually. Async task downloads remain
the preferred path for larger generated archives.

## Permissions

- Public users allowed to use Batch can view the Gallery, download individual
  images, download the current row as one ZIP, and use copy-all.
- Public users do not receive per-item raw copy/open controls, `links.txt`,
  `aria2.txt`, or the strategy matrix.
- Staff retain all existing raw and export tools.
- Bulk collectors continue to read the current successful DOM, preserving
  the legacy video-first order plus image parser order, stable deduplication,
  progressive append, and retry replacement.

## Verification

Automated verification uses controlled bytes and no real Provider or
production request:

```text
Django full suite: 1109 passed
Node full suite:   129 passed
Django check:      passed
Migration check:   no changes detected
JavaScript syntax: passed
git diff --check:  passed
```

Controlled Chrome acceptance used 3-, 5-, and 10-image rows with portrait,
landscape, square, tall, and wide PNG fixtures:

- 1920px: every item rendered, five columns available for larger rows, no
  cropping, and no horizontal overflow.
- 390px: one column, usable actions, contained images, and no horizontal
  overflow.
- No 1600px preview was loaded initially. Opening image 3 of a five-image row
  loaded exactly that lightbox candidate and displayed `3 / 5`.
- Keyboard navigation reached `5 / 5`, disabled next at the boundary, moved
  back with ArrowLeft, and closed with Escape.
- A per-photo action created one image download. Download All for the
  five-image row created one ZIP browser download and updated only that row.
- Browser console errors and warnings attributable to the candidate: zero.

## Remaining Fidelity Boundary

```text
PHOTO_ORIGINAL_VARIANT_SELECTION_NOT_PROVEN
PHOTO_DOWNLOAD_VARIANT_FIDELITY_BACKLOG
```

This task proves that preview transformations do not replace the download
candidate. It does not prove that the current XHS parser always selects the
highest-quality original image among every CDN variant. Product copy therefore
uses `下载`, not `下载原图`. A separate photo-fidelity task may improve variant
selection without reintroducing unbounded media probes.
