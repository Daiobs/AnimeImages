# Upstream Platform Coverage

## Status

**Real acceptance: PARTIAL.** The implementation provides controlled discovery,
exact-extractor parsing and resource admission for additional platforms through
`yt_dlp_generic` and `gallery_dl_generic`. It does not establish that all listed
extractors or platforms work. In the audited additional-platform sample cohort,
**valid deliverable successes = 0; successful browser downloads = 0**.

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
new path makes 1417 additional extractor entries routable and locally bulk-enabled
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
  implementation is credential-free; Blogger's embedded API key blocked its smoke.
- yt-dlp preserves raw formats but delivery requires a complete direct video;
  separate audio/video tracks and HLS/DASH are not supported by this extension.
  gallery-dl accepts direct image/video messages, not queued extraction jobs.
- Product admission validates public HTTPS candidates, issues exact-resource
  manifests with short-lived authority and limits assets. Browser CORS/source
  restrictions can still prevent saving; no automatic server-download fallback
  is authorized by this path.
- Access requires provider/runtime and request gates. Public allowlists do not
  override explicit blocks. A coverage row means an attempt may be allowed, not
  that the site has passed a real smoke test.

## Real Probe Evidence

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
five unrelated results as successful images. Browser download success remains
unproven, not a measured claim that every browser attempt fails.

## Local Product Controls

Use the homepage's **Platform coverage** link (`/?coverage=1`) to search installed
extractors and current access. Existing Admin Provider Settings has two bulk
checkboxes using the existing public platform allowlist:
`upstream:yt_dlp_generic` and `upstream:gallery_dl_generic`.
Neither is enabled by a schema/default change. Existing environment/provider and
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

## Remaining Provider Blockers

| Provider | Potential increment | Why not connected to this extension |
| --- | --- | --- |
| ParseHub | Native image/mixed posts on Coolapk, Tieba, Weibo, Xiaoheihe and Zuiyou; Pipix media; article media on Douban, Weixin and Zhihu; Threads and Snapchat candidates | Product worker only accepts `xhs_images` with exact-note authority. Broad Lab worker emits a Lab contract and lacks exact-platform matching/product resource authority. Needs a narrow single-post bridge, source/redirect admission, image/direct-video mapping and real validation. MIT alone does not establish integration readiness. |
| videodl | Dedicated video/news clients such as AcFun, CCTV, PearVideo and Meipai | Noncommercial local Staff Lab boundary; no automatic product promotion. Exact-backend policy and sharing consent must remain enforced. Worker emits video/cover/audio evidence, not a general image-post delivery contract. |
| TianTian / KuKuTool | Possible service-side image/video breadth | Existing platform/provider and media-host allowlists do not authorize arbitrary newly matched sites. No reviewed new-platform service inventory or new-host sharing mechanism was established. TianTian requires account credentials; neither service was called. Dynamic routing deliberately does not add service fallback. |

ParseHub requires `parsehub==2.2.0` at
`144260f4349c5bb57415f7555a572fcd48c3b8ba`. videodl requires distribution
`videofetch==0.9.1` at `6cd1de0fe1209380b183b654c65e98a629637873`, with the
PolyForm Noncommercial license marker. The documented example isolated runtimes
were absent during the audit; no alternative credential-bearing configuration
was inspected, so current installations were not verified.

videodl's reviewed policy has 60 allowed, one sharing-confirmation-required and
nine blocked dedicated backends; common backends have 21 confirmation-required
and 16 blocked entries. `KuKuToolVideoClient` requires explicit sharing consent;
unknown IDs fail closed. Product rollout permission is not that consent.

Acceptance remains PARTIAL until a genuinely new single-item source produces
identity-correct, admitted media and a separately observed browser save. Preserve
failed-stage evidence and do not relax exclusions merely to obtain a success.

## Source Pointers

- `tools/upstream_match_worker.py`: inventory, exclusions, exact execution and guards.
- `xhs_download/services/upstream_matching.py`: runtime identity and bounded parsing.
- `xhs_download/services/upstream_coverage.py`: request-aware coverage presentation.
- `xhs_download/services/provider_access.py`: allow/block and access gates.
- `xhs_download/services/upstream_delivery.py`: normalization and resource authority.
- [Provider Lab](provider-lab.md) and [ParseHub capability matrix](research/parsehub/03_CAPABILITY_MATRIX.md): pinned upstream boundaries.
- Official version fixtures: [Blogger](https://github.com/mikf/gallery-dl/blob/v1.32.4/test/results/blogspot.py), [Bluesky](https://github.com/mikf/gallery-dl/blob/v1.32.4/test/results/bluesky.py), [Mastodon](https://github.com/mikf/gallery-dl/blob/v1.32.4/test/results/mastodon.py), [Wikimedia](https://github.com/mikf/gallery-dl/blob/v1.32.4/test/results/wikimediacommons.py).
