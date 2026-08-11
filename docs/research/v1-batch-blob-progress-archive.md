# V1 Batch Browser Progress Archive

Date: 2026-08-12

This note preserves the useful UI history without retaining dead production
code. It contains no source URL, signed media URL, Provider credential, cookie,
or token.

## Historical Implementation

The ordinary Batch video path previously lived in `static/js/batch_parse.js`.
Its main pieces were:

- `readBlobWithProgress(response, onProgress)`
- `downloadBatchDirectUrl(url, filename, onStatus, onProgress)`
- `updateBatchDownloadProgress(button, percent, label)`
- the `data-batch-download-progress` card markup
- the corresponding progress track, bar, label, and indeterminate animation in
  `static/css/app.css`

The implementation appeared during the `6abd299` / `7496ddf` Batch download
work and remained visible in later V1 history, including `faf71f6` and the
`485951b` production baseline.

## UI Behavior

The page fetched the CDN response in JavaScript, read every body chunk, retained
those chunks in memory, calculated a visible 0-100 percent value from
`Content-Length`, assembled a complete Blob, and finally clicked an object-URL
download anchor.

The colored progress treatment was useful because it represented bytes the page
itself was receiving. It was not a server job progress indicator and it did not
mean that the browser download manager had already created a file task.

## Why It Left The Ordinary Path

Recovery v1 keeps the browser-side Blob data path for ordinary TianTianJieXi and
KuKuTool videos, but Single and Batch now call one shared primitive. The ordinary
product UI exposes only preparation, acquisition, and save-handoff states. It no
longer presents a separate Batch-only transfer meter or asks users to understand
transport modes.

The old implementation also retained an array of every response chunk before
creating the Blob. That duplicated logic, increased large-file memory pressure,
and allowed Single and Batch behavior to drift.

## Reuse Boundary

The visual pattern may be reused for work whose progress AnimeImages genuinely
owns, such as:

- YouTube audio/video merge jobs
- ZIP generation
- other server-generated output jobs

It should not be restored as a second ordinary-video downloader. Git history is
the source of truth for the removed implementation.
