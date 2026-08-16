# Download UI & Batch Actions Production Acceptance v1

Date: 2026-08-16

## Decision

```text
DOWNLOAD_UI_BATCH_ACTIONS_PRODUCTION_ACCEPTED
```

PR #42 was exact-deployed and accepted at:

```text
production runtime: 9ba5702e9a90b9179e21d53393d8e27c39c51833
checkpoint:         checkpoint/production-download-ui-batch-actions-v1
previous checkpoint: checkpoint/production-provider-stabilization-v1
```

No production environment, Runtime Setting, database schema, Nginx, systemd,
Redis, PostgreSQL, Provider order, Provider timeout, or Batch deadline was
changed. The application service was restarted once by the existing deploy
flow. No rollback was required.

## Deployment Gates

- Production started clean at `d746ec9ff66532071b8079592f8b4831cae0fe51`.
- Candidate and `origin/main` were pinned to the exact PR #42 merge SHA.
- Django checks, migration dry-run and plan, focused Batch access/runtime tests,
  Provider Strategy tests, Browser Streaming telemetry tests, Node Batch tests,
  Node ordinary-media tests, JavaScript syntax checks, and `git diff --check`
  passed before deployment.
- Deployment applied no migration and copied three changed static files.
- The served Service Worker remained byte-identical to the candidate source and
  retained its content type, root scope, and no-cache contract.
- Environment, systemd, Nginx, Batch runtime, and Provider runtime hashes did
  not change. The repository Nginx drift checker remained at
  `NGINX_CONFIG_DRIFT=0`.

## Single Acceptance

- The Single page has no `[data-ordinary-active-downloads]` global panel.
- One real TianTianJieXi ordinary MP4 was parsed and downloaded in production.
- Chrome created and completed a Browser Download Manager task.
- The final file was exactly `110,691,390` bytes, matching the parsed exact
  size; no partial download remained.
- The page stayed on AnimeImages and did not open a Provider playback page.
- Card-local progress reached 100%; Chrome Download Manager remained the file
  task authority.
- During that transfer, `/media/download-prepare/`,
  `/media/download-stream/`, and `/media/download-status/` each had zero
  requests and zero response bytes. One small download descriptor response was
  control-plane JSON only.

The accepted ordinary-media path remains:

```text
Provider/CDN -> Browser Service Worker -> Browser Download Manager -> Disk
AnimeImages ordinary media bytes = 0
Server Relay = NO
post-start Blob fallback = NO
```

## Batch Acceptance

Production mixed parsing returned two successful cards in progressive order:

```text
video resources: 1
image resources: 10
total resources: 11
preview images:  4
```

The first gallery preview was `loading=eager` with high fetch priority; the
other three previews remained lazy. All ten image resources remained available
to actions and exports even though only four were previewed.

Anonymous production requests confirmed:

- `/batch/`, start, and both item requests returned HTTP 200;
- copy-all was present;
- `links.txt`, `aria2.txt`, per-item copy/open/details, and the strategy matrix
  were absent;
- the successful current-DOM resource set contained one video plus all ten
  images in stable first-occurrence order.

The staff production session confirmed:

- copy-all, `links.txt`, and `aria2.txt` were present;
- per-item copy/open/details and the strategy matrix remained present;
- downloaded `links.txt` contained the exact 11-resource current-DOM sequence;
- downloaded `aria2.txt` contained the same 11 URLs and 11 safe output names;
- video and image extensions were appropriate and no Cookie, Authorization,
  Referer, header, credential, or secret field was exported.

Copy-all and both exports use the same tested stable collector. Controlled
browser and Node contract tests cover the clipboard write and deduplication
behavior; production DOM and downloaded exports confirmed the exact mixed-media
resource sequence without recording the resource URLs in this report.

## Layout And Dynamic DOM

- Single and Batch DOMs contain no global active-download panel.
- Video cover, file information, and download action share one media area.
- Gallery preview and all image actions share one media area; there is no
  independent right-side cover column.
- Production desktop inspection showed no horizontal overflow or detached
  narrow preview column.
- Exact-candidate controlled browser gates passed at 1920 px and 390 px before
  deployment. Served production CSS and JavaScript were byte-identical to that
  candidate.
- Progressive append produced the production mixed cards. Current-DOM bulk
  collection included dynamically appended resources. Retry replacement and
  current-DOM recollection remained covered by the controlled gate to avoid
  additional Provider calls.

## Observation

The application was observed from the 12:22 HKT restart through at least 13:18
HKT, well beyond the required ten-minute window:

```text
service:                 active/running
NRestarts:               0
local readiness:         200
public readiness:        200
homepage:                200
Batch:                   200
5xx:                     0
Traceback:               0
Redis severe errors:     0
unexpected 429:          0
Worker errors:           0
```

Production remains pinned to the runtime SHA above. A later docs-only merge may
advance repository `main`; that is `REPO_HEAD_AHEAD_DOCS_ONLY = YES`, not
production runtime code drift and not a reason to redeploy.
