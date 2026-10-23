# Product Contracts

The confirmed text is in [product-invariants.md](../docs/product-invariants.md).
These tests protect INV-01 through INV-09. Base for the recorded counterchecks:
`5afbab0d5a1dff19aa3b3a71e7a2b94ec7f3b0ae`; INV-01 (added by T12) used
`f918b598a671c8715d519c4103b14cae28502dee`, its ultra-entry cases used
`4877818ca02a43006d61ba46022f0e17b709bdf3`, and its KuKuTool-switch cases used
`ddf2f80f39130d9ea634b8fa9d41982b16240061`.

Use the existing application interpreter, in a checkout without `.env`:

```sh
python manage.py test contracts --noinput
node --test contracts/*.test.js
python -m unittest discover -s scripts/tests -p 'test_merge_guards.py' -v
```

The `contract-tests` CI job installs Nginx and runs these commands. INV-08 starts
real Django and Nginx on ephemeral loopback ports, never port 8000. It tests every
declared public `/media/` route with GET/POST and records actual Django ingress,
not just HTTP status. Local absence of Nginx produces two explicit skips; absence
in CI is a failure. `CONTRACT_NGINX_BINARY` can point to an existing executable.

## Boundaries

- Real HTTP routes, admission, scheduling, candidates, templates, download consumers
  and frontend controllers are retained. Only upstream HTTP/DNS, isolated lock storage,
  and browser platform boundaries are replaced. Local database connections remain real.
- INV-01 drives the anonymous homepage parse and the continuation poll in real time:
  the admin budget is 1.0 s (its minimum) and the fake Native answers after 0.8 s.
  Native, TianTian and the KuKuTool sidecar are faked at the HTTP boundary. The local
  KuKuTool Node runtime is declared absent, because that subprocess would bypass the
  boundary and reach the real network. The provider order is read from
  `DOUYIN_ULTRA_PROVIDER_ORDER`; there is no admin control for it yet.
- INV-01 also checks the ultra entry on the public Douyin page and a Douyin parse:
  with KuKuTool switched off in admin, ultra stays offered while TianTian in the
  configured order is enabled, and is not offered when no configured service is.
  The `KUKUTOOL_PROVIDER_ENABLED` environment switch turns off KuKuTool only: with
  it off, the page still offers ultra and the parse asks TianTian, and KuKuTool is
  never asked, even when it is first in the configured order.
- INV-02 uses TianTian through the Bilibili fallback as the representative recovery
  path. Timeout/disconnect are simulated after a POST send; they do not prove a remote
  terminal state. Recovery uses natural, bounded expiry without admin intervention.
- INV-03 checks highest default, exact alternative resource/token, SW streaming with
  a bound selection, legacy Worker messaging, and the existing unsupported-browser
  Blob compatibility path. Bilibili Native and service HTTPS complete files use
  the same browser-first route with their original authorization. Authorized
  ordinary HTTPS complete videos are not limited to a Provider or static CDN list.
  Customary UA/Accept/Referer/Origin and case-insensitive `Sec-Fetch-Mode` headers
  alone are not required access context. Known required cookies, authentication,
  custom headers, explicit `required_headers`, missing context and IP binding
  remain restrictive; this is not a blanket exception for `Sec-*`. HTTP
  resources keep the existing attachment path; no scheme is silently rewritten.
  The Worker executes generic HTTPS and safe-endpoint constraints after resource
  authorization, with CORS, redirect, MIME and size checks retained. The public
  Worker response includes its dependency as one update
  unit. Old-Worker URL rejection, registration errors and startup timeouts never
  silently select Blob; actual missing browser capability retains bounded Blob.
  Unknown registration, cancellation and media-response failures never trigger a
  second media request.
  Native attachment navigation now precedes Worker setup when the existing bounded
  metadata request's final adopted result observed a same-URL video attachment
  with a valid source filename and matching extension, under navigation-compatible
  request conditions; native attachment navigation does not require CORS.
  Ordinary metadata probes naturally populate short-lived positive and negative
  evidence keyed by URL and necessary context. If no valid evidence exists, the
  download action may use the same bounded probe for the selected video only,
  after admission, cost and rate limits. It does not probe the candidate list,
  extend authorization, or bypass per-hop server DNS/address safety. An existing
  parse-time fact is reused without another HEAD. SW response scope uses the initial
  host plus the adopted probe's observed final host, bounded by evidence and candidate
  expiry, in the existing wire policy. No platform family or empty-policy exception
  authorizes redirects; absent evidence still allows a same-host transfer.
  The frontend retains the selected URL,
  with no fetch/Blob/Worker call and no completion claim. The fallback uses the
  existing Worker under its download-only scope, independent of an old root page
  controller; the wire shape and old in-flight tasks are retained. Prepared tasks
  retain their owning Worker through a bounded message lifetime, handed to fetch
  on navigation; task messages remain with that owner. Cancellation, the original
  earlier authorization expiry and preparation failure release it without retry.
