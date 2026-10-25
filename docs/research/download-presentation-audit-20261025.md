# 下载审计与结果展示只读审查

日期：2026-10-25。应用基线：`8d91d52f7bc13e0e483912ba762f655bfcb12662`，文件树：`cd8d377a13ff5127b59e55f14c9ce540ca4014fd`。

以下审查是本 PR 最初的只读阶段记录，保留当时基线和证据边界。Human 随后批准开始修复；当前实现范围见末尾“批准后的修复”。历史行号以审查基线为准。只读阶段没有重新调用 Provider、播放或下载媒体，也没有读取生产运行配置或请求日志。

[已记录发布](https://github.com/Daiobs/AnimeImages/pull/131#issuecomment-6394457818)为 `671446300d4e5b8f403cabe21d76a66e6240c5c7` → 本基线，包含这之间的多个已合并 PR，并非只有 #131 的标题修正。本次未重新实时核验生产 SHA。

## 结论

| 项目 | 已确认事实 | 尚不能确认的生产事实 |
| --- | --- | --- |
| 后台下载审计 | 存在阶段混算成功和浏览器交付漏记，不能用现有成功数代表实际保存数 | 生产受影响历史条数、用户各次实际 transport |
| 小红书管理员/匿名展示 | 独立服务候选与 Native+ 是不同结果构造；前者可重复显示同一视频并丢失可复用封面 | 当次准入列表、实际 Provider、候选与普通卡是否确为同一资源 |
| 两个 720P | 只合并特定 `1080p/1080P` 别名；同绑定 `720p/720P` 离线仍显示两个 | 截图两项的实际 URL/资产身份是否相同，不能只凭清晰度合并 |
| 文件名“乱码” | 补充的 32 位后缀与候选 ID 命名分支一致，可离线复现 | 脱敏前基名和当次描述符/transport，不能确认中文编码损坏 |
| 封面延迟 | 动态封面要先核验，再完整 CORS fetch；旧卡走另一条图片加载路径 | 当次究竟是无 cover、过滤、CORS、重定向、超时还是网络慢 |
| 中央播放按钮 | 已接真实动作，冻结当前选择、重新授权后打开对应 CDN；离线接线测试通过 | 这两个生产作品是否实际推进播放；打开新标签不等于播放成功 |

## 确认的问题与最小修复方向

### R1 · P2：后台把准备成功与下载成功混算

- [views.py:5859](../../xhs_download/views.py#L5859) 的 `single_prepare`、[views.py:6588](../../xhs_download/views.py#L6588) 的直连描述符、[views.py:6620](../../xhs_download/views.py#L6620) 的 route-only 都记录 `SUCCESS`，发生在媒体传输之前。
- [admin_console.py:308](../../xhs_download/services/admin_console.py#L308) 按全部 `DownloadRequestLog` 行统计次数和成功，未区分阶段。一次 route + prepare + stream 失败，在这些分支均写入时会形成两条成功、一条失败；这是代码推导示例，不是生产统计。
- [ytdlp_result_picker.js:479](../../static/js/ytdlp_result_picker.js#L479) 的所选格式直接请求动作接口，再调用共用下载器；[ytdlp_selection_views.py:185](../../xhs_download/ytdlp_selection_views.py#L185) 仅输出描述符日志，没有相应下载审计表记录。
- [ordinary_media_streaming.js:177](../../static/js/ordinary_media_streaming.js#L177) 现有遥测只报失败/取消；[browser_streaming_telemetry.py:515](../../xhs_download/services/browser_streaming_telemetry.py#L515) 只输出日志和协议计数。原生移交、SW 成功、Blob 完整接收没有统一 DB 终态。

影响：后台成功率、下载次数和传输方式不能回答用户实际完成了哪一步。最小方向是扩展现有审计记录及控制器事件关联，不另建下载器，不改变选路；准备和移交不计入传输成功率，未观测到终态保持未知。

同一审计链的附属缺口：

- 通用 Worker 请求不带 Provider 是既定设计，但遥测没有在页面侧补回关联，容易记成 `unknown`。后端 [browser_streaming_telemetry.py:230](../../xhs_download/services/browser_streaming_telemetry.py#L230) 仍按旧媒体域范围拒绝 `bilivideo.com/cn`，与当前前端范围不一致。修复应在页面/服务端关联已授权尝试，不能把业务字段塞回冻结的 Worker 协议，也不能增加下载域限制。
- 保留的 POST 附件分支 [views.py:6735](../../xhs_download/views.py#L6735) 未挂终态回调，返回响应前即在 [views.py:6834](../../xhs_download/views.py#L6834) 写成功，并把返回的预计字节作为实际字节。复用现有流终态回调即可；不要误称 prepare→stream 主路径也全部如此。
- 部分下载前过期/限流/拒绝直接返回，未写失败阶段；应标明“未开始传输”，不能伪装成源站传输失败。
- `download_stream_*` 的实际字节/关闭原因存在 LogRecord `extra` 中，但默认 [logging.py:65](../../djangoProject/logging.py#L65) 与 [settings.py:672](../../djangoProject/settings.py#L672) 不完整输出这些字段。应测试最终格式化文本，而不只断言内存中的 LogRecord。

### R2 · P2：同一小红书视频被分成选择器和普通卡

[xhs_independent.py:119](../../xhs_download/services/platforms/xhs_independent.py#L119) 合并独立服务结果时保留 Native assets，并复制服务 candidates；candidate-only 服务没有 Native 下载资产绑定。[ytdlp_picker.py:20](../../xhs_download/templatetags/ytdlp_picker.py#L20) 只凭下载绑定寻找对应旧卡，[ytdlp_picker.py:28](../../xhs_download/templatetags/ytdlp_picker.py#L28) 再将候选视频组与剩余旧卡相加。

离线反例：分别使用仓库现有天天、KuKu 脱敏响应，经过真实候选创建、独立结果合并、展示函数和模板；即使 Native URL 与其中一个候选相同，仍为一个选择器组 + 一个普通卡，计数 2，`legacy_video=None`。这与图 1 的形状一致，但不能仅凭两个 9.1 MB 就断言生产 URL 相同。

最小方向：在现有结果合并/展示层明确同作品、同资源的展示归属，统一卡片排除和数量计算；没有可靠归属则保留分立。展示归属不等于下载授权，不伪造 `legacy_asset_id`，不把第一档 manifest 授给第二档，不删除 Native 原有合法操作。

### R3 · P2：已有封面未被选择器复用，动态封面额外依赖 CORS

[ytdlp_result_picker.html:16](../../templates/partials/ytdlp_result_picker.html#L16) 仅在 `legacy_video` 成立时使用旧卡封面；R2 的路径即使有 Native 封面也不会使用。否则只读 [service_format_selection.py:293](../../xhs_download/services/service_format_selection.py#L293) 的 payload `cover`。

[ytdlp_result_picker.js:578](../../static/js/ytdlp_result_picker.js#L578) 先请求 cover 描述符，再以 `mode:cors`、`cache:no-store`、`redirect:error` 完整读取图片 Blob 后赋值，整体上限 8 秒，失败静默留占位。这是封面图片，不是视频下载 Blob；不能据此认定视频也走 Blob。旧卡的图片路径不具有完全相同的前置条件。

最小方向：首先复用当前结果中已确认同作品的封面，不读取历史过期信息；仍需动态授权时，复用既有图片显示路径而非视频下载策略。保留 URL 安全、图片类型/大小和原准入约束，不新增图片/视频中转，不因图片失败影响视频选择。生产秒级延迟需已有请求 timing 才能归因，不能声称 8 秒计时器本身造成固定等待。

### R4 · P2：大小写别名只对 1080P 特判

[service_format_selection.py:112](../../xhs_download/services/service_format_selection.py#L112) 仅处理 `1080p/1080P`；[ytdlp_selection.py:171](../../xhs_download/services/ytdlp_selection.py#L171) 把无法区别的同标签项显示为“版本 1/2”。同一绑定、相同事实、一项大小已知的离线输入：`720p/720P` 输入 2、输出 2；改成 `1080p/1080P` 则输出 1。

最小方向：对有相同资源/绑定证据的纯拼写别名使用统一规范化，不按分辨率写死。保留原始候选记录及已知大小；真实不同 URL、编码、帧率、语言或冲突事实不能仅按高度删除。截图两项身份尚未取得，故本报告不主张直接删掉第二项。

### R5 · P2：内部候选身份进入可控文件名

[ytdlp_selection.py:322](../../xhs_download/services/ytdlp_selection.py#L322) 在没有原生 format ID 时用 candidate ID，再生成 `短标题_ID.扩展名`；服务选择器在没有可复用有效 legacy filename 时沿用该结果，[service_format_selection.py:345](../../xhs_download/services/service_format_selection.py#L345)。

用户补充的脱敏文件名尾部是 `_` 加 32 位十六进制 ID 再加 `.mp4`；离线输入该 ID 得到 `Fixture title_<candidate_id>.mp4` 的相同形式。本公开记录省略实际 ID。这是可识别的应用 ID 后缀，不是已确认的中文乱码。命名函数并无 Staff 特判；管理员和匿名可能进入不同的候选/旧卡路径，实际对应性仍需当次动作记录。

最小方向：页面可控命名统一消费既有短标题，必要的多资源消歧用可读序号/格式差异，不暴露随机候选 ID；candidate ID 留在内部授权。CDN 原生交付最终名称仍由源站响应决定，按 INV-06 保留合理源站文件名，不能为强制标题改名而强改下载方式。

导致 720 别名未合并的 `1080p/1080P` 特判来自 #100；ID 后缀和动态封面机制在 #99/#100 已存在。`git blame` 不支持“全部由 #131 新增”的结论。

## Staff 与匿名为何不一样

[xhs_independent.py:31](../../xhs_download/services/platforms/xhs_independent.py#L31) 在现有第三方门禁下为 Staff/匿名计算不同的合法独立服务集合。授权集合包含天天时，[registry.py:82](../../xhs_download/services/registry.py#L82) 关闭该次 Native+ 的重复天天增强，由独立服务提供候选。

未获独立服务准入、但现有超清门禁允许的请求仍可能走 [xhs_tiantian_hybrid.py:375](../../xhs_download/services/platforms/xhs_tiantian_hybrid.py#L375) 的 Native+，保留 Native 封面并返回单个最高候选 asset，没有同样的 picker 桥接。这能解释图 1/2 的结构差异，不证明生产当时恰好采用了哪项配置。

不应通过放开匿名权限来“修复显示”。两条已获准路径应消费一致的展示语义；任何缺失格式只能在原准入下接线。Staff 技术区允许存在，普通用户继续隐藏，权限与显示分开处理。图 2 的页面 100% 不能单独证明最终文件完整落盘。

## 审计建议口径（待批准实施）

复用现有 `DownloadRequestLog`，用一个不含秘密的尝试 ID 关联准备与传输事件，区分计划策略和实际传输。建议字段为阶段、实际 transport、证据来源、观测字节、错误码及合法资源关联；不记录签名 URL、Cookie、token，不信任客户端任意提交的资源或成功结论。后台仅管理员可见，客户端事件保留 `client_observed` 属性和现有 CSRF/限流要求。

| 实际方式 | 移交 | 开始 | 完成能说明什么 | 失败/未知 |
| --- | --- | --- | --- | --- |
| 原生直链 `browser_native` | 已发起导航/附件请求 | 网页不能确认浏览器接单 | 网页不能确认完成，保持未知 | 可记移交前本地失败；不得超时猜测下载失败 |
| SW 流式 `browser_streaming` | 已注册/触发下载响应 | 区分 fetch 开始与首字节 | Worker EOF 与字节校验通过，不等于磁盘落盘 | 记录真实失败、取消和无法确认的状态 |
| 浏览器 Blob `browser_blob` | 完整接收后调用保存移交 | fetch/首字节 | 完整正文接收、校验与保存移交分别记录，不等于保存确认 | 记录实际异常；不因此新增自动重试 |
| 服务端交付 `server_attachment` 等 | 附件响应已返回（服务端视角），客户端接收未确认 | 上游读取/下游 yield | 服务端流 EOF 与实际 yield 字节，不保证客户端文件保存 | 下游关闭与上游失败分开；预计字节不当实际字节 |

后台保留用户要求的移交、开始、完成、失败，同时允许取消/未知；“完成”必须显示完成的是哪个阶段。原生完成缺失不能为补审计而加代理、Blob、媒体重复请求或修改 Worker。服务器生成 ZIP/音视频合并应标明输出类型，不与普通服务端转交流量混为一谈。涉及模型/视图等的实际修复按既有风险清单评定，不能借本 L0 文档 PR 先行实施。

## 播放按钮

选择器中央按钮由 [ytdlp_result_picker.js:376](../../static/js/ytdlp_result_picker.js#L376) 同步冻结当前选择，先开可清理的空标签，再发 `action=open`。服务端核验当前权限、候选、URL 和有效期后返回，只有仍是同一选择才导航；不会切换复选框或发起下载流程。弹窗被拦、关闭、换档、拒绝和晚到响应已有回归。

因此“按钮没有接线”不成立；但 CDN 的 MIME、附件响应、访问条件和浏览器解码决定最终是播放、下载还是错误。当前页面只能确认链接已打开，不能跨域读取真实播放进度。本轮未新增播放，不把离线弹窗测试或旧样本成功扩写为这两条生产 CDN 已播放。旧卡与选择器的播放路径也不应混称为同一次测试。

## 验证与后续最小范围

- 55 项 Django 测试通过：`xhs_download.tests_douyin_format_selection`、`xhs_download.tests_service_format_selection`。使用无 `.env` 的独立工作树、内存数据库/缓存及既有网络替身；其中日志 `external_call=True` 是受控测试事件，不是新增真实调用。
- 209 项 Node 测试通过：`tests_js/ytdlp_result_picker.test.js`、`tests_js/ytdlp_picker_batch_dom.test.js`，包括真实前端控制器的选择、播放、弹窗失败及晚到响应。
- 另四个既有 Node/契约文件 exit 0：`ordinary_media_download.test.js`、`ordinary_media_streaming.test.js`、`download_reliability.test.js`、`contracts/frontend_contracts.test.js`；dot reporter 记录 204 个通过标记，未重复执行。
- 两份现有服务响应夹具完成小红书函数/模板级反例；另完成 720/1080 别名和候选 ID 文件名函数级复现。不是生产请求，也不是新浏览器文件保存验收。
- 文档链接/行号、敏感内容和 `git diff --check` 在提交前检查；该文档 Head 的实际 CI 见 PR，不借用应用基线绿灯。不重跑完整本地应用测试。

建议后续修复只覆盖 R1 审计口径和事件关联、R2/R3 展示归属与封面、R4 有证据的别名、R5 可控命名。使用同输入分别验证 Staff/匿名、首页/Batch；先离线反例再最小真实验收，不改准入、不删除原候选或改 manifest，也不增加 Worker 协议门槛。

本次未解决项：生产两项 720 的资源同一性、管理员当次实际 transport、封面请求具体失败/耗时、两条 CDN 实际播放均待定向证据；不是已经确认的源站或权限故障。真实解析、媒体请求、播放、完整保存、生产登录和生产配置读取均新增 0 次。本地 8000 继续使用原 `8d91d52` 进程。

## 批准后的修复

- R1：扩展现有 `DownloadRequestLog`，以一次尝试关联准备、移交、传输开始、传输完成、失败、取消及未知。原生、SW、浏览器 Blob、服务端附件分开记录；新客户端事件须持服务端在合法动作后签发的身份绑定审计凭据，沿用 CSRF、限流，凭据不授予媒体访问权。后台区分客户端观测和服务端观测，原生移交不称完成，历史记录不追认为文件落盘。新增迁移 0030 只扩展审计字段。
- R1 覆盖边界：新浏览器审计凭据仅签发给普通视频/音频；图片批量派发仅保留一条聚合准备记录与原有派发反馈，不按图片逐项写 attempt/token。`browser_photo_archive`、图集 ZIP 和音视频合流保留现有交付与 legacy 观测口径，本轮未接入其完整的新阶段审计，不能据此声称已确认整组图片、ZIP 或合流文件保存完成。
- R1 取消口径：新审计控制台的按尝试汇总保留服务端取消前已 yield 的字节，状态仍为 cancelled，不算上游失败。旧策略/风险读取默认排除取消事件，避免既有“非 success 即失败”逻辑误判；这些旧策略派生面板的流量数字不含取消流量，不能替代新审计控制台的服务端已发送统计。本轮不扩展旧策略循环。
- R2：当前可信作品的 Native 封面可供同作品服务选择器展示；只有明确同资源且选择器仍有合法操作时才排除重复旧卡。不同文件继续保留其原有操作，不借用 Native manifest，也不修改匿名准入。
- R3：动态封面保留服务端动作授权与 HTTPS URL 校验，交给普通 `img` 显示，不再先以 CORS/no-store 把整个图片读成 Blob。图片解码由浏览器处理；此路径不再声称执行原 JS 的 2 MiB 正文检查。这不改变视频的 MIME、大小、下载路径及 URL 授权约束。
- R4：有精确资源或既有绑定证据时合并纯大小写质量别名；不同 URL、编码、帧率、语言或冲突大小不合并。原始候选仍全部保留；图片不增加新字段，维持原候选数量/字节预算。
- R5：应用可控名称使用既有短标题、真实扩展名和必要的稳定序号，不拼候选 UUID。原生 CDN 名称继续按 INV-06 使用合格源站文件名，不为改名改变交付方式。
- 播放按钮保持原接线。上述修复不证明截图两条生产 CDN 的实际播放，也不把 Staff 样本算作匿名通过。

两个 Worker 协议文件、产品不变量文字、正式路线和生产均不改动。本 PR 保持 Draft，当前 Head 的技术检查与实机验收结果在 PR 描述中报告；L2 仍需 Human 复审批准。
