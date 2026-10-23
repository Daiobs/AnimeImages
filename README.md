# DokiTool

DokiTool 是一个基于 Django 的多平台媒体工具：粘贴公开作品链接，查看作品信息与预览，选择上游实际返回的清晰度，再保存所选资源。

仓库仍名为 `Daiobs/AnimeImages`，部分包名、文件路径和 JavaScript 命名空间也继续使用 AnimeImages；产品名称为 DokiTool。

## 当前状态

本说明核对的应用快照是 main `c2b8ac51cfb87d4c030d321bf763080708db9593`，包含已合并的 #96–#100。最后已记录的生产应用仍为 `bf9f1255b006ea93b7a0cf766d28f1db6138113f`；本次只同步文档，没有访问或部署生产。

这两个 SHA 分别表示功能快照和发布记录，不是永久的最新版本指针。证据、验证范围和历史说明见 [当前版本状态](docs/current-version-status.md)。代码已合并、某来源已向访客开放、实际解析成功和文件保存成功是不同事实。

## 主要功能

- **单作品解析**：首页及小红书、抖音、哔哩哔哩入口，保留标题、平台、资源数量和实际取得的媒体信息。
- **逐条 Batch**：展示各作品的完成、失败、超时或未执行状态，可按既有权限重试单条、下载、复制或导出链接。
- **视频格式选择**：共享 yt-dlp、天天解析、Kukutool 结果及抖音专用服务结果可展示真实返回的清晰度；选择与下载资源、文件名和授权对应。
- **图片与音频结果**：保留原始图片、图集和音频候选；视频作品的独立音频位于次级区域，纯音频作品以音频为主。是否可保存取决于具体资源条件。
- **浏览器交付**：已有响应证据支持正常附件和正确媒体扩展名时，优先由浏览器原生直连 CDN 保存；其余适用的完整视频使用共用 Service Worker 流式下载。图片及图片 ZIP 使用已有浏览器路径；特殊合流输出与兼容交付单独处理。
- **管理员评估与选路**：Provider Lab、跨批次平台台账、执行历史、候选比较及精确 hostname 路线草稿，支持有依据地确认首选与备选。

已接入能力来源包括成熟 Native 路线及 yt-dlp、gallery-dl、ParseHub、videodl、天天解析、Kukutool。它们并非每次全部执行：实际使用哪些通道由来源准入、配置、Runtime、匹配和路线策略决定。没有人工路线的适用来源可以使用已获准的默认策略；人工路线是偏好和执行范围的选择，不是所有 Provider 的启用许可证。

## 普通用户如何使用

1. 打开首页，粘贴作品链接或包含链接的分享文本，点击 **获取素材**。
2. 查看作品标题、平台、资源数量与封面。有多档视频时，在视频旁选择清晰度；手机端上下排列。
3. 点击原有 **下载** 按钮，保存当前选中的资源。某些 yt-dlp HTTPS MP4 尚缺轨道事实时，先按提示检查所选格式，确认后再下载；不会自动改成另一档。
4. 查看浏览器下载列表确认实际完成。页面收到全部字节或显示已交付，不单独证明文件已落盘。

视频格式全部展开并按清晰度降序显示；同清晰度的编码、帧率、语言或协议差异仍保留。已确认同资源的 `1080p` / `1080P` 别名可合并为一个选项，原始候选仍可查看。**一个视频的多个格式不是多个独立视频**；上游只有一档时正常显示，不承诺每条作品都有原画或多档。

播放与保存分开：当前选择器提供新标签页播放时，中央三角会核验并打开点击时所选的合法 CDN 视频；保留内嵌预览的路径仍按该路径播放，预览不改变下载选择。源站可能拒绝访问，浏览器也可能拦截弹窗。下载操作不主动打开播放器；浏览器自己的下载后打开偏好另行生效。

格式选择不是管理员专属。匿名用户能否解析某来源、使用某 Provider 或保存视频，仍取决于实际访客开放配置、限额与资源授权。能打开首页不代表任意链接都可解析或下载。

### 常见提示

- **未知大小、编码或音轨**：表示本次未取得可靠信息，不从扩展名或其他档位推断。
- **HLS/DASH、分离轨或需要上下文**：仍保留候选和具体原因，不等于没有解析到媒体，也不保证能够直接保存。
- **链接已过期**：正常重新解析取得新结果，不能继续使用旧媒体授权。
- **浏览器下载兼容**：SW 备用路径按需使用下载专用作用域，不要求替换旧页面 Worker、关闭所有标签或清数据。任务准备有界，正常更新不转移在途任务。只有实际缺少 SW 能力且符合条件时才使用有界浏览器 Blob；媒体仍由浏览器直接获取，不等于服务器中转。已启动、状态未知或已取消的任务不会自动重下。

详细操作和各次真实验收范围见 [格式选择与保存指南](docs/ytdlp-result-picker.md)。

## 主要页面

| 入口 | 用途 |
| --- | --- |
| `/` | 普通单作品解析与下载 |
| `/xhs/`、`/tiktok/`、`/bilibili/` | 现有平台入口 |
| `/batch/` | 逐条批量解析与结果操作，按当前权限开放 |
| `/library/` | 本地图片库 |
| 顶部“平台覆盖”按钮 | 查看覆盖说明；不代表实时成功保证 |
| `/admin/provider-lab/` | Staff 评估、批次与历史、平台总表、路线草稿 |
| `/admin/provider-settings/` | Provider 日常开放模式与相关配置 |
| `/admin/provider-health-tests/` | 已配置的健康检查及验证记录 |

