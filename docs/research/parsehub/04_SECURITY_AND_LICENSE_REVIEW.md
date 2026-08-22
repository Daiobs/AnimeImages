# ParseHub Security And License Review

## Security findings

| Area | Static finding | AnimeImages decision |
| --- | --- | --- |
| Dynamic registry | Parser modules are discovered and imported dynamically | Do not use in public runtime; retain explicit registry |
| URL admission | Regex platform matching precedes platform requests; no equivalent central blocked-host/IP boundary was found | Replace with AnimeImages scheme, hostname and IP admission |
| Redirects | Keyword-based short links are followed server-side with a fixed timeout | Use the AnimeImages safe resolver, redirect cap and revalidation |
| Result serialization | Raw source and direct media URLs are serializable | Never pass supplier results to views; normalize and sign manifests |
| Cookies | Several platform APIs accept Cookies; CLI persists them in a private local file | No public credential input; capability and Admin gates remain authoritative |
| Kuaishou | Embedded Cookie-like session material exists in the registered parser source | `SECURITY_REVIEW_REQUIRED`; never copy the value or implementation directly |
| Signatures/devices | Douyin includes Web/mobile signing and device registration/pooling | Dedicated threat review and staff smoke are required before any derived work |
| Local downloader | Server/local process performs media HEAD, Range and body transfer | Reject for ordinary public media; preserve zero-relay Browser Streaming |
| yt-dlp errors | Bounded output tails may still contain upstream URLs or request context | Any adopted lifecycle must sanitize before logs or user errors |
| Dependency surface | HTTP, parsers, image/video, crypto/signing and development yt-dlp dependencies | No dependency import in this baseline; review per future candidate |

`EMBEDDED_SESSION_MATERIAL_PRESENT = YES` is intentionally the complete record.
The material itself was not copied to documentation, fixtures, logs, commit
messages or PR text.

## Risk conclusions

- **SOURCE-DERIVED FACT:** redirect following and platform requests are bounded
  in several call sites, but timeout values and retry styles are not governed by
  one request deadline.
- **INFERENCE:** a regex match plus dynamic parser import is insufficient SSRF
  protection for a public Django service.
- **SOURCE-DERIVED FACT:** direct URLs and optional headers live in supplier
  media/result objects. Passing these objects to public templates would bypass
  AnimeImages manifest and permission boundaries.
- **ANIMEIMAGES FACT:** AnimeImages must retain exact media-host admission,
  blocked-host/IP checks, ProviderOutcome, sanitized diagnostics, public/staff
  runtime gates and zero-relay ordinary-media delivery.
- **UNVERIFIED:** endpoint reliability, anti-bot behavior, account risk, CORS and
  current platform availability were not tested.

## License

ParseHub is licensed under MIT at the pinned commit. The notice names
`Copyright (c) 2024 梓澪`.

No substantive upstream implementation is copied by this research PR. Static
facts, architectural comparisons and an independently implemented supplier-
neutral comparator are recorded as research provenance.

Future work must follow this rule:

1. Pin repository, commit and source files.
2. Record the MIT license and copyright holder.
3. If code is copied or substantively adapted, update
   `THIRD_PARTY_NOTICES.md`, preserve the MIT notice and add a concise source
   provenance comment in the derived implementation.
4. If only concepts or public interface facts are used, record provenance in
   the relevant research/design document.
5. Review licenses of transitive tools separately; ParseHub's MIT license does
   not replace the licenses of yt-dlp or other dependencies.

## Secret handling result

```text
ACTUAL_SECRET_COPIED = NO
REAL_COOKIE_USED = NO
REAL_TOKEN_USED = NO
SIGNED_MEDIA_URL_RECORDED = NO
LIVE_PROVIDER_REQUEST = NO
```
