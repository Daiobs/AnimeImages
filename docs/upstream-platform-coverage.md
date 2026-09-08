# Upstream Platform Coverage

## Status

**Wave 2: parser increment with delivery limits; original new-site automatic
delivery target remains unmet.**
PR #87's three-site browser delivery below is historical accepted evidence, not
new Wave 2 success. PR #87 is merged; this follow-up remains a separate Draft.
The implementation provides controlled discovery,
exact-extractor parsing and resource admission for additional platforms through
`yt_dlp_generic`, `gallery_dl_generic` and an isolated `parsehub_generic` bridge.
It does not establish that all listed extractors or platforms work. On 2026-09-08,
ImgBB, PeerTube and Tieba each produced a valid browser-saved file from a real
content-page input. All three are outside the original 11 rules. Tieba also passed
the anonymous Batch path. Earlier unsuccessful cohorts remain recorded below.

## What Counts Mean

| Level | Meaning | Not evidence of |
| --- | --- | --- |
| Raw total | Installed upstream extractor classes, including established routes and excluded classes | Unique sites or newly supported platforms |
| Eligible | Classes admitted by local filtering for dynamic single-item matching | A match for every URL, enabled access or successful parsing |
| Enabled / allowed | Runtime readiness, provider switches, request access and allow/block policy permit an attempt | Upstream availability or media delivery |
| Real parse | An exact matched extractor returns usable raw metadata/candidates within its budget | A normalized, complete downloadable resource |
| Download | Admitted resources actually save through the browser | Something proved by matching, parsing or manifest issuance alone |

Local installed-worker snapshot re-read on 2026-09-07:

| Executor | Observed package version | Raw total | Eligible |
| --- | --- | ---: | ---: |
| yt-dlp | `2026.3.17` | 1871 | 1287 |
| gallery-dl | `1.32.4` | 977 | 130 |

These counts can change with installed packages and local exclusions. Read the
current worker snapshot rather than treating these numbers as a support promise.
The current count model is `total = eligible + already_routed + other_excluded`;
`excluded = already_routed + other_excluded`. Enabled counts are request-specific
and were not established by this snapshot. Before this change, the third-party
adapter had 11 hand-maintained platform rules and no dynamic matcher route. The
initial snapshot made 1417 additional extractor entries routable and locally bulk-enabled
for attempts, not 1417 independently verified sites. Unique platform count is not
reliably deduplicated. Existing rules and the four mature routes remain in place.

## Implementation Boundary

- Inventory and URL matching run offline in an isolated worker. Matching binds
  the provider, extractor ID and package version; parse revalidates that match.
  Cache identity includes executor/package evidence, not just the URL.
- Established platform routes retain precedence and their extractor families are
  excluded from new dynamic coverage. Generic, collection and delegation paths
  are filtered or rejected. Name/subcategory filters remain heuristics; returned
  content must still satisfy the single-item contract.
- Parsing is bounded, anonymous and parse-only: no user cookies, browser-cookie
  import, proxies or downloader invocation. Network guards reject non-public
  destinations and disallowed execution. This is not a claim that every upstream
  implementation is credential-free; the earlier Blogger probe was not executed
  because the embedded key's authorization context was not established. A key/token
  identifier alone does not establish whether a value is a user credential.
- yt-dlp now applies native `process_video_result(download=False)` after exact
  single-item extraction, with an explicit format selection that does not invoke
  an external merger. Original track fields are retained separately from upstream
  derived defaults. Delivery requires a complete direct video;
  separate audio/video tracks and HLS/DASH are not supported by this extension.
  gallery-dl accepts direct image messages, not queued extraction jobs. Videos
  lacking complete-track evidence are not promoted to complete downloadable media.
- At most one unknown-track HTTPS MP4 receives a child-only metadata probe:
  at most two Range requests, 128 KiB per request, five seconds within the parse
  deadline, no redirects and no full media transfer. A structurally complete
  `moov/trak/mdia/hdlr` table can establish audio/video presence; it does not invent
  codec names. Explicit upstream `none` still wins. Unknown tracks, explicit split
  tracks, streaming formats and missing candidates have distinct safe errors.
