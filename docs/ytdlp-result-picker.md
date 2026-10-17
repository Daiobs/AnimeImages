# yt-dlp 用户结果与格式选择

本增量将普通首页和 Batch 中**共享 yt-dlp 执行路径的单作品候选结果**呈现为作品卡、格式选择和所选资源动作。平台路由、管理员台账、其他 Provider、旧缓存和多作品结果保留原行为；不将新 UI 当作所有 yt-dlp 功能已经开放。

## 用户流程

1. 展示来源 hostname、作品标题、作者、时长和所选文件大小。封面从当前已签发作品记录读取，可选封面失败不影响候选。
2. 视频优先；纯音频作品直接显示音频；视频作品的独立音频默认折叠。
3. 默认展示每个分辨率/码率的一个代表，更多格式可继续展开。所有已保留的格式身份都在，不按分辨率删除编码、帧率或语言变体。大小估计明确标注，未知不补值。
4. 默认选择符合已有格式偏好的可直接处理视频；没有匹配偏好时从可处理视频中选择。用户可以改变选择，主按钮、文件大小及复制动作使用同一候选。
5. 视频预览只在明确点击后准备，播放器 `preload=none`，不自动播放。预览可选择较轻的完整格式，不改变下载选择。
6. 历史技术信息和全部原始候选仍可展开。HLS/DASH、分离轨、未知轨道、HTTP 和必要上下文结果不消失，但本次不会把它们伪装成完整可下载文件。

样式使用 DokiTool 蓝紫色与浅色背景，不替换站点品牌或引入 Vue/Electron。

## 实际接线

- `tools/upstream_match_worker.py` 在既有字节预算中保留可选 thumbnail；不会因装饰字段变化下载媒体。
- `normalize_upstream_result` 给单作品的首个候选绑定作品摘要和私有封面引用；排序后仍按实际持有作品信息的候选取摘要。
- `services/ytdlp_selection.py` 负责纯展示投影和可信缓存记录的所选文件描述，不运行提取器。
- `POST /media/ytdlp-selected-format/` 只接受候选 ID、短期资源 token 和动作。客户端不能发送媒体 URL、format selector 或轨道声明。每次重新检查当前 Provider/来源准入、CSRF、私有记录和既有下载限制。
- 描述符限定浏览器使用，不签发任意服务器媒体资源，不代理媒体。未选格式、其他候选、已失效 token、额外 URL 字段不能替代所选内容。
- 下载调用现有 `AnimeImagesOrdinaryMediaDownload`。仅增加明确标记的 yt-dlp 独立音频 MIME/类型支持；其他音频、图片和其他 Provider 不因此进入这条新路径。
- 保留凭据省略、CORS、重定向拒绝、响应类型、文件大小、停滞和取消处理。失败不自动改用服务器，也不偷偷改成别的格式或纯音频。
- “已交给浏览器保存”不是磁盘保存的遥测证明。真实文件验收须检查浏览器下载和最终文件。

## 刻意没有扩大

本次不启用原生 GenericIE、JS Runtime、Cookie 读取、浏览器自动提取或新来源公开权限；这些需要独立的运行能力验证，不应借界面变化直接放开。既有成熟/旧版 `YtDlpResultNormalizer` 路径没有可用共享候选时继续使用原界面，不伪造格式列表。

本次没有服务器合流、HLS 分片下载、转码或无损升级。只有完整 HTTPS 视频和明确的独立音频适用现有浏览器保存；来源 CORS/HTTP 条件仍可能失败。原封面需要上下文或不能跨域时保留占位信息，不新增图片中转。

## 验证

```sh
python manage.py test xhs_download.tests_ytdlp_selection xhs_download.tests_quality_ui xhs_download.tests_upstream_candidates --noinput
node --test tests_js/ytdlp_result_picker.test.js
node --test tests_js/*.test.js
python manage.py check
python manage.py makemigrations --check --dry-run
git diff --check
```

运行完整套件须与原 CI 一样安装可选 yt-dlp/gallery-dl 及 ParseHub contract Runtime。没有这些运行库的隔离环境不能声称完整套件通过。

新回归包含真实 Normalizer/候选签发/解析接口/模板，外部提取与下载使用明确的合成输入。额外 Chromium 内存页检查使用实际模板和 JS、Django 测试客户端及合成媒体；两份视频和一份 M4A 的浏览器保存与输入字节一致。该检查不是用户本地8000的导航、真实 Provider 调用或真实 CDN/CORS 验收。循环地址导航受浏览器策略限制时不绕过。

## 参考

交互参考：`cshuangyy/videdown`，审查快照 `0c620c1b5b487e2ab735c929dd0127419e5cb963`，主要参考 `src/components/DownloadView.vue` 中“作品卡—格式选择—主操作”的组织方式。

- https://github.com/cshuangyy/videdown/tree/0c620c1b5b487e2ab735c929dd0127419e5cb963
- 该项目为 MIT（Copyright (c) 2024 Videdown）。本增量为独立实现，未复制其 Vue/Electron 实现；不引入它的禁用 TLS 参数、Cookie/Puppeteer 路径、HLS 排除、破坏性格式去重或音频强制转 MP3。

无需修改现有正式路线或生产配置。新旧版本及其他 Provider 的回归由原 CI 继续执行。
