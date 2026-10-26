# Provider 盘点（统一管线第 2 步 P1）

核对基线：`26bf3c1271e214c1a979ad39e7fc12db5c9dfcde`（#134），2026-10-25。
[目标原文](../goals/unified-pipeline-step2.md)和[统一管线契约](unified-pipeline.md)是规格；
本文件只是代码及现有证据的清单，不修改契约，不宣告任何平台已经切换。

**后续决定（2026-10-25）：** 以下是 #136 之前的盘点快照。Human 已以 #136 的契约第 9 节解除 D-01，
并明确跨进程 raw 传递归 P12、缓存无 raw 只记 `SHADOW_NO_RAW`。随后 Human 修正
videodl 范围：纳入 P7，生产开放状态不影响纯适配器交付，不启用任何后台配置。
执行以[进度日志](../goals/unified-pipeline-step2.md#进度日志)记录的最新决定为准，不再按下面的历史暂停状态停工。

**P7 原始边界（2026-10-26）：** ParseHub 通用读取子进程中的原生 ParseHub 对象
（委托分支复用 `dl.info_json` 的 P3 映射），小红书图片读取 `_extract` 内的页面状态，
videodl 读取 `observe(infos)` 的 VideoInfo。三者本步都只有 RawMapper 与录制测试；
fetch 和跨进程捕获留 P12，不读取旧 rows/items 或候选令牌，不新增请求。
videodl 的五份既有缩减录制不冒充完整录制；实际 backend 由调用方显式提供，
与原生 source 显示标签区分。新补充的受控构造均单独标注，不代表真实网络验收。
本步已补齐 4 份完整 ParseHub 历史原始录制（B站、抖音、两份富文本），
两份富文本与固定 Worker 的离线提取结果一致；小红书同项配对和委托 yt-dlp 以受控构造覆盖。
videodl 同一 VideoInfo 内的独立音频 URL 按原生下载器的明确配对语义填写，
不跨对象猜配；5 份缩减录制与 2 份合成录制的证据范围分别标注。

## P1 历史结论与暂停点

**P1 仅文档；P2 及之后尚未实施。发现 D-01 契约缺口，需 Human 决定后再继续。**

- **D-01：轨道未知没有模型表示。** [天天原始录制](../../xhs_download/fixtures/native_replay/40b6b3302653089b0ed07351.json)的
  `videos[].streams[]` 只有 URL、大小、质量标签；[Kukutool 原始录制](../../xhs_download/fixtures/native_replay/1d723e3f60f5f7b7634cd84a.json)同样没有音轨事实。
  [旧服务选择器](../../xhs_download/services/service_format_selection.py#L1)明确文件声明不能证明完整音轨，
  [既有测试](../../xhs_download/tests_service_format_selection.py#L82)断言 `audio_tracks=unknown`。
  [模型](../../xhs_download/services/pipeline/model.py#L19)的 `Tracks` 只有 `av/video_only/audio_only`，
  `Variant.tracks` 默认 `av`。不能根据 `.mp4`、`videos`、旧可下载状态或默认值补造完整音视频。
  QualityProbe 的大小、MIME、可达性也不是轨道证据。这里需要明确未知事实的表示/处理规则；
  临时返回 `Failure(invalid_result)` 可以暴露不可映射，但不能当作该 Provider 已适配完成。
  本 PR 不新增枚举、不改内核、不改既有契约测试，也不为补齐字段探测媒体。
- **原始数据可捕获，不等于当前返回值就是 raw。** 四类工具 Worker 内已有观察点，天天也已有
  `observe(payload)`；不能把“产品调用出口不保留 raw”夸大成“上游从未返回 raw”。
  但旧缓存中仅存投影时不能复原原始响应，更不能为影子命中缓存再发一次上游请求。
- **P3/P12 边界待明确。** yt-dlp 旧 CLI 可以返回 info dict；exact-worker 在子进程内保留了观察点，
  产品出口却裁剪字段。将该原始数据跨进程传回会涉及 L2 的
  `tools/upstream_match_worker.py`，不能因 P3 预期 L1 就改动它或复用旧候选。
  应由 Human 确认与 P12 捕获工作的分工后实施，风险仍由 protected-files 判定。
- 生产可达性未重新连接生产核验。下表区分已有运行证据、代码条件可达和停用。
  未核实的开关/路线不以默认值代替，不据此删除 Provider；videodl 是否纳入 P7 仍需确认现有生产准入。
  本次未打开后台、未登录 Staff、未修改任何配置。

## 口径与入口

- **已见**：2026-10-25 对生产 `834d331` 的既有验收记录确实观察到该调用；
  不表示所有作品均可用，也不把当时成功视为永久开放。
- **条件可达**：当前正式入口有该路径，受运行能力、后台模式、来源准入/路线约束；
  本轮没有新的生产配置快照，当前是否放行仍待核实。
- **停用**：已确认决定明确停用；不是因为一次失败而自动排除。
- 账号状态查询、readiness、下载 smoke 是检查入口，不是新的解析 Provider。
  `animeimages_current`、`xhs_current` 是组合基线；
  `mobile_feed/mobile_share` 是 Native 子入口；
  `native_enhanced/tiantian_first/ultra_race` 是旧编排身份，不另造适配器。
- `yt_dlp_existing/gallery_dl_existing/tiantian_existing/kukutool_existing/parsehub_repo`
  是 Lab 别名，执行身份见 [PROVIDER_ALIASES](../../xhs_download/services/quality_execution.py#L15)。
  `yt_dlp_merge/youtube_merge` 使用 yt-dlp 原始数据，是旧选流/合流派生身份，归 P3。
- 正常入口：[registry.parse_media_page](../../xhs_download/services/registry.py#L133)；
  三个专用平台在通用适配器之前匹配。通用成熟平台使用静态规则，额外来源使用已安装工具 registry、
  [共享执行器](../../xhs_download/services/quality_execution.py#L450)和
  [来源路线](../../xhs_download/services/upstream_routes.py#L74)。
  手工通用路线不覆盖已有成熟平台入口。
- 开关与准入不是一回事：[Runtime Gates](../provider-runtime-gates.md)。
  `public` 模式也不能代替匿名来源允许列表；Staff 成功不能推出匿名成功。
- 健康测试清单：[PROVIDER_HEALTH_TESTS](../../xhs_download/services/provider_health_tests.py#L93)；
  每日验证消费后台勾选项，不表示清单里所有 Provider 已启用。

## 专用 Provider × 平台

下表位置为本次基线的 `文件:函数:行`，不将旧 normalizer 当作新 map。
目录简称 `platforms/` 指 `xhs_download/services/platforms/`。

| Provider ID | 平台/内容 | 生产可达性及依据 | 上游调用与原始数据 | 旧 ParseResult 位置 | 计划 |
| --- | --- | --- | --- | --- | --- |
| native | 小红书图文、视频、Live Photo、富文本 | 正式首入口；既有生产记录有 Native 失败及服务接替，不能据此判为禁用；健康项 xhs_*_current | platforms/xiaohongshu.py:parse_url:799，847 取得 HTML；xhs_page_state.py:extract_xhs_page_state:741 内有 note 原对象 | xiaohongshu.py:parse_html:435，592/665 构造结果；图片分支还有探测/增强，不能直接作为纯 map | P10 |
| native | 抖音图文、视频、原声 | 条件可达；正式普通解析和超清保底；健康项 douyin_native_parse | platforms/douyin.py:fetch_mobile_feed_aweme:767 / fetch_mobile_share_aweme:817；返回 aweme 原始子树 | douyin.py:parse_aweme:684 | P9 |
| native | B站普通视频、动态 | 已见：生产匿名普通视频成功；动态是代码可达，未新验收；健康项 bilibili_native_parse | platforms/bilibili.py:api_get_json:335 返回 API data；_parse_url:648 收集 view/playurl；动态另走 bilibili_dynamic.py | bilibili.py:_parse_url:704；bilibili_dynamic_normalizer.py | P8 |
| tiantianjiexi | 抖音超清/图文 | 已见：生产匿名超清由天天成功；受 ULTRA_HD_PROVIDER_ENABLED、天天能力及 douyin_ultra 准入；健康项 douyin_tiantianjiexi_ultra | platforms/douyin_providers.py:DouyinTianTianJieXiProvider.parse:1178 → 天天共用 client，原始 JSON 见 R-TT | douyin_providers.py:result_from_payload:1376 / build_external_parse_result:905 | P5 |
| kukutool | 抖音超清 | 条件可达：KUKUTOOL_PROVIDER_ENABLED、ULTRA_HD_PROVIDER_ENABLED、后台超清模式/顺序；健康项 douyin_kukutool_ultra；本轮未确认运行开关 | platforms/douyin_kukutool.py:run_kukutool_helper:858 返回解密业务对象；run_kukutool_sidecar:1007 在 1030 取得 JSON 后转换 | douyin_kukutool.py:build_result:2043 → douyin_providers.py:build_external_parse_result:905 | P6 |
| tikhub | 抖音 | 停用：INV-01 明确 TikHub 已停用；保留代码不等于已启用 | douyin_providers.py:fetch_high_quality_data:2359 在 2413 读 JSON，并返回完整对象 | DouyinTikHubProvider.parse:2722 → build_external_parse_result | 不做；停用，不新增清单外 PR |
| tiantianjiexi_generic | B站普通视频第一分P兜底 | 条件可达：Native 合格失败后且获准；现有 B站允许列表语义；不能由 Native 成功推断本次调用天天 | platforms/bilibili_fallback.py:parse_fallback:96 → R-TT | bilibili_service_result.py:build_service_result:137，206 构造结果 | P5 |
| tiantianjiexi_generic | 小红书视频增强/独立服务 | 已见：生产匿名视频经天天取得；不同来源/独立服务准入不能相互外推；健康项 third_party_tiantianjiexi 与 xhs_*_current | xhs_tiantian_hybrid.py:enhance_xhs_ordinary_video:303；xhs_independent.py:parse_independent:140 → R-TT | hybrid:431；独立服务走 quality_execution.py:_execute_service:370 / _normalize_videodl_candidates:327 | P5 |
| kukutool_generic | 小红书独立服务 | 条件可达：THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED、第三方模式、独立来源准入；不能套用抖音 KuKuTool 开关 | xhs_independent.py:parse_independent:140 → kukutool_generic.py:parse:645 → R-KK | quality_execution.py:_execute_service / _normalize_videodl_candidates | P6（同一 KuKuTool 家族，不新增 Provider） |
| parsehub_xhs_images | 小红书图片增强 | 条件可达：PARSEHUB_PROVIDER_ENABLED 与 PARSEHUB_PROVIDER_PYTHON，依赖 Native 作品对应；区别于通用 PARSEHUB_UPSTREAM_ENABLED | platforms/xhs_parsehub_images.py:load_parsehub_worker_payload:359 / _run_worker:321；专用 worker 的 _extract:168 在 172-174 取得 HTML/data/note，之后才构造 items | xhs_parsehub_images.py:enhance_xhs_images_with_parsehub:726，_parsehub_asset:657 | P7 |

## 通用调用与 raw 边界索引

以下 R-* 索引用于后面的逐组合清单。上游库原生对象可以作输入；项目派生候选、manifest、token 不可以。
已有观察点是代码能力证据，不是本轮真实调用记录。

| 索引 / Provider | 最底层原始数据、调用位置 | 旧返回和 ParseResult 映射 | 计划 |
| --- | --- | --- | --- |
| R-YT / yt_dlp_generic | tools/upstream_match_worker.py:parse_ytdlp:475，process_video_result:566 的 observe:568 在裁剪前取得 extractor info dict；旧 CLI platforms/ytdlp_generic.py:run_ytdlp_json:41 在 70 解码 JSON | Worker 后处理/compact_descriptor:599/605 会裁剪；quality_execution.py:_execute_upstream:273 → upstream_delivery.py:normalize_upstream_result:295。静态路径 YtDlpResultNormalizer.normalize:422 | P3 |
| R-GD / gallery_dl_generic | tools/upstream_match_worker.py:parse_gallery:649，699 的 observe 取得原始 message；旧 CLI platforms/gallerydl_generic.py:run_gallerydl_json:68 | Queue 展开后才观察，716 裁剪 metadata；共享路径 normalize_upstream_result；静态 GalleryDlResultNormalizer.normalize:369 | P4 |
| R-TT / tiantianjiexi_generic | platforms/tiantianjiexi_generic.py:TianTianJieXiApiClient.parse_url:452，474 读取 JSON，490 observe(payload) 在 unwrap/normalize 前 | 返回及缓存已经是旧归一化 ServiceFormatPayload；静态 TianTianJieXiResultNormalizer.normalize:1367；共享 _execute_service:370 → _normalize_videodl_candidates:327 | P5 |
| R-KK / kukutool_generic | platforms/kukutool_provider.js:main:56，118 读取 JSON，123-126 解密并输出业务 result；douyin_kukutool.py:run_kukutool_url_helper:1086，1147/1162 解码并返回业务对象 | helper/payload cache 保留解密业务对象；静态 KukutoolResultNormalizer.normalize:799；共享 _execute_service → _normalize_videodl_candidates | P6 |
| R-PH / parsehub_generic | tools/upstream_parsehub_worker.py:parse:622，632 原生 ParseHub 对象、634 observe；normalize_result:199 的 220 读取 dl.info_json；委托 yt-dlp 的 info dict 在 601 | 产品出口是 rows 或富文本摘要；upstream_parsehub.py:parse_upstream_url → normalize_upstream_result；对象及 lazy info_json 不能从 rows 还原 | P7 |
| R-VD / videodl_repo | tools/provider_lab/runtime/workers/videodl_worker.py:run_exact_backend:190，222 parsefromurl，225-226 observe(infos) 取得 VideoInfo | normalize_result:286 只输出选定字段及 raw_shape；quality_execution.py:_videodl_payload:202 再变成 rows；_normalize_videodl_candidates:327 返回 ParseResult | P7，仅确认生产可达后纳入；不扩大许可 |

补充：

- `tools/upstream_census_worker.py:collect:57` 已使用工具 observe；
  `xhs_download/management/commands/capture_upstream.py:collect:24` 已捕获天天/Kukutool。
  它们会另发请求，**不能直接调用来做零额外请求的影子比较**。
- Capture 的脱敏/限深/限量形状是录制材料，不是完整 raw 接口。不得用 `raw_shape` 反推已经删除的字段。
- 天天旧 payload 缓存、已签发候选缓存，以及 XHS Native 的最终结果缓存，不持有原 envelope/HTML。
  P12 需明确这类命中的比较范围；不能重新请求、续签或从旧结果制造 raw。
- 小红书的 HTML、B站的多 API data、KuKuTool sidecar envelope 在旧调用中曾取得，但部分不随返回值暴露。
  需要在已有取数点捕获，不是重新编写网络客户端。
- yt-dlp BMFF 补充事实属于本项目探测，不得在 field_sources 记成上游原生事实。
  未探测的同作品其他格式不能继承轨道结论。
- ParseHub XHS 专用 worker 为 `tools/parsehub_provider/parsehub_xhs_image_worker.py`，
  `_extract:168` 通过 XHSAPI 取得 HTML 和页面 data，再精确选取 note；返回值只保留图片投影，
  不暴露原始 data/note。P7/P12 需从该现有取数点捕获，不能拿其生成的 items 充当原始数据，
  也不能因 R-PH generic 已有 observe 就认为专用路径已经覆盖。

## 通用 Provider × 平台清单

静态矩阵来自 `platforms/third_party.py:THIRD_PARTY_PLATFORM_RULES:97`。
每行均为**条件可达**：代码有尝试入口，仍需能力开关、后台模式、该来源准入；
本轮没有生产调用或当前开关快照，不声明当前匿名可用。
R-* 给出每行的上游调用、raw 形状及旧映射位置。

| Provider ID | 平台 | 生产依据/边界 | raw 与旧映射索引 | 计划 |
| --- | --- | --- | --- | --- |
| kukutool_generic | tiktok | 条件可达：静态规则；当前开放待核实 | R-KK | P6 |
| tiantianjiexi_generic | tiktok | 条件可达：静态规则；当前开放待核实 | R-TT | P5 |
| yt_dlp_generic | tiktok | 条件可达：静态规则；当前开放待核实 | R-YT | P3 |
| gallery_dl_generic | tiktok | 条件可达：静态规则；当前开放待核实 | R-GD | P4 |
| kukutool_generic | kuaishou | 条件可达：静态规则；当前开放待核实 | R-KK | P6 |
| tiantianjiexi_generic | kuaishou | 条件可达：静态规则；当前开放待核实 | R-TT | P5 |
| yt_dlp_generic | kuaishou | 条件可达：静态规则；当前开放待核实 | R-YT | P3 |
| gallery_dl_generic | kuaishou | 条件可达：静态规则；当前开放待核实 | R-GD | P4 |
| kukutool_generic | instagram | 条件可达：静态规则；当前开放待核实 | R-KK | P6 |
| tiantianjiexi_generic | instagram | 条件可达：静态规则；当前开放待核实 | R-TT | P5 |
| yt_dlp_generic | instagram | 条件可达：静态规则；当前开放待核实 | R-YT | P3 |
| gallery_dl_generic | instagram | 条件可达：静态规则；当前开放待核实 | R-GD | P4 |
| kukutool_generic | facebook | 条件可达：静态规则；当前开放待核实 | R-KK | P6 |
| tiantianjiexi_generic | facebook | 条件可达：静态规则；当前开放待核实 | R-TT | P5 |
| yt_dlp_generic | facebook | 条件可达：静态规则；当前开放待核实 | R-YT | P3 |
| gallery_dl_generic | facebook | 条件可达：静态规则；当前开放待核实 | R-GD | P4 |
| yt_dlp_generic | youtube | 条件可达：静态规则；当前开放待核实 | R-YT | P3 |
| kukutool_generic | youtube | 条件可达：静态规则；当前开放待核实 | R-KK | P6 |
| tiantianjiexi_generic | youtube | 条件可达：静态规则；当前开放待核实 | R-TT | P5 |
| kukutool_generic | x | 条件可达：静态规则；当前开放待核实 | R-KK | P6 |
| tiantianjiexi_generic | x | 条件可达：静态规则；当前开放待核实 | R-TT | P5 |
| yt_dlp_generic | x | 条件可达：静态规则；当前开放待核实 | R-YT | P3 |
| gallery_dl_generic | x | 条件可达：静态规则；当前开放待核实 | R-GD | P4 |
| kukutool_generic | imgur | 条件可达：静态规则；当前开放待核实 | R-KK | P6 |
| tiantianjiexi_generic | imgur | 条件可达：静态规则；当前开放待核实 | R-TT | P5 |
| gallery_dl_generic | imgur | 条件可达：静态规则；当前开放待核实 | R-GD | P4 |
| kukutool_generic | reddit | 条件可达：静态规则；当前开放待核实 | R-KK | P6 |
| tiantianjiexi_generic | reddit | 条件可达：静态规则；当前开放待核实 | R-TT | P5 |
| gallery_dl_generic | reddit | 条件可达：静态规则；当前开放待核实 | R-GD | P4 |
| kukutool_generic | tumblr | 条件可达：静态规则；当前开放待核实 | R-KK | P6 |
| tiantianjiexi_generic | tumblr | 条件可达：静态规则；当前开放待核实 | R-TT | P5 |
| gallery_dl_generic | tumblr | 条件可达：静态规则；当前开放待核实 | R-GD | P4 |
| kukutool_generic | flickr | 条件可达：静态规则；当前开放待核实 | R-KK | P6 |
| tiantianjiexi_generic | flickr | 条件可达：静态规则；当前开放待核实 | R-TT | P5 |
| gallery_dl_generic | flickr | 条件可达：静态规则；当前开放待核实 | R-GD | P4 |
| kukutool_generic | deviantart | 条件可达：静态规则；当前开放待核实 | R-KK | P6 |
| tiantianjiexi_generic | deviantart | 条件可达：静态规则；当前开放待核实 | R-TT | P5 |
| gallery_dl_generic | deviantart | 条件可达：静态规则；当前开放待核实 | R-GD | P4 |

动态来源不能用一份静态网站名单代替已安装 registry；以下每行保留动态范围，不把 extractor/backend 数量当平台数量。

| Provider ID | 平台范围 | 生产依据/边界 | raw 与旧映射索引 | 计划 |
| --- | --- | --- | --- | --- |
| yt_dlp_generic | 静态成熟平台外，已安装 extractor 精确匹配的来源（含既有 PeerTube 样本） | YTDLP_PROVIDER_ENABLED、版本准入、路由及来源准入；历史成功不是当前开放证明 | R-YT | P3 |
| gallery_dl_generic | 静态成熟平台外，已安装 registry 可执行的来源 | GALLERYDL_PROVIDER_ENABLED；排除 generic/directlink/recursive/ytdl/oauth 等执行器；具体开放待核实 | R-GD | P4 |
| tiantianjiexi_generic | 既有 service bridge 获准来源 | THIRD_PARTY_PROVIDER_ENABLED、TIANTIANJIEXI_PROVIDER_ENABLED、账号能力及来源准入；无本地完整支持网站目录 | R-TT | P5 |
| kukutool_generic | 既有 service bridge 获准来源 | THIRD_PARTY_PROVIDER_ENABLED、THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED、helper 能力及来源准入；非全站支持保证 | R-KK | P6 |
| parsehub_generic | 子进程 registry 精确匹配的来源 | PARSEHUB_UPSTREAM_ENABLED、固定 Runtime、来源路由；不能覆盖已路由的成熟入口；当前生产开放待核实 | R-PH | P7 |
| videodl_repo | 受控 Dedicated，或显式保存并授权的 Common backend | VIDEODL_PROVIDER_LAB_ENABLED、exact pin、backend 策略及来源授权；生产是否可达/许可尚未确认，不据此排除也不启用 | R-VD | P7 待确认 |
| youtube_merge | YouTube | YOUTUBE_MERGE_PROVIDER_ENABLED、单独模式/配额；既有生产匿名 YouTube 拒绝记录不等于 Staff 不可达 | R-YT，同一 payload；services/youtube_merge.py:build_youtube_merge_parse_result:454 | P3 |
| yt_dlp_merge | 通用 yt-dlp 分离轨内容 | YTDLP_MERGE_PROVIDER_ENABLED 及实际候选/上下文；不是新的原始 Provider | R-YT；platforms/ytdlp_merge.py:build_parse_result:294 | P3 |

ParseHub 固定版本的历史目录见[能力清单](../research/parsehub/03_CAPABILITY_MATRIX.md)；
其 Bilibili/Douyin/XHS 等供应能力不等于普通首页会绕过三个专用入口调用它。
videodl 的 70 Dedicated/37 Common 是固定策略的 backend 数，不是 107 个独立平台，也不是生产许可。

## 现有样本与待补范围

`xhs_download/fixtures/native_replay/index.json` 索引 15 份缩减录制（不含索引本身）：
yt-dlp 2、gallery-dl 3、天天 2、Kukutool 1、ParseHub 2、videodl 5。
URL/正文被替换、部分内部字段省略，不能用于实时请求，也不能据匿名化来源证明平台和真实 host_policy。

| Provider × 平台 | 已有材料 | 仍缺什么 |
| --- | --- | --- |
| yt-dlp × 通用视频/音频 | native_replay 的 421dcc…、88e9…；tests_ytdlp_runtime_compatibility.py、tools/tests_upstream_worker_quality.py | 明确 YouTube 归属且保留分轨/配对、请求上下文的完整 raw 录制 |
| gallery-dl × 通用媒体 | native_replay 的 5d6c…、3f3b…、232ed…；gallerydl_generic 及执行路径测试 | 按平台保留完整 metadata；3f3b…身份仍 pending，不当确认作品 |
| 天天 × 通用多档/图集 | 40b6b3302653089b0ed07351.json、e162b21c7cd6812ff941e9cc.json | 轨道事实缺失；来源已脱敏，不能证明每个平台均覆盖 |
| 天天 × B站 | tests_bilibili_service_result.py:REAL_RESPONSE:30，记录来源为真实响应、签名替换 | 不含轨道/大小的原生事实；不要把后续 probe 混为原生响应 |
| 天天 × 抖音/小红书 | tests_douyin_tiantian_gallery.py:79、tests_xhs_tiantian_hybrid.py:123 的受控数据 | 不是本轮新鲜真实录制；适配实施时仍需按目标核对字段覆盖 |
| Kukutool × 通用多档 | 1d723e3f60f5f7b7634cd84a.json | 轨道未知；平台归属脱敏 |
| Kukutool × 抖音/小红书 | test_douyin_kukutool.py、独立服务及归一化测试中的 helper/sidecar 替身 | 不能把受控对象当两平台真实全字段证据 |
| ParseHub × 富文本/图集/实况/视频 | native_replay 的 b571…、2fb71…；tools/parsehub_lab/fixtures 下 supplier.json；real-types 测试 | 完整 dl.info_json、真实配对和每平台上下文；animeimages.json 是旧比较端，不作 map 输入 |
| videodl × Dedicated/Common | native_replay 五份 VideoInfo 缩减录制，含 MP4/HLS/失败；tools/provider_lab/fixtures/source_cases.json | 生产可达性待确认；raw_shape 不完整；source_cases 是来源目录，不是上游响应 |
| Native × 小红书 | tests_xhs_media_fidelity.py 的 HTML/note 构造器；test_fixtures/xhs_generic_shell.html、xhs_rich_media_cases.json | 受控构造与真实录制分开；完整 HTML/note 及 Live Photo 事实不能从最终资产反推 |
| Native × 抖音 | tests.py:3598 起的 aweme 对象及移动端边界替身 | 保留原声、完整格式与失败形态的原始录制待核实 |
| Native × B站 | tests_bilibili_native_session.py:35 起的 view/playurl；test_fixtures/bilibili_dynamic_cases.json | 受控 API 数据不是本轮真实录制；分轨、多段 durl 不由最终错误摘要还原 |
| TikHub × 抖音 | tests.py:2643 起的受控 JSON | 已停用，本目标不追加录制 |

不为 P1 采集新样本。后续如确需录制，按目标文件的每 Provider × 平台最多一次执行并立即脱敏；
D-01 决定前不发起此类请求。现有 URL、Cookie、token、签名不得进入新文档。

## 旧路径条件分支位置

下面是可复核 grep 候选清单，不是立即删除清单，也不把纯存在性/权限校验一律认定为违规。
第 3 步切换时仍需按职责区分平台分支、资源绑定及安全校验。本步不改这些文件。

检索范围：registry、platforms/、provider_strategy、quality_execution、upstream_routes、
upstream_candidates、upstream_delivery、两类 selection、downloads、download_handoff、work_caption、
common、quality、assets、batch_parse/、views、ytdlp_selection_views，以及 app/batch_parse/
ytdlp_result_picker/ordinary_media_download 前端。表达式：

```text
(if |elif |else if|case |\? ).*(platform|provider|asset_id|assetId|resource_id|resourceId)|((platform|provider|asset_id|assetId|resource_id|resourceId).*(==|!=|===|!==|startsWith|startswith| in | not in ))
```

共 37 个文件、499 个命中行；这是指定范围的机械候选清单，不包含所有间接调用/动态 dispatch，
也不声称已做全仓审查。行号冻结于上面的基线。

| 文件 | 命中行 |
| --- | --- |
| [static/js/batch_parse.js](../../static/js/batch_parse.js#L22) | 22, 513, 514, 515, 538, 560, 562, 1082, 1212, 1276, 1324, 1349, 2021 |
| [static/js/ordinary_media_download.js](../../static/js/ordinary_media_download.js#L397) | 397, 402, 407, 413 |
| [static/js/ytdlp_result_picker.js](../../static/js/ytdlp_result_picker.js#L62) | 62, 68, 74, 80, 83, 84, 85, 86, 87, 92, 97, 99, 100, 103, 104, 113, 363 |
| [xhs_download/services/batch_parse/results.py](../../xhs_download/services/batch_parse/results.py#L99) | 99, 276, 288, 289, 345, 346, 361, 366, 371, 375, 403, 405, 429, 446, 518, 620 |
| [xhs_download/services/download_handoff.py](../../xhs_download/services/download_handoff.py#L317) | 317, 318 |
| [xhs_download/services/downloads.py](../../xhs_download/services/downloads.py#L136) | 136, 138, 258, 488, 490, 496, 499, 500, 508, 523, 526, 529, 533, 545, 547, 549, 551, 553, 559, 577, 579, 583, 591, 593, 597, 640, 656, 659, 661, 663, 666, 668, 670, 674, 688, 690, 702, 704, 757, 761, 774, 794, 904, 905, 961, 962, 1098, 1103, 1109, 1110, 1111, 1124, 1160, 1169, 1397, 1429, 1430 |
| [xhs_download/services/platforms/base.py](../../xhs_download/services/platforms/base.py#L35) | 35, 66 |
| [xhs_download/services/platforms/bilibili_dynamic_normalizer.py](../../xhs_download/services/platforms/bilibili_dynamic_normalizer.py#L390) | 390, 404, 406, 487 |
| [xhs_download/services/platforms/bilibili_fallback.py](../../xhs_download/services/platforms/bilibili_fallback.py#L52) | 52, 55, 61 |
| [xhs_download/services/platforms/bilibili_service_result.py](../../xhs_download/services/platforms/bilibili_service_result.py#L72) | 72, 73, 76, 77 |
| [xhs_download/services/platforms/douyin_kukutool.py](../../xhs_download/services/platforms/douyin_kukutool.py#L516) | 516, 522, 532, 579 |
| [xhs_download/services/platforms/douyin_providers.py](../../xhs_download/services/platforms/douyin_providers.py#L209) | 209, 441, 449, 761, 944, 965, 999, 1019, 1020, 1023, 1179, 1345, 1347, 1412, 1489, 1490, 1506, 1507, 1739, 1747, 1864, 1865, 1867, 1881, 1976, 1979, 2030, 2090, 2464, 2495, 2723, 2831, 2966, 3006, 3008, 3011 |
| [xhs_download/services/platforms/douyin.py](../../xhs_download/services/platforms/douyin.py#L1118) | 1118, 1148, 1152, 1158 |
| [xhs_download/services/platforms/external_tools.py](../../xhs_download/services/platforms/external_tools.py#L419) | 419 |
| [xhs_download/services/platforms/kukutool_generic.py](../../xhs_download/services/platforms/kukutool_generic.py#L573) | 573 |
| [xhs_download/services/platforms/third_party.py](../../xhs_download/services/platforms/third_party.py#L213) | 213, 286, 290, 293, 320, 326, 411, 414, 416, 420, 426, 428, 439, 446, 464, 469, 477, 478, 571, 572, 574, 576, 596, 610, 620, 621, 641, 644, 646, 648, 660, 738, 747, 748, 761, 786, 854, 878, 891, 900, 928, 929, 930, 941, 945, 953 |
| [xhs_download/services/platforms/tiantianjiexi_generic.py](../../xhs_download/services/platforms/tiantianjiexi_generic.py#L865) | 865 |
| [xhs_download/services/platforms/xhs_image_download_authority.py](../../xhs_download/services/platforms/xhs_image_download_authority.py#L125) | 125, 138, 147 |
| [xhs_download/services/platforms/xhs_independent.py](../../xhs_download/services/platforms/xhs_independent.py#L36) | 36, 56, 57, 108, 111, 114, 121, 142, 148, 162 |
| [xhs_download/services/platforms/xhs_livephoto_asset_spec_mapper.py](../../xhs_download/services/platforms/xhs_livephoto_asset_spec_mapper.py#L325) | 325, 378, 380, 415, 416, 419, 441, 442, 454 |
| [xhs_download/services/platforms/xhs_livephoto_builder.py](../../xhs_download/services/platforms/xhs_livephoto_builder.py#L540) | 540, 686, 968, 969, 1061, 1066 |
| [xhs_download/services/platforms/xhs_parsehub_images.py](../../xhs_download/services/platforms/xhs_parsehub_images.py#L309) | 309, 310, 311, 713, 743, 872, 874 |
| [xhs_download/services/platforms/xhs_preview.py](../../xhs_download/services/platforms/xhs_preview.py#L311) | 311, 376, 394 |
| [xhs_download/services/platforms/xhs_rich_media.py](../../xhs_download/services/platforms/xhs_rich_media.py#L222) | 222, 240, 242, 317 |
| [xhs_download/services/platforms/xhs_tiantian_hybrid.py](../../xhs_download/services/platforms/xhs_tiantian_hybrid.py#L111) | 111, 145, 149, 233, 287, 315, 318, 336 |
| [xhs_download/services/platforms/xhs_tiantian_media_policy.py](../../xhs_download/services/platforms/xhs_tiantian_media_policy.py#L71) | 71, 78, 82, 86 |
| [xhs_download/services/platforms/ytdlp_merge.py](../../xhs_download/services/platforms/ytdlp_merge.py#L390) | 390, 392, 394, 417, 451 |
| [xhs_download/services/provider_strategy.py](../../xhs_download/services/provider_strategy.py#L138) | 138, 151, 152, 169 |
| [xhs_download/services/quality_execution.py](../../xhs_download/services/quality_execution.py#L47) | 47, 56, 59, 72, 91, 92, 95, 102, 109, 128, 132, 229, 230, 235, 258, 284, 300, 354, 387, 392, 422, 447, 461, 464, 468, 491, 493, 498 |
| [xhs_download/services/registry.py](../../xhs_download/services/registry.py#L28) | 28, 82, 88, 96 |
| [xhs_download/services/service_format_selection.py](../../xhs_download/services/service_format_selection.py#L63) | 63, 84, 98, 103, 137, 175, 177, 200, 201, 202, 231, 244, 249, 277, 284, 329, 340, 348, 350, 363, 364, 371, 378, 387, 398, 400, 421, 428 |
| [xhs_download/services/upstream_candidates.py](../../xhs_download/services/upstream_candidates.py#L110) | 110, 147, 151, 166, 172, 180, 233, 246 |
| [xhs_download/services/upstream_delivery.py](../../xhs_download/services/upstream_delivery.py#L54) | 54, 57, 59, 200, 202, 251, 284, 301, 311, 412, 435, 441, 452 |
| [xhs_download/services/upstream_routes.py](../../xhs_download/services/upstream_routes.py#L82) | 82, 111, 116, 118, 120, 156, 186, 187, 216, 230, 231, 232, 234, 237, 238, 240, 252, 259, 260, 274, 288, 291, 313, 338, 343, 349, 351, 356, 395, 399, 404, 405 |
| [xhs_download/services/ytdlp_selection.py](../../xhs_download/services/ytdlp_selection.py#L227) | 227, 302, 303, 366, 367 |
| [xhs_download/views.py](../../xhs_download/views.py#L391) | 391, 393, 399, 401, 403, 405, 410, 430, 454, 539, 541, 548, 551, 554, 621, 626, 633, 635, 982, 989, 1107, 1108, 1167, 1766, 1778, 1782, 1786, 1792, 1804, 1808, 2372, 2513, 2517, 2789, 2791, 3097, 3161, 3209, 3299, 3510, 3511, 3577, 3638, 3655, 3712, 3749, 3761, 3764, 3797, 3806, 3891, 3921, 3933, 4112, 4281, 4295, 4297, 4304, 4309, 4324, 4333, 4342, 4359, 4395, 4414, 4435, 4533, 4534, 4711, 4717, 4722, 4726, 4741, 4746, 4757, 4770, 4889, 4912, 5085, 5175, 5309, 5543, 5565, 5821, 6207, 6377, 6383, 6389, 6439, 6466, 7348 |
| [xhs_download/ytdlp_selection_views.py](../../xhs_download/ytdlp_selection_views.py#L52) | 52, 57, 68, 70, 73, 74, 77, 78, 80, 83, 111, 206 |

## 本轮检查与边界

- 目标文件正文与协调人原文件逐字一致；只允许在末尾进度日志追加本轮记录。
- 原始边界使用静态代码、已提交录制和既有验收摘要核对；没有上游请求、媒体取数或生产写入。
- 57 项现有 pipeline 内核契约通过；未修改契约、内核、Worker 或产品路径。
- 日常 8000 保持原进程/原版本 `834d331`，不因文档 PR 切换。
- 待 Human 决定 D-01；其他未核实项保持待核实，不以猜测补齐。生产影子仍未实现/未启用。