- Product admission validates public HTTPS candidates, issues exact-resource
  manifests with short-lived authority and limits assets. Browser CORS/source
  restrictions can still prevent saving; no automatic server-download fallback
  is authorized by this path.
- Access requires provider/runtime and request gates. Public allowlists do not
  override explicit blocks. A coverage row means an attempt may be allowed, not
  that the site has passed a real smoke test.

## Initial Probe Evidence (Historical)

Earlier results below are the reported audit outcomes, not rerun for this doc.
The last four-candidate follow-up used official gallery-dl `v1.32.4` fixtures,
local SQLite/LocMem isolation, disabled mail and service providers, and bypassed
local environment-file reading. No media was downloaded.

| Sample family | Observed outcome | Acceptance interpretation |
| --- | --- | --- |
| Streamable, Dailymotion, Vimeo, Artstation, Pinterest | Bounded execution timeout | No valid deliverable evidence; not a browser failure |
| Unsplash, Pexels | Upstream access denied | No valid deliverable evidence |
| Coub / AcFun | Raw extraction succeeded; no complete direct HTTP video candidate | Raw success only, not delivery success |
| Meipai | No formats | No usable result; sample staleness is not independently established |
| PearVideo | Worker extraction error | No usable result; the retained fixed code does not establish the upstream root cause |
| Wikiart | Five items came from a search, not the requested single artwork | Invalid false positive; `WikiartImageExtractor` now explicitly excluded |
| Blogger post | `SKIP_EMBEDDED_API_KEY` | Not executed under the no-credentials constraint |
| Bluesky post | `PARSE_FAILED` after a real bounded parse attempt | Failed before normalization; no finer cause established by the retained summary |
| Mastodon status | `NO_ADMITTED_MATCH` | Current `status` subcategory is not eligible; not a network failure |
| Wikimedia file page | `NO_ADMITTED_MATCH` | Current article/file extractor is not eligible; not a network failure |

The follow-up stopped after four candidates; only Bluesky reached real parsing.
The first cohort covered 12 distinct site families; the follow-up considered four
more but skipped three before network parsing. Pinterest matched both executors,
which were tested separately in the research probe. Product requests retain a
single 30-second total budget and at most two sequential provider attempts.
Do not count skipped/unmatched candidates as upstream parse failures, or Wikiart's
five unrelated results as successful images. At that earlier checkpoint browser
download success was unproven, not a measured claim that every browser attempt fails.

## Compatibility And Real Delivery (2026-09-08)

Native and wrapped parse-only calls used the same machine, sample, installed
version and time window, without user cookies, netrc or proxy inheritance. Calls
had bounded time/output budgets. Source links, response bodies and temporary media
URLs were kept outside Git; this table contains safe summaries only.

| Site | Native / wrapper evidence | Product / browser result |
| --- | --- | --- |
| Coub | Both return two audio-only and two video-only entries. Native format processing supplied IDs/extensions/protocols omitted by the earlier wrapper; corrected wrapper now agrees. No muxed candidate in this response. | Explicit separate-track limitation; not a browser or CORS failure |
| AcFun | Both return six HLS formats. Corrected wrapper preserves native IDs/order/format metadata. | Explicit streaming-format limitation; no direct MP4 was lost |
| ImgBB | Both return the same one-image result | Anonymous homepage saved JPEG, 141,666 bytes, 960 x 719; valid decoded image |
| PeerTube | Both return five direct MP4 candidates plus HLS; original codecs are unknown. One 131,072-byte Range of the selected candidate establishes both tracks. | Homepage saved 16,815,344-byte MP4, exactly matching Content-Length; local ffprobe finds H.264 1920 x 1080 and AAC, duration 114.063s; Chromium local-file playback advances without error |
| Tieba | Native ParseHub and bridge each return one image; bridge verifies response thread identity | Homepage and anonymous Batch each save a 218,055-byte JPEG, 440 x 1193, matching Content-Length |
| Pipix | Native and bridge each return one identity-bound video, track completeness unknown | Not issued as complete video; no browser success claimed |
| Zuiyou | Native returns no media; bridge reports `no_formats` | No false success |
| Wikimedia File | Previously blocked at matching; URL-scoped correction reaches extraction, then both native and wrapper encounter upstream HTTP failure | No delivery claim; article/category traversal remains denied |
| Mastodon status | Native bounded request aborts with no media; wrapper remains unmatched | No blanket status admission without usable identity evidence |
| TED | Both fail during extraction with TypeError under yt-dlp 2026.3.17 | A newer official JSON/NoneType fix exists; exact traceback equivalence and upgraded success not verified |
| Wistia | Both attempted variants fail | Not counted as successful |

