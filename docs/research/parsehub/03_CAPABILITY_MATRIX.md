# ParseHub Capability Matrix

The complete machine-readable matrix is
[`capability_matrix.json`](capability_matrix.json). It contains 28 rows: eight
base modules and 20 platforms. Every row records overlap/gap, value, maturity,
test/live evidence, authentication, Cookie/proxy/signing/dependency/redirect/
SSRF risk, media-host discovery, result compatibility, Browser Streaming and
zero-relay compatibility, Public/Staff suitability, license impact, priority and
the next proof required.

## Classification totals

| Classification | Count |
| --- | ---: |
| `ADOPT_CANDIDATE` | 3 |
| `REFERENCE_ONLY` | 3 |
| `DEMAND_DRIVEN` | 12 |
| `ALREADY_STRONGER_IN_ANIMEIMAGES` | 4 |
| `SECURITY_REVIEW_REQUIRED` | 4 |
| `REJECT_DIRECT` | 2 |
| **Total** | **28** |

## Base modules

| Module | Primary gap/value | Classification | Priority |
| --- | --- | --- | --- |
| Facade | Bypasses AnimeImages governance and exposes direct result/download behavior | `REJECT_DIRECT` | P3 |
| BaseParser registry | Explicit AnimeImages registry and admission are stronger | `ALREADY_STRONGER_IN_ANIMEIMAGES` | P3 |
| Provider API layer | Useful separation and platform knowledge; no common local safety contract | `REFERENCE_ONLY` | P2 |
| Result/MediaRef model | LivePhoto pairing, animation, multimedia and rich text | `ADOPT_CANDIDATE` | P1 |
| Local downloader | Conflicts with ordinary-media zero-relay architecture | `REJECT_DIRECT` | P3 |
| yt-dlp runtime | Process-group cancellation lifecycle fills a bounded operational gap | `ADOPT_CANDIDATE` | P0 |
| CLI/config | Parse/download proxy separation is useful for tooling only | `REFERENCE_ONLY` | P2 |
| Tests/CI/release | Useful maintenance pattern; no product code gap | `REFERENCE_ONLY` | P3 |

## Code-derived platform inventory

All 20 rows are present in the README table, `Platform` enum and registered
parser modules. There are no README-only or code-only platform identities. The
parser package's static `__init__` imports are not the registry source of truth:
`BaseParser.get_registry()` walks and imports all parser modules dynamically.