## 本地启动

在全新克隆中，使用现有开发环境约定启动：

```bash
git clone https://github.com/Daiobs/AnimeImages.git
cd AnimeImages
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example.local .env
python manage.py check
python manage.py migrate
python manage.py runserver 127.0.0.1:8000
```

访问 `http://127.0.0.1:8000/`。已有工作目录应继续使用原解释器、`.env` 和数据库，不重复覆盖示例配置或重建数据。开发基线为 Python 3.14.5 / Django 5.2.x；完整环境、检查和隔离 Runtime 操作见 [开发指南](docs/development.md)。

### 可选 yt-dlp Runtime

yt-dlp 不属于应用必装依赖。共享执行路径的兼容版本以 [`VERIFIED_RUNTIME_VERSIONS`](xhs_download/services/quality_execution.py) 为权威来源，目前验证了 **2026.3.17 和 2026.6.9**，不自动认可所有更高版本。PyPI 的 `2026.6.9` 与模块报告的 `2026.06.09` 是同一发行版本的合法表示；同次请求仍核对父进程、匹配与 Worker 的实际版本，缓存也区分 Runtime。

需要安装时，在应用 `.venv` 之外使用单独环境，例如：

```bash
python3.12 -m venv /absolute/local/path/ytdlp-2026.6.9
/absolute/local/path/ytdlp-2026.6.9/bin/python -m pip install --index-url https://pypi.org/simple --no-deps yt-dlp==2026.6.9
```

项目配置使用已有 `YTDLP_BINARY`，例如 `/absolute/local/path/ytdlp-2026.6.9/bin/python -m yt_dlp`，并核对 `YTDLP_PROVIDER_ENABLED` 及当前来源准入。安装包不自动开放公众能力。其他可选 Runtime 的固定版本、隔离安装和参数见 [Provider Lab 指南](docs/provider-lab.md)。

更换 yt-dlp 版本时，先验证真实隔离进程及共享转换，再更新兼容集合与现有 CI；不是只改版本字符串。主要回归入口为 `xhs_download.tests_ytdlp_runtime_compatibility`、`xhs_download.tests_execution_path_parity` 和 `tools.tests_upstream_worker_quality`。现有 CI 含 Python 3.12 的新旧版本隔离验证，不代表生产环境已更新。

## 管理员评估与路线

在已配置开放的 Provider Lab 中，管理员可按平台查看跨批次记录、Scout 与 Lab 来源、作品身份依据、候选和当前路线，再进入草稿。实时资源检查凭据是短期的；执行历史与经过脱敏的导入摘要可以持久保存，历史本身不重新授予下载权限。

路线保存使用正常后台权限、revision 和审计。当前人工路线按**精确 hostname**生效，不是内容类型路由；修改会影响该 host 的适用作品。成熟平台既有路线受到保护。当前接通的两个已审查 videodl Common 可在明确指定 Backend、核验策略/Runtime 并确认该 host 后续共享范围后进入固定路线；其他 Common 不自动获准。单次 Lab 确认或导入结果不等于未来调用授权。

只有一个具备明确目标媒体确认依据的通道时，可称“当前证据中的唯一已验证候选”；成功但身份未确认仍标为未确认媒体。不同作品的成功不能直接排画质。推荐与草稿不会自动保存成正式路线。具体权限、参数与使用限制见 [Provider Lab](docs/provider-lab.md) 和 [Provider Runtime Gates](docs/provider-runtime-gates.md)。

## 已知限制与使用边界

- 平台支持声明、能力匹配、某条样本成功和当前可保存性不能互相替代；登录、DRM、限流、签名过期、TLS/CORS 等实际条件仍会限制结果。
- #99/#100 保留了匿名 PeerTube 两档视频、非 yt-dlp 服务文件，以及正常 Chrome/Safari 流式保存的限定证据；不是所有平台、全部格式或所有浏览器验收。应用内浏览器的首块取消问题仍有环境层证据缺口。
- 独立音频、所有 HLS 合流、全部历史样本的视觉效果未全面验收。既有特定服务端合流能力不等于任意 HLS 都可下载。
- XHS Live Photo 的 Staff 单作品能力与匿名/Batch 的静态展示不同；打包原始 still + motion 不等于自动导入 iOS 相册。
- 仅处理合法获准的内容；本项目不会因接通接口而获得内容使用权或扩大第三方服务/软件许可。videodl 的既有非商业许可及已批准用途仍需遵守，生产或商业用途不能从代码可执行性推断。
- 不在日志、PR 或提交中放入凭据、Cookie、签名媒体链接和私有环境文件。

## 进一步阅读

- [当前版本与生产记录](docs/current-version-status.md)
- [后续开发交接](docs/codex-handoff.md)
- [开发与验证](docs/development.md)
- [应用架构](docs/architecture.md)
- [格式选择与保存](docs/ytdlp-result-picker.md)
- [Provider Lab、平台台账与路线](docs/provider-lab.md)
- [现有固定 SHA 发布通道](ops/managed-release/README.md)
- [变更记录](CHANGELOG.md)

发布沿用现有通道并须另行授权；合并代码或更新本文档都不会自动部署生产。