The three successful sites exercise three upstream execution paths: gallery-dl,
yt-dlp and ParseHub. There are four successful product saves including the separate
Tieba Batch check. No opened playback tab, empty file, HTML response or manifest-only
result is counted. Page JavaScript errors were zero and server media download
endpoint requests were zero for each successful browser run. Media bodies were
fetched by Chromium; only the bounded video metadata Range runs in the worker.
No redirect/CORS workaround or server relay was introduced. These successes do
not erase the earlier failed cohorts or prove that every eligible extractor works.

Matching changes are deliberately narrow: Wikimedia's `File:` URL branch is
admitted, not arbitrary articles, categories or queues. Existing-family exclusions
remain conservative and may omit legal capabilities outside the old rules; they
are not a claim of complete platform coverage. No successful case required
delegation, so recursive/delegated execution remains rejected in this revision.

## Local Product Controls

Use the homepage's **Platform coverage** link (`/?coverage=1`) to search installed
extractors and current access. Existing Admin Provider Settings has three bulk
checkboxes using the existing public platform allowlist:
`upstream:yt_dlp_generic`, `upstream:gallery_dl_generic` and `upstream:parsehub_generic`.
None is enabled by a schema/default change. Existing environment/provider and
runtime access gates still apply. Explicit exclusions in the existing blocked
platforms field take precedence: `upstream`, `upstream:provider_id`,
`upstream:provider_id:extractor_id`, or `upstream:extractor:extractor_id`.

Single and Batch use the same admitted matches. New resources require the
existing route-only endpoint to confirm an exact issued resource before browser
fetch. Resource authority expires after 300 seconds or cache eviction; users
then reparse. HTTPS, CORS and no-redirect fetch are required. Ordinary videos use
the existing bounded browser Blob path; images use the existing browser archive.
No Service Worker policy or mature-platform transport was expanded. The extension
rejects server save, prepare and server ZIP rather than silently relaying media.

Local Chromium acceptance used a separate SQLite database and cache with real
mail disabled. Normal Admin login, bulk enable/disable, exact-extractor exclusion,
host exclusion and disabled runtime precedence were exercised and original local
settings restored. A real AcFun source reached extraction through both anonymous
homepage and Batch and displayed the controlled unsupported-format result, with
zero download actions and zero JavaScript errors. This proves product routing and
failure presentation, not a successful save. No production session was used.
Homepage, coverage and Batch were checked at desktop/mobile widths. The Provider
Settings form's label/grid collision was corrected within that template; browser
checks at 1440/768/390/320 pixels found no page-level horizontal overflow, with
wide tables scrolling inside their container. The final scoped template checks
passed 22 tests, and the temporary local account was removed.