| Platform | Parser / provider API | URL, short-link and parameter facts | Content/model and fallback | Auth/proxy evidence | Classification |
| --- | --- | --- | --- | --- | --- |
| Bilibili | `bilibili.py` / `bilibili.py` | video, opus, numeric dynamic IDs; `b23.tv`, `bili2233.cn`; keeps `p` | native video/dynamic plus yt-dlp; image and LivePhoto pairs | optional Cookie; proxy | `ADOPT_CANDIDATE` |
| Coolapk | `coolapk.py` / `coolapk.py` | feed and picture routes | native image, Ani, multimedia, rich text | proxy; login unverified | `DEMAND_DRIVEN` |
| Douban | `douban.py` / `douban.py` | topic/group/app dispatch; `douc.cc` | native video/photo/rich text | Cookie-capable provider; proxy | `DEMAND_DRIVEN` |
| Douyin | `douyin.py` / `douyin.py` | content routes; `v.douyin`, `iesdouyin`; keeps `modal_id` | Cookie Web API then signed mobile/device path; video/image/LivePhoto/Story | optional Cookie, device state, proxy | `ALREADY_STRONGER_IN_ANIMEIMAGES` |
| Facebook | `facebook.py` / none | watch/share/video/reel routes | yt-dlp video | optional external-tool Cookie/proxy | `DEMAND_DRIVEN` |
| Instagram | `instagram.py` / `instagram.py` | post/reel/share routes | native video/image/multimedia | Cookie/login/region risk; proxy | `SECURITY_REVIEW_REQUIRED` |
| Kuaishou | `kuaishou.py` / `kuaishou.py` | web/mobile/live routes; short share routes | native API plus HTML/structured-state fallbacks | Cookie/proxy; embedded session-material blocker | `SECURITY_REVIEW_REQUIRED` |
| Pipix | `pipix.py` / `pipix.py` | H5 share/item routes | native video/image | proxy; auth unverified | `DEMAND_DRIVEN` |
| Snapchat | `snapchat.py` / none | profile/spotlight content routes | yt-dlp video | external-tool auth/proxy dependent | `DEMAND_DRIVEN` |
| Threads | `threads.py` / `threads.py` | post/share routes | public API then Cookie fallback; ordered multimedia | Cookie for restricted content; proxy | `SECURITY_REVIEW_REQUIRED` |
| Tieba | `tieba.py` / `tieba.py` | numeric post routes | native/API-HTML video/image/Ani | proxy; auth unverified | `DEMAND_DRIVEN` |
| TikTok | `tiktok.py` / `tiktok.py` | content routes; `vt.tiktok` | Web API video/image | Cookie/signing/redirect risk; proxy | `DEMAND_DRIVEN` |
| Twitter/X | `twitter.py` / `twitter.py` | X, Twitter and fixupx status routes | native video/image/Ani/multimedia/rich text | Cookie/auth context; proxy | `SECURITY_REVIEW_REQUIRED` |
| Weibo | `weibo.py` / `weibo.py` | mobile/video/web post and app-share routes | native video/image/Ani/LivePhoto/multimedia | Cookie-capable provider; proxy | `DEMAND_DRIVEN` |
| Weixin | `weixin.py` / `weixin.py` | public article routes | HTML rich text and inline images | no parser Cookie declaration; proxy | `DEMAND_DRIVEN` |
| XHS | `xhs.py` / `xhs.py` | Xiaohongshu/xhslink; removes `xsec_token` after parse | page-state video/gallery/LivePhoto, raw-image construction | Cookie/proxy; identity/false-positive risk | `ALREADY_STRONGER_IN_ANIMEIMAGES` |
| Xiaoheihe | `xiaoheihe.py` / `xiaoheihe.py` | app/link board routes | native video/image/Ani/multimedia/rich text | Cookie-capable provider; proxy | `DEMAND_DRIVEN` |
| YouTube | `youtube.py` / none | video/music routes; excludes live/post/channel; preserves playlist `list` | yt-dlp video/music | optional external-tool Cookie/proxy | `ALREADY_STRONGER_IN_ANIMEIMAGES` |
| Zhihu | `zhihu.py` / `zhihu.py` | pin/question/article routes | native/API-HTML rich text, multimedia, images and video | Cookie-capable provider; proxy | `DEMAND_DRIVEN` |
| Zuiyou | `zuiyou.py` / `zuiyou.py` | hybrid share post with `pid` | native video/image/multimedia | proxy; auth unverified | `DEMAND_DRIVEN` |

## Reconciliation findings

- **SOURCE-DERIVED FACT:** the enum and registered platform parser counts are 20.
- **SOURCE-DERIVED FACT:** dedicated `provider_api` modules total 17. Facebook,
  Snapchat and YouTube intentionally delegate to the yt-dlp base.
- **SOURCE-DERIVED FACT:** every code platform appears in the README table; the
  README heading's `17+` wording is less precise than its own 20 rows.
- **SOURCE-DERIVED FACT:** a parser file or README row is not live-reliability
  evidence. All supplier platform availability remains `UNVERIFIED`.
- **ANIMEIMAGES FACT:** dedicated XHS, Douyin and Bilibili adapters plus gated
  generic rules already cover the product's current platform boundary.

## Highest-value supply capabilities

1. yt-dlp process-group cancellation and cleanup lifecycle.
2. Rich media domain concepts: LivePhoto pairing, animation, multimedia and
   rich text.
3. Bilibili Dynamic/LivePhoto platform knowledge.
4. Platform breadth as demand-discovery evidence, not automatic rollout.
5. Parse/download proxy intent separation for offline/admin tooling.

## Highest security risks

1. Kuaishou embedded Cookie-like session material.
2. Douyin mobile signing, registration and device-pool state.
3. Cookie/auth-dependent Instagram, Threads and Twitter API paths.
4. Dynamic registry plus server-side redirect following without the central
   AnimeImages host/IP admission contract.
5. Direct URL/header serialization and local/server downloader behavior.

## Highest dependency costs

1. Full package facade and its complete dependency graph.
2. Douyin mobile signing/device implementation.
3. Native rich-social providers using HTML, crypto/signing and media libraries.
4. yt-dlp-backed platform extraction as a runtime dependency (already separately
   governed in AnimeImages).
5. Rich-text/article conversion dependencies for Weixin/Zhihu/Coolapk.
