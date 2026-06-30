# Download Routing Engine v2

Download Routing Engine v2 is the first unified decision layer for AnimeImages downloads. Its purpose is to reduce unnecessary server egress, keep existing ZIP downloads as a safe fallback, improve mobile download messaging, and make every download route visible in admin observability.

## Route Types

- `direct`: the browser downloads the upstream resource directly. Server egress is estimated as `0`.
- `proxy`: the server is expected to proxy one file. The first MVP returns a proxy hint and keeps the old ZIP path available.
- `zip`: the server downloads resources and returns a ZIP archive.
- `blocked`: Download Cost Guard or safety validation refused the request.
- `fallback`: reserved for a future direct-to-server fallback path.

## Activation

The existing synchronous endpoint remains:

```text
/media/download-images/
```

Default behavior is unchanged: requests without `download_mode=auto` still use the old ZIP download path, unless the user selected the development-only server-save mode.

Routing Engine v2 is enabled only when the request body contains:

```json
{"download_mode": "auto"}
```

When `auto` returns `direct`, the response is JSON instead of a ZIP file:

```json
{
  "status": "routed",
  "route_type": "direct",
  "route_reason": "图片资源优先浏览器直链下载，降低服务器流量",
  "items": []
}
```

## Decision Rules

The first version does not probe resources with HEAD or Range requests. It only uses known `media_items` metadata such as `type`, `size_bytes`, `provider`, `source_platform`, URL scheme, and provider manifest status.

Resources that can use `direct`:

- Normal public image URLs.
- Small image batches, currently up to 12 images.
- Provider items only when their provider manifest allows public download and the provider does not require server-side recovery or merge.

Resources that stay server-side:

- More than 12 images.
- Mixed image/video batches.
- Multiple resources that need one user-friendly archive.
- YouTube merge resources.
- yt-dlp merge resources.
- KuKuTool ultra-HD resources.
- Provider signed recovery resources that should not expose an unverified URL directly.

Single normal videos currently return `proxy` as a conservative hint. A dedicated proxy single-file download endpoint can be added later without changing the route contract.

## Cost Estimates

`DownloadRequestLog` records:

- `route_type`
- `route_reason`
- `server_egress_estimated_bytes`
- `browser_egress_estimated_bytes`

For `direct`, known `size_bytes` contributes to browser egress. For `zip` and `proxy`, known `size_bytes` contributes to server egress. These numbers are estimates and can be `0` when upstream size is unknown.

## Mobile Notes

Web pages cannot reliably force iOS or Android browsers to save files into the photo album. Mobile result pages therefore show:

```text
手机端建议单张/单个保存。ZIP 文件通常会保存到浏览器下载目录或系统“文件”App 中，不能保证直接进入相册。
```

Do not promise automatic album saving. Prefer direct single-image or single-video actions on mobile, and keep ZIP as the multi-resource fallback.

## Admin Observability

Use:

```text
/admin/console/
/admin/download-logs/
```

The console shows the last 24 hours of route distribution, estimated server egress, estimated browser egress, and estimated server egress saved by direct routes.

The download logs page includes a `route_type` column and can be filtered by route type.

## Rollback

Frontend rollback is simple: stop sending `download_mode=auto`. The old ZIP behavior remains available in the same endpoint.

Backend rollback is also low risk: the new `DownloadRequestLog` fields default to empty strings or zero and do not affect old download records.