Local regression on 2026-09-07/08: new focused Django 71, complete
`xhs_download` 2183, whole-project Django 2189, and Node 256 passed. JavaScript
syntax, Django system check, migration dry-run and diff whitespace checks passed.
Static-version and presentation checks were repeated after the final style/version
adjustments (20 passed). No model, dependency or migration was added. An earlier
full run hit the existing fixed-window click-interval assertion; its isolated
recheck and subsequent full run passed without changing the rate-limit code.
The first remote run failed because the application requirements intentionally do
not install the optional upstream packages. CI now explicitly installs the two
audited versions above for offline contract tests; application requirements and
production runtime installation remain unchanged. Tests are not skipped when the
packages are missing from the test environment.

## Remaining Provider Blockers

| Provider | Potential increment | Why not connected to this extension |
| --- | --- | --- |
| ParseHub | This revision connects reviewed Tieba, Pipix and Zuiyou single-item entry points; other platforms remain outside this bridge | Tieba real image delivery verified. Pipix video completeness remains unknown; Zuiyou sample returns no media. Other platforms are not implicitly authorized by the Lab inventory. |
| videodl | Dedicated video/news clients such as AcFun, CCTV, PearVideo and Meipai | Noncommercial local Staff Lab boundary; no automatic product promotion. Exact-backend policy and sharing consent must remain enforced. Worker emits video/cover/audio evidence, not a general image-post delivery contract. |
| TianTian / KuKuTool | Possible service-side image/video breadth | Existing platform/provider and media-host allowlists do not authorize arbitrary newly matched sites. No reviewed new-platform service inventory or new-host sharing mechanism was established. TianTian requires account credentials; neither service was called. Dynamic routing deliberately does not add service fallback. |

ParseHub requires `parsehub==2.2.0` at
`144260f4349c5bb57415f7555a572fcd48c3b8ba`. videodl requires distribution
`videofetch==0.9.1` at `6cd1de0fe1209380b183b654c65e98a629637873`, with the
PolyForm Noncommercial license marker. Example runtimes were absent during the
initial audit. This correction bootstrapped a separate development ParseHub venv
at the exact pin, verified its distribution/direct-URL identity and performed the
real parse-only comparisons above. No production runtime was installed or accessed.

The bridge is opt-in with `PARSEHUB_UPSTREAM_ENABLED=False` by default and an empty
`PARSEHUB_UPSTREAM_PYTHON`. For a local experiment, prepare an isolated venv using
the existing [ParseHub bootstrap](../tools/parsehub_provider/README.md), then set
these two variables explicitly and allow `upstream:parsehub_generic` through the
existing Provider settings. Do not reuse the XHS flag to enable this bridge.
Matching verifies the exact runtime offline; parsing revalidates it in a new
bounded process. No ParseHub package is imported into the Django process.
Unconfirmed videos do not receive authority; usable images in mixed results are
retained with a visible limitation notice. Existing XHS behavior is unchanged.

videodl's reviewed policy has 60 allowed, one sharing-confirmation-required and
nine blocked dedicated backends; common backends have 21 confirmation-required
and 16 blocked entries. `KuKuToolVideoClient` requires explicit sharing consent;
unknown IDs fail closed. Product rollout permission is not that consent.

This revision meets the sampled three-site real-delivery target and remains Draft
for human review. HLS/DASH, unknown-track videos and the failed sites are not
promoted by that result. Preserve failed-stage evidence and exclusions.

Correction regression: focused upstream/mail/daily tests 211 passed, complete
`xhs_download` 2231 passed, whole-project Django 2237 passed and Node 257 passed
without skips or TODOs. Django check, migration dry-run, static JavaScript syntax
and whitespace checks passed. The new optional environment keys use the existing
`env_config` accessors; `settings.py`, Settings Guard, dependencies and migrations
are unchanged. Exact-head remote CI is recorded in PR metadata after pushing.
The task-owned 8001 service uses an isolated SQLite database/cache, an isolated
ParseHub runtime and disabled email delivery; the original 8000 service is untouched.

## Wave 2: Compatible Results And Public Share Inputs (2026-09-08)

