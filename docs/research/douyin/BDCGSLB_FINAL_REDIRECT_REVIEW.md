# BDCGSLB Final Redirect Review

## Decision

- `BDCGSLB_ATTRIBUTION = SUFFICIENT`
- `BDCGSLB_MEDIA_BEHAVIOR = PASS`
- `POST_FIX_LIVE_BDCGSLB_REPRODUCTION = NOT_DETERMINISTICALLY_REPRODUCED`
- `INTERMEDIATE_REDIRECT_CHAIN_VISIBILITY = BROWSER_OPAQUE`
- `UNKNOWN_FINAL_HOST_AUTO_ADMISSION = NO`
- `MEDIA_MAGIC_ATTESTATION = DEFERRED_NO_SAFE_COMPATIBILITY_PROOF`
- `BROWSER_MEDIA_CANDIDATE_FALLBACK_V1 = DEFERRED_SEPARATE_PR`
- `BROWSER_STREAM_RANGE_RESUME_V1 = DEFERRED_POST_RELEASE`
- `DOWNLOAD_PAGE_CANCEL = DEFERRED_PENDING_USER_FEEDBACK`

`*.bdcgslb.com` is admitted only as a final browser-observed redirect from an
already admitted `*.365yg.com` initial media URL. It is not an initial media
source and cannot be placed in a download descriptor or Provider manifest.

## Problem

A real TianTianJieXi result for Douyin item `7323177294577552667` supplied an
initial `v5-default.365yg.com` media URL. Chrome followed the media request to a
strict subdomain of `bdcgslb.com`, where it received HTTP 200,
`video/mp4`, and an expected size of 110,691,390 bytes. The old flat host
allowlist rejected that final response as `FINAL_HOST_REJECTED` before any
body bytes entered the browser download stream.

Single and Batch use the same ordinary-media Service Worker protocol, so this
was one transport-policy defect with two user-visible entry points. Batch also
left its old progress presentation at zero after the terminal failure.

## Initial And Final Trust

Initial admission and final redirect admission have different authority:

- Initial URLs must be HTTPS, use default port 443, contain no userinfo, and
  match an existing reviewed initial source: `*.365yg.com`,
  `*.douyinvod.com`, or exact `v3-dy-a-x.ixigua.com`.
- A final URL may remain on an existing reviewed Douyin byte-media host.
- A strict subdomain of `bdcgslb.com` is final-only and is accepted only when
  the initial family is `DOUYIN_365YG` and the browser response confirms that
  a redirect occurred.
- The `bdcgslb.com` apex, lookalikes, trailing-dot forms, HTTP, userinfo,
  non-default ports, IP literals, localhost, and loopback hosts are rejected.
- An untrusted initial source cannot gain authority by redirecting to a
  reviewed final-only host.

This keeps Provider manifests and descriptors on the original strict source
policy. Final-only trust exists only while classifying the browser's actual
response to an already approved descriptor.

## Domain Evidence

The review collected only hostname-level and public infrastructure evidence:

- Public DNS delegates `bdcgslb.com` through Volcano Engine/ByteDance-branded
  authoritative DNS (`volc-dns.com` and `volc-dns.net`), with the SOA contact
  in the ByteDance domain.
- The observed HTTPS endpoint presented a valid certificate covering strict
  `*.bdcgslb.com` subdomains.
- The observed address resolved publicly and was not private, loopback, or an
  IP literal supplied as a hostname.
- Earlier real-browser evidence observed an approved `*.365yg.com` initial
  request and a strict `*.bdcgslb.com` final response, followed by HTTP 200
  and `video/mp4`.
- Browser CORS and body access were sufficient for the ordinary-media stream.

The browser reliably exposes the initial request, final `Response.url`, the
`redirected` flag, and final response headers/body. It does not expose the
complete cross-origin intermediate redirect chain, so the evidence proves the
reviewed initial-to-final relationship without claiming visibility into every
intermediate hop.

The exact edge label is dynamic infrastructure and is deliberately not pinned.
Only the strict subdomain relation is reviewed. Post-fix live downloads
completed successfully, but bounded follow-up attempts did not deterministically
reproduce BDCGSLB and instead remained on `365yg.com`. The final-only branch is
therefore supported by the earlier real-browser evidence, domain review, and
controlled protocol/Worker/Blob tests. This does not widen the policy or
establish any other final-only family.

A 1,024-byte Range response was confirmed on a current `365yg.com` source.
Range behavior was not independently proven for BDCGSLB and is not used as an
admission condition in this hotfix.

## Final Response Gates

Host admission alone is not enough. The existing response gates remain in
force:

1. Successful HTTP response.
2. Readable response body.
3. Allowed video content type or the existing controlled octet-stream form.
4. Existing expected-size and maximum-size checks.
5. Source-bound final host admission.

HTML, JSON, XML, SVG, empty bodies, unknown content, oversized media, and
unreviewed final hosts continue to fail closed. First-chunk media magic was not
added because the current compatibility set does not provide enough evidence
to make it a safe universal streaming gate without changing the data path.

## Telemetry Boundary

Unknown final hosts remain rejected. Existing terminal failure telemetry may
record only normalized, public hostname facts and fixed enums:

- initial media hostname and family;
- observed final hostname and class;
- redirect flag;
- HTTP status and content-type class;
- fixed response rejection reason and failure stage.

It never stores a media URL, path, query, signed token, Cookie, request header,
Provider payload, or source page URL. Public UI receives only a generic retry
message. Staff diagnostics may display the same sanitized hostname-level facts.
No observation automatically changes trust.

## Data Plane

The hotfix does not introduce a server relay or media proxy. Ordinary media
continues to use:

```text
Provider/CDN -> user browser Service Worker -> browser download manager -> disk
```

`/media/download-prepare/`, `/media/download-stream/`, and
`/media/download-status/` are not part of this path. Candidate fallback, Range
resume, and page-level cancellation remain separate design work.