- INV-04 uses both Safari and Chrome UA fixtures and the real attachment controller.
  This is not a new real-browser save, disk-persistence proof, or historical Worker
  binary run. YouTube merge and every platform/CDN are not exhaustively covered.
  Controlled-clock slow transfers also cross the former 15-second observation and
  60-second launch boundaries: status survives actual progress, launch remains
  one-use/expired, and downstream closure is distinct from an upstream failure.
- INV-06 includes raw Douyin captions through the public result and nondefault
  selection endpoint: short title, description and hashtags remain separate.
  Native attachment filenames may use a reasonable source basename; controlled
  filename paths retain the short-title rule. `filename*` has strict priority:
  an unknown charset or invalid extended value cannot fall back to `filename`
  to assert native eligibility. The decoded extension must match response MIME.
  A synthetic .bin counterexample is
  not evidence that a live Douyin CDN returned that filename.
- INV-07 checks anonymous, ordinary authenticated and Staff presentation. Public
  authorization attributes remain available for legitimate selection actions.
- INV-09 checks actual feedback quotas and public Bilibili gray admission, including
  spoofed XFF, X-Real-IP, Forwarded and User-Agent inputs.

## Recorded Counterchecks

Product mutations were made only in an isolated `git archive` copy with no `.env`.
Each was restored after the targeted test. Failures below were assertion failures,
not syntax/import errors. The Nginx countercheck is also an executable CI test.

| Contract | Isolated mutation | Observed failure |
| --- | --- | --- |
| INV-01 | Unchanged `f918b59` (Native after the preferred window; order rewritten) | Native first asked at 1.15 s, after the 1.0 s budget; order `kukutool,tiantianjiexi` called only TianTian |
| INV-01 (ultra entry) | Unchanged `4877818` (ultra entry follows KuKuTool health only) | KuKuTool off in admin, TianTian configured: Douyin page `data-ultra-hd-available="false"`; `ultra_hd=1` parse showed only Native and never asked TianTian |
| INV-01 (KuKuTool switch) | Unchanged `ddf2f80` (`KUKUTOOL_PROVIDER_ENABLED` inside the Douyin ultra capability gate) | Switch off, TianTian configured: Douyin page did not offer ultra; with order `kukutool,tiantianjiexi,native` the `ultra_hd=1` parse asked no service (`[]`) |
| INV-02 | Failure cache retained for 24 hours | Failure, timeout and disconnect could not recover into usable public HTML |
| INV-03 | Choose lowest default; force bound selections to Blob; require Worker v4 | Wrong default; no SW navigation; incompatible legacy registration |
| INV-03 (browser first) | Unchanged `c26453c` through public Bilibili parse and action routes | Native auto route was `proxy`; service HTTPS selected download returned 409 instead of a direct descriptor |
| INV-03/06 (generic delivery) | Unchanged `7ab8296`, with only the new normal-entry contract copied into an isolated archive | Three authorized default-header/outside-list cases returned 409; the admission and required-context rejection cases still passed |
| INV-04 | Premature saved copy; attachment `DENY` instead of `SAMEORIGIN` | Controller rejects premature completion; both UA response checks reject framing |
| INV-04 (transfer observation) | Unchanged `8e8beda` | Three-minute observation ends as `poll_timeout`; slow HTTP transfer status returns 410 after 60 s; downstream close reports `UPSTREAM_CONNECTION_ERROR` |
| INV-05 | Remove fallback; call service before Native; bypass admission; allow 404 fallback | Failure classification, order and permission assertions fail |
| INV-06 | Retain `.bin`; remove caption transmission or short-title projection | Wrong extension; placeholder or description/hashtags enter selected filename |
| INV-07 | Unconditionally show technical panel | Anonymous/member presentation assertions fail |
| INV-08 | Remove `/media/service-selected-format/` from temporary snippet | HTTP 404 and no Django ingress; countercheck expects this failure |
| INV-09 | Trust leftmost XFF | Spoofed quota becomes 201 instead of 429; gray decision changes |

The original counterchecks did not change either frozen Worker/protocol file.
The Human-approved browser-first correction updates the existing protocol URL
policy in one PR, without changing the wire shape or requiring a version handshake.
Merge-guard event replays are local API doubles, not actual labels or branch protection.