Baseline: `d2e9ce04d8dc38574874cf33587486e6b1b3b549`. The independent local
worktree, application venv, SQLite database and cache leave the existing shared
8000/8001 worktree untouched. Local preview uses loopback port 8002 and has real
email delivery disabled. The existing exact-pin ParseHub runtime is read-only;
the Django process does not import it. CI installs that same pin in a separate
runtime and requires the real type tests to execute, not skip.

### Implemented Scope

- Add two specific ParseHub entries: Coolapk feed/picture and Douban group topic.
  Five bridge entries now means five attemptable entries, not five working sites.
- Accept trusted installed Image/Video/Multimedia/RichText result classes and
  legitimate subclasses with `isinstance`; reject arbitrary same-name objects.
  RichText contributes only its explicit media list, never rendered HTML or URLs
  recursively scraped from Markdown. Image order is retained. Animated MP4
  references remain video candidates, not mislabeled downloadable images.
- Douban checks the actual response topic ID. Coolapk requires exactly one
  `feed-detail` and a matching server-rendered App intent identity in script text;
  request URL echo alone, arbitrary page text and conflicting identities fail.
- Coolapk preserves the access parameters `s` and `shareKey` while parsing;
  known share tracking is ignored. Douban accepts a bounded explicit tracking-key
  set. Unknown query keys, identity-changing parameters, fragments, account/search
  paths and unreviewed short links are rejected. Existing share-text extraction
  and desktop/mobile Douban topic forms continue through the shared matcher.
  Short-link resolution is **not implemented** in this increment; `douc.cc`
  remains rejected rather than passed to an arbitrary recursive resolver.
- The original four mature routes and existing 11 rules keep priority. None of
  the six proposed candidate families was swallowed by those rules, so no
  fallback-on-error or platform-wide routing reorder was needed.
- ParseHub unknown-track MP4 candidates can use the existing single-candidate
  guarded BMFF probe within the same total deadline. Verified audio+video evidence
  permits existing browser delivery; absent/unknown tracks retain limitations.
  No full server media transfer, URL rewriting or new downloader is introduced.
- Batch preview failures receive a stable error instead of calling an undefined
  function; upstream archive errors retain the actual safe failure category,
  rather than always describing a CORS failure as an expired resource.

### Real Evidence And Remaining Gates

Native and wrapped calls used the same pinned package and local network, without
user login, cookies or proxy inheritance. Raw responses and private screenshots
remain outside Git; no real media URLs or account credentials are archived here.

| Candidate | Native / product result | Delivery / remaining limitation |
| --- | --- | --- |
| Douban | Response identity confirmed; native and wrapper each yield seven explicit body images; anonymous homepage and Batch show seven | Media HTTP 418 and `MissingAllowOriginHeader`; Chrome cannot read the response; no saved file |
| Coolapk | Eight images; native and wrapper agree; response App intent matches requested post and media order matches its detail container; anonymous homepage and Batch show eight | Media HTTP 200 but `MissingAllowOriginHeader`; Chrome cannot read the response; no saved file |
| Weixin | Fixed-package public sample returns HTTP 200 but lacks supported body container; native ParseError | No candidate delivered; not called CORS |
| Zuiyou | Additional public sample: exact post identity, three videos and six images | All nine returned URLs use HTTP; bridge rejects `insecure_media`, no fabricated HTTPS conversion or delivery |
| Pipix | Additional public sample: HTTP 200 with null item detail, native ParseError | No verified item payload; no browser delivery claim |
| Weibo | One final anonymous native call with Cookie explicitly removed returned HTTP 302 toward the login domain | Redirect not followed; no anonymous result or media delivery |
| Zhihu | Fixed implementation requires a login-cookie value | Not connected or tested with credentials |
| Xiaoheihe | Fixed implementation includes device-profile exchange and a generated cookie | Not promoted in this increment; no user credential acquisition |

The browser source/CDN failures above are separate from parser compatibility.
They do not authorize a server relay. ImgBB, PeerTube and Tieba remain prior-wave
regressions, not newly counted sites. Multi-image browser saving, new three-site
delivery, two newly connected ParseHub delivery paths and short-link acceptance
remain **unmet**. That initial Wave 2 result was **PARTIAL**. The subsequent human
scope adjustment below permits review of the parser increment with delivery
limits; it does not retroactively pass the original delivery target. One failed
sample also does not prove every resource from that site fails.

### Delivery-Limit Follow-Up (2026-09-08)

The review scope is now the usable parser increment, existing permitted resource
actions and accurate failure states, not further random platform expansion.
For each of Douban and Coolapk, one representative image came from the same
product parse, at local origin `http://127.0.0.1:8002`, in a fresh anonymous Chrome
context with no related-site login. Fetch and native navigation used the same
unchanged resource URL within the same short observation window. The product
already used omitted credentials, no-referrer and redirect-error fetch; adding
those same options again would not be a fix.

| Stage | Douban representative | Coolapk representative |
| --- | --- | --- |
| Parse | Seven ordered body images | Eight ordered body images |
| Inline preview | Not displayed | Not displayed; Chromium reports `ERR_BLOCKED_BY_ORB` |
| Web fetch | JavaScript receives TypeError, no readable response | JavaScript receives TypeError, no readable response |
| DevTools diagnostic only | HTTP 418, image/jpeg, missing applicable Allow-Origin | HTTP 200, image/jpeg, missing applicable Allow-Origin |
| Native top-level open | Same URL, HTTP 418, image cannot display | Same URL, HTTP 200, JPEG displays at 720 x 1561 |
| Native manual save | `MANUAL_SAVE_NOT_VERIFIED` | `MANUAL_SAVE_NOT_VERIFIED` |
| Web automatic save / ZIP | No successful file | No successful file |

Native opening is not file saving. Native save dialogs were not operated; neither
CDP response-body extraction nor an out-of-browser request was used to manufacture
a saved file. There is no new manual-save or automatic-save success to count.
Douban's top-level rejection means native saving must not be promised. Coolapk's
display success establishes a useful native-open action, not a verified save.

DevTools can observe a status that the page script cannot read. The user-facing
TypeError message therefore describes browser unreadability and possible network
or source restrictions; it does not assert confirmed CORS, expiration or HTTP 418.
Inline ORB, cross-origin script readability and top-level access are separate
observations. No site-wide permanent CORS classification is stored.

The homepage had a concrete preview bug: its failure retry appended `_retry` to
external image URLs. It now preserves upstream URLs and displays one stable
preview failure without automatic retries, matching Batch's existing protection.
Image previews already lacked an unnecessary crossorigin attribute; none was
removed as a supposed fix. No parser change is used to address media policy.

Existing raw-link permissions remain in force. Copy/open actions are not download
success, do not increment saved counts and do not automatically open multiple
tabs. Native navigation may follow source redirects and does not inherit fetch's
redirect-error guarantee. Source access and actual browser saving remain subject
to the source response. No relay, proxy, URL rewrite, thumbnail substitution,
canvas export or browser-security bypass is introduced.

Staff homepage now offers one open action; Batch reuses its copy/open controls.
Both obtain fresh single-image authorization through the existing route with a
Staff-only `resource_link` intent. Expired, revoked, substituted or tampered
resources fail closed, with no DOM-URL fallback. Pure link authorization does
not create a successful download log. Anonymous raw-link permissions are not
expanded. Homepage failures stop the busy progress state and retain a stable
preview placeholder without covering the selection control. All-failed ZIPs
retain the first safe failure category and explicitly report no ZIP saved.

Final local verification: 2275 whole-project Django tests and 271 Node tests
passed. Focused tests cover native link semantics, real signing-clock expiration,
revocation, tampering, inactive/non-Staff denial, unchanged download logging,
source order, bounded retry and cancellation. Checks and migration dry-run pass.

Offline Chrome regression separates two forms of evidence:

- Two actual HTTP loopback origins, with default browser security: six CORS/HTTP
  cases passed (200, 403 and 418 with/without CORS). Six current-template preview
  cases passed, including displayable images whose fetch is unreadable and ORB
  failure. Source parameters were preserved without retries.
- Synthetic HTTPS responses supplied by browser test interception: existing
  product automatic PNG and two-entry original ZIP produced browser download
  events and exact fixture bytes. Staff link operations used a synthetic
  authorization response and did not count as saves. These are intercepted
  offline regression results, not real HTTPS/CDN delivery or native manual-save
  evidence. The product still rejects insecure HTTP media.

An earlier offline self-signed HTTPS harness used a certificate-ignore option;
its results were discarded and temporary certificates removed. The final run
uses no certificate exception, insecure flag or trust-store modification. Real
site comparisons never used that harness. No APIRequest or CDP body extraction
was counted as browser saving.

The adjusted review status is
`WAVE2_PARSER_INCREMENT_READY_WITH_DELIVERY_LIMITS`, subject to the exact-head CI
recorded in PR #88. New automatic site/file saves remain **0 / 0**; original
three-site, multi-image automatic save and short-link goals remain unmet.

### Access And Rollout

The new entries share `parsehub_generic`, the existing bulk allowlist and explicit
exclusions. The bridge still defaults off and requires an independent runtime.
Deployments already allowing the entire ParseHub bridge will gain additional
attemptable entries after a future upgrade; this is not a per-site verified
download promise. This task neither deploys nor changes production flags, email,
Daily, TopicEye/Rardar or PR #62.

### Verification

Local complete `xhs_download`: 2252 passed; whole-project Django: 2258 passed.
Node: 262 passed without skips or TODOs. Final anonymous Batch browser regression
renders seven/eight images, has zero page JavaScript errors, no horizontal overflow
at 390px, and zero server media-download endpoint calls. Source failures remain
visible and are not labeled successful saves or automatically relayed.
The suites run with the optional product bridge off, while explicit bridge tests
enable it and real-type tests use `PARSEHUB_COMPAT_PYTHON`. An initial local run
inherited the preview's enabled bridge and failed two default-off inventory
assertions; rerunning with the intended isolated test gate passed without
changing their expectations. Separate child tests exercise eight installed-type
cases and thirteen source/response cases, without live network calls.

Django system check, migration dry-run and diff whitespace checks pass. There
are no model/migration/application-requirements changes. CI adds isolated pinned
ParseHub installation and the Batch preview-error Node regression. Exact-head
remote CI is recorded in the Draft PR after pushing.

## Source Pointers

- `tools/upstream_match_worker.py`: inventory, exclusions, exact execution and guards.
- `xhs_download/services/upstream_matching.py`: runtime identity and bounded parsing.
- `xhs_download/services/upstream_coverage.py`: request-aware coverage presentation.
- `xhs_download/services/provider_access.py`: allow/block and access gates.
- `xhs_download/services/upstream_delivery.py`: normalization and resource authority.
- `tools/upstream_metadata_probe.py`: bounded structural track evidence.
- `tools/upstream_parsehub_worker.py`: exact-pin single-item ParseHub boundary.
- [Official TED fix](https://github.com/yt-dlp/yt-dlp/pull/17510): later upstream JSON/NoneType correction; no blanket runtime upgrade performed here.
- [Provider Lab](provider-lab.md) and [ParseHub capability matrix](research/parsehub/03_CAPABILITY_MATRIX.md): pinned upstream boundaries.
- Official version fixtures: [Blogger](https://github.com/mikf/gallery-dl/blob/v1.32.4/test/results/blogspot.py), [Bluesky](https://github.com/mikf/gallery-dl/blob/v1.32.4/test/results/bluesky.py), [Mastodon](https://github.com/mikf/gallery-dl/blob/v1.32.4/test/results/mastodon.py), [Wikimedia](https://github.com/mikf/gallery-dl/blob/v1.32.4/test/results/wikimediacommons.py).
