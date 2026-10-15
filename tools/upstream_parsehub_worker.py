"""Pinned registry execution in a disposable child, never a media downloader.

Registry and result types are imported only by the child. Gallery messages use
URL (3) and metadata (2) shapes. Candidate declarations are not fetch authority.
"""
import asyncio
import contextlib
import contextvars
from importlib import metadata
import json
import math
import os
from pathlib import Path
import re
import sys
import time
from urllib.parse import urljoin, urlsplit
from unittest.mock import patch

try:
    from .upstream_match_worker import install_network_guard, safe_error, validate_url, validate_media_url
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from upstream_match_worker import install_network_guard, safe_error, validate_url, validate_media_url

PIN = "144260f4349c5bb57415f7555a572fcd48c3b8ba"
VERSION = "2.2.0"
PROVIDER_ID = "parsehub_generic"
PROVIDER_VERSION = f"parsehub@{VERSION}+{PIN}"
# Candidate descriptors are independent of downstream downloadable-asset limits.
MAX_ASSETS = 1000
MAX_BYTES = 2 * 1024 * 1024
MAX_NETWORK_BYTES = 12 * 1024 * 1024
MAX_WIRE_BYTES = 32 * 1024 * 1024
MAX_REQUESTS = 64
MAX_REDIRECTS = 8
MAX_DELEGATIONS = 16
MAX_DEPTH = 8
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "avif"}
VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "webm"}
COMPLETENESS = {"unknown", "image_candidate", "complete", "video_only", "audio_only", "hls", "dash", "richtext"}
QUALITY_FIELDS = ("width", "height", "duration", "fps", "tbr", "abr", "vbr", "bitrate",
                  "filesize", "filesize_approx", "format_id", "format_note", "resolution",
                  "vcodec", "acodec", "protocol", "audio_channels", "language",
                  "role", "work_id", "upstream_asset_id", "asset_id",
                  "audio_tracks", "video_tracks", "upstream_role")


def registry():
    from parsehub.parsers.base import BaseParser
    return tuple(BaseParser.get_registry())


def entry(cls):
    return {"provider_id": PROVIDER_ID, "extractor_id": cls.__name__,
            "label": cls.__platform__.display_name, "version": PROVIDER_VERSION}


def source_identity(url):
    """Only retained exact-content exceptions interpret site identity."""
    validate_url(url)
    p = urlsplit(url)
    if p.hostname in {"www.coolapk.com", "coolapk.com"}:
        match = re.fullmatch(r"/(?:feed|picture)/([1-9][0-9]{0,19})/?", p.path)
        if not match:
            raise ValueError("extractor_mismatch")
        return "CoolapkParser", match[1]
    if p.hostname in {"www.douban.com", "m.douban.com", "douban.com"}:
        match = re.fullmatch(r"/(?:group/)?topic/([1-9][0-9]{0,19})/?", p.path)
        if match:
            return "DoubanParser", match[1]
    return "", ""


def upstream_source(url):
    validate_url(url)
    p = urlsplit(url)
    return p._replace(netloc="www.coolapk.com").geturl() if p.hostname == "coolapk.com" else url


def match_url(url):
    try:
        url = upstream_source(url)
    except (ValueError, TypeError):
        return ()
    return tuple(entry(cls) for cls in registry() if cls.match(url))


def import_parser(extractor):
    for cls in registry():
        if cls.__name__ == extractor:
            return cls
    raise ValueError("extractor_mismatch")


def verify_install():
    dist = metadata.distribution("parsehub")
    direct = json.loads(dist.read_text("direct_url.json") or "{}")
    if dist.version != VERSION or direct.get("vcs_info", {}).get("commit_id") != PIN:
        raise ValueError("version_changed")


def timeout_value(value):
    if isinstance(value, bool):
        raise ValueError("invalid_request")
    value = float(value)
    if not math.isfinite(value) or not 0.1 <= value <= 30:
        raise ValueError("invalid_request")
    return value


def failure_summary(error, stage):
    current = error
    for _ in range(5):
        code = str(current)
        if code in {"identity_mismatch", "unsupported_result", "request_limit", "invalid_request",
                    "output_limit", "network_denied", "delegation_limit", "scope_limited",
                    "redirect_limit", "network_timeout", "no_formats", "invalid_url"}:
            return {"ok": False, "error": code, "stage": stage, "error_class": type(current).__name__}
        response = getattr(current, "response", None)
        if response is not None and getattr(response, "status_code", 0) >= 400:
            return {"ok": False, "error": "access_denied" if response.status_code in {401, 403}
                    else "upstream_unavailable", "stage": stage, "error_class": type(current).__name__}
        current = getattr(current, "cause", None) or current.__cause__
        if current is None:
            break
    return safe_error(error, stage)


def plain_text(value, limit=2000):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(str(value or "")[:16000], "html.parser")
    for node in soup(["script", "style"]):
        node.decompose()
    text = re.sub(r"https?://[^\s<>]+", "[link]", soup.get_text(" ", strip=True))
    return re.sub(r"(?i)\b(cookie|authorization|access_token|password)\s*[:=]\s*\S+", r"\1=[redacted]", text)[:limit]


def scalar_fields(obj):
    get = obj.get if isinstance(obj, dict) else lambda key: getattr(obj, key, None)
    fields = {}
    for key in QUALITY_FIELDS:
        value = get(key)
        if isinstance(value, bool) or value is None:
            continue
        if isinstance(value, (int, float)) and math.isfinite(value) and value >= 0:
            if value or key not in {"width", "height", "duration", "fps"}:
                fields[key] = value
        elif isinstance(value, str) and value:
            fields[key] = value if key in {'format_id', 'asset_id', 'upstream_asset_id', 'work_id'} else plain_text(value, 200)
    for key in ('requires_merge', 'audio_asset_present'):
        if type(get(key)) is bool:
            fields[key] = get(key)
    relationships = get('relationships')
    if isinstance(relationships, (list, tuple)):
        fields['relationships'] = [
            {key: relation[key] for key in
             ('relationship_type', 'source_asset_id', 'target_asset_id') if isinstance(relation.get(key), str)}
            for relation in relationships[:16] if isinstance(relation, dict)
            and all(isinstance(relation.get(key), str) and 0 < len(relation[key]) <= 120
                    for key in ('relationship_type', 'source_asset_id', 'target_asset_id'))]
    return fields


def normalize_result(result, source, *, extractor=None, identity_evidence="upstream_parser_result"):
    from parsehub.types.media_ref import AniRef, ImageRef, LivePhotoRef, MediaRef, VideoRef
    from parsehub.types.result import ParseResult
    if not isinstance(result, ParseResult):
        raise ValueError("unsupported_result")
    validate_url(source)
    canonical = validate_url(getattr(result, "raw_url", "") or source)
    exception, content_id = source_identity(source)
    if exception and source_identity(canonical) != (exception, content_id):
        raise ValueError("identity_mismatch")
    if not content_id and identity_evidence == "upstream_response_id":
        _, content_id = source_identity(canonical)
    common = {"title": plain_text(getattr(result, "title", ""), 200),
              "content_summary": plain_text(getattr(result, "markdown_content", "") or getattr(result, "content", "")),
              "source_url": source, "canonical_source": canonical, "source_id": content_id,
              "extractor_id": extractor or exception or type(result).__name__,
              "identity_evidence": identity_evidence, "quality_evidence": "upstream_declared",
              "result_type": type(result).__name__, "candidate_schema": "upstream_candidate_v1"}
    media = getattr(result, "media", None)
    media = list(media) if isinstance(media, (list, tuple)) else [media] if media else []
    info = getattr(getattr(result, "dl", None), "info_json", None)
    formats = None
    if isinstance(info, dict):
        common.update(source_id=str(info.get("id") or "")[:200], selected_backend="yt-dlp",
                      author=plain_text(info.get("uploader") or info.get("creator"), 200))
        formats = info.get("formats") or info.get("requested_formats") or ([info] if info.get("url") else [])
        if not isinstance(formats, list):
            raise ValueError("unsupported_result")
        # Upstream's YtVideoParseResult.video is a webpage, not a media file.
        media = []
    candidates = []
    for item in media:
        if not isinstance(item, MediaRef):
            raise ValueError("unsupported_result")
        kind = "video" if isinstance(item, VideoRef) else "image" if isinstance(item, (ImageRef, AniRef, LivePhotoRef)) else "unknown"
        ext = str(getattr(item, "ext", "") or "").lower().lstrip(".")
        path_ext = Path(urlsplit(item.url).path).suffix.lower().lstrip(".")
        if isinstance(item, AniRef) and (ext in VIDEO_EXTENSIONS or path_ext in VIDEO_EXTENSIONS):
            kind, ext = "video", ext if ext in VIDEO_EXTENSIONS else path_ext
        details = scalar_fields(item)
        if isinstance(item, LivePhotoRef):
            details.update(relationship_id=f"livephoto-{len(candidates)}", relationship_role="still")
        candidates.append((item.url, kind, ext, details))
        if isinstance(item, LivePhotoRef) and item.video_url:
            candidates.append((item.video_url, "video", item.video_ext,
                               {"relationship_id": details["relationship_id"], "relationship_role": "motion"}))
    for fmt in formats or []:
        if not isinstance(fmt, dict) or not isinstance(fmt.get("url"), str):
            raise ValueError("unsupported_result")
        kind = "audio" if fmt.get("vcodec") == "none" and fmt.get("acodec") not in {None, "none"} else "video" if fmt.get("vcodec") not in {None, "none"} else "unknown"
        details = scalar_fields(fmt)
        headers = fmt.get("http_headers") or {}
        if isinstance(headers, dict):
            details["request_context"] = {k: v for k, v in headers.items()
                                           if isinstance(k, str) and isinstance(v, str)
                                           and k.lower() in {"user-agent", "referer", "accept", "accept-language"}}
        candidates.append((fmt["url"], kind, str(fmt.get("ext") or "").lower(), details))
    messages, used = [], 0
    for index, (url, kind, ext, details) in enumerate(candidates):
        validate_media_url(url)
        if len(messages) >= MAX_ASSETS:
            break
        path_ext = Path(urlsplit(url).path).suffix.lower().lstrip(".")
        protocol = details.get("protocol", "")
        transport = "hls" if path_ext == "m3u8" or ext == "m3u8" or "m3u8" in protocol else "dash" if path_ext == "mpd" or ext == "mpd" or "dash" in protocol else "file"
        completeness = "image_candidate" if kind == "image" else "unknown"
        if kind == "audio":
            completeness = "audio_only"
        elif details.get("acodec") == "none" and kind == "video":
            completeness = "video_only"
        elif kind == "video" and details.get("acodec") and details.get("vcodec"):
            completeness = "complete"
        restrictions = []
        if urlsplit(url).scheme == "http":
            restrictions.append("insecure_transport")
        if transport != "file":
            restrictions.append("stream_download_unavailable")
        if completeness in {"video_only", "audio_only"}:
            restrictions.append("separate_track")
        if completeness == "unknown":
            restrictions.append("unknown_media_properties")
        if ((kind == "video" and (ext in IMAGE_EXTENSIONS or path_ext in IMAGE_EXTENSIONS)) or
                (kind == "image" and (ext in VIDEO_EXTENSIONS or path_ext in VIDEO_EXTENSIONS))):
            restrictions.append("media_type_conflict")
        # Work-level prose and canonical URLs need not be repeated per candidate.
        context = common if not messages else {
            key: common[key] for key in ('extractor_id', 'identity_evidence', 'quality_evidence',
                                        'result_type', 'candidate_schema')}
        meta = {**context, **details, "extension": ext, "media_type": kind,
                "mime_type": f"{kind}/unknown" if kind != "unknown" else "application/octet-stream",
                "mime_evidence": "upstream_reference_type", "candidate_kind": transport if transport != "file" else completeness,
                "media_completeness": completeness, "requires_completeness_check": kind == "video" and completeness == "unknown",
                "operation_restrictions": restrictions, "candidate_index": index}
        row = [3, url, meta]
        size = len(json.dumps(row).encode())
        if used + size > MAX_BYTES - 16384:
            break
        messages.append(row)
        used += size
    if messages:
        if len(messages) < len(candidates):
            messages[0][2].update(candidates_truncated=True, candidate_total=len(candidates), candidate_retained=len(messages),
                              upstream_scope={'bounded': True, 'completion': 'unknown',
                                  'returned_count': len(candidates), 'retained_count': len(messages),
                                  'declared_count': len(candidates), 'examined_count': index + 1,
                                  'rejected_count': 0,
                                  'unexpanded_count': len(candidates) - len(messages),
                                  'reason': 'candidate_limit' if len(candidates) > MAX_ASSETS else 'output_limit'})
        return messages
    if common["content_summary"]:
        return [[2, {**common, "candidate_kind": "richtext", "media_completeness": "richtext",
                     "operation_restrictions": ["no_explicit_media"]}]]
    raise ValueError("no_formats")


def verify_response_identity(extractor, content_id, response):
    if extractor == "CoolapkParser":
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(response.text, "html.parser")
        scripts = "\n".join(node.text for node in soup.find_all("script"))
        ids = set(re.findall(r"intent://www\.coolapk\.com/(?:feed|picture)/([0-9]+)#Intent;", scripts))
        if len(soup.find_all(id="feed-detail")) != 1 or ids != {content_id}:
            raise ValueError("identity_mismatch")
        return
    try:
        payload = response.json()
    except (ValueError, TypeError):
        raise ValueError("identity_mismatch") from None
    if not isinstance(payload, dict) or str(payload.get("id")) != content_id:
        raise ValueError("identity_mismatch")


class Budget:
    def __init__(self, timeout):
        self.deadline = time.monotonic() + timeout
        self.requests = self.bytes = self.redirects = self.delegations = self.connections = self.wire_bytes = 0
        self.decoded_bytes = 0
        self.identity = False
        self.response_ids = set()
        self.body_media = None

    def check(self):
        if time.monotonic() >= self.deadline:
            raise ValueError("network_timeout")

    def request(self, url):
        self.check()
        validate_url(url)
        self.requests += 1
        if self.requests > MAX_REQUESTS:
            raise ValueError("request_limit")

    def received(self, size):
        self.check()
        self.bytes += size
        if self.bytes > MAX_NETWORK_BYTES:
            raise ValueError("output_limit")

    def redirect(self):
        self.redirects += 1
        if self.redirects > MAX_REDIRECTS:
            raise ValueError("redirect_limit")

    def decoded(self, size):
        self.check()
        self.decoded_bytes += size
        if self.decoded_bytes > MAX_NETWORK_BYTES:
            raise ValueError("output_limit")


def restrict_ytdlp_transport(ydl):
    """Native handlers do not pass through Python socket/HTTP guards."""
    handlers = ydl._request_director.handlers
    for key in list(handlers):
        if key != "Urllib":
            handlers.pop(key).close()
    if "Urllib" not in handlers:
        raise ValueError("network_denied")


@contextlib.contextmanager
def execution_boundary(budget, source, extractor):
    """Bound actual HTTP hops/reads; socket guard pins every public DNS answer."""
    import http.client
    import httpx
    from parsehub.parsers.base import BaseParser
    from parsehub.parsers.base.ytdlp import YtParser
    import yt_dlp
    from yt_dlp.networking._urllib import HTTPHandler
    import urllib3.response
    import httpx._decoders
    original_async, original_sync = httpx.AsyncClient, httpx.Client
    parse_original = BaseParser.parse
    stack = contextvars.ContextVar("parsehub_stack", default=())
    exception, content_id = source_identity(source)

    def inspect_response(response):
        if response.status_code in {301, 302, 303, 307, 308}:
            budget.redirect()
            validate_url(urljoin(str(response.url), response.headers.get("location", "")))
        target = urlsplit(str(response.url))
        response_id = content_id
        identity_target = (extractor == "CoolapkParser" and target.hostname == "www.coolapk.com" and
                           target.path.rstrip("/") == urlsplit(source).path.rstrip("/"))
        if extractor == "DoubanParser" and target.hostname == "m.douban.com":
            topic = re.fullmatch(r"/rexxar/api/v2/group/topic/([1-9][0-9]{0,19})", target.path)
            if topic:
                response_id = content_id or topic[1]
                identity_target = topic[1] == response_id
        if identity_target and 200 <= response.status_code < 300:
            verify_response_identity(extractor, response_id, response)
            budget.identity = True
            budget.response_ids.add(response_id)
            if extractor == "CoolapkParser":
                from bs4 import BeautifulSoup
                detail = BeautifulSoup(response.text, "html.parser").find(id="feed-detail")
                article = detail.find(class_="feed-article-message")
                images = article.select("img.message-image") if article else detail.select(".message-image-group img")
                budget.body_media = [urljoin(str(response.url), str(img.get("src", ""))) for img in images]

    def options(kwargs):
        kwargs.update(trust_env=False, proxy=None, cookies=None, auth=None, verify=True,
                      timeout=max(.1, budget.deadline - time.monotonic()), max_redirects=MAX_REDIRECTS)
        return kwargs

    def prepare(request):
        budget.request(str(request.url))
        for key in ("cookie", "authorization", "proxy-authorization"):
            request.headers.pop(key, None)
        request.extensions["timeout"] = dict.fromkeys(("connect", "read", "write", "pool"),
                                                       max(.1, budget.deadline - time.monotonic()))

    class AsyncClient(original_async):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **options(kwargs))

        async def _send_single_request(self, request):
            prepare(request)
            response = await super()._send_single_request(request)
            chunks = []
            try:
                async for chunk in response.aiter_bytes():
                    budget.received(len(chunk))
                    chunks.append(chunk)
                response._content = b"".join(chunks)
                inspect_response(response)
                return response
            finally:
                await response.aclose()

    class Client(original_sync):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **options(kwargs))

        def _send_single_request(self, request):
            prepare(request)
            response = super()._send_single_request(request)
            chunks = []
            try:
                for chunk in response.iter_bytes():
                    budget.received(len(chunk))
                    chunks.append(chunk)
                response._content = b"".join(chunks)
                inspect_response(response)
                return response
            finally:
                response.close()

    putrequest, putheader = http.client.HTTPConnection.putrequest, http.client.HTTPConnection.putheader
    read, read1 = http.client.HTTPResponse.read, http.client.HTTPResponse.read1
    begin = http.client.HTTPResponse.begin

    def guarded_request(conn, method, url, *args, **kwargs):
        scheme = "https" if isinstance(conn, http.client.HTTPSConnection) else "http"
        host = f"[{conn.host}]" if ":" in conn.host else conn.host
        budget.request(urljoin(f"{scheme}://{host}:{conn.port}", url))
        conn.timeout = max(.1, budget.deadline - time.monotonic())
        return putrequest(conn, method, url, *args, **kwargs)

    def guarded_header(conn, header, *values):
        name = header.decode("ascii") if isinstance(header, bytes) else str(header)
        if name.lower() not in {"cookie", "authorization", "proxy-authorization"}:
            return putheader(conn, header, *values)

    decoder_factory = urllib3.response._get_decoder

    def bounded_decoder(encoding):
        decoder = decoder_factory(encoding)

        class Decoder:
            def decompress(self, data, max_length=-1):
                budget.check()
                limit = max(1, MAX_NETWORK_BYTES - budget.decoded_bytes + 1)
                if max_length is not None and max_length >= 0:
                    limit = min(limit, max_length)
                output = decoder.decompress(data, max_length=limit)
                budget.decoded(len(output))
                return output

            @property
            def has_unconsumed_tail(self):
                return decoder.has_unconsumed_tail

            def flush(self):
                # Drain pending output through the capped decoder before flush.
                chunks = []
                while self.has_unconsumed_tail:
                    chunk = self.decompress(b"")
                    if not chunk and self.has_unconsumed_tail:
                        raise ValueError("output_limit")
                    chunks.append(chunk)
                chunk = decoder.flush()
                budget.decoded(len(chunk))
                return b"".join((*chunks, chunk))

        return Decoder()

    def decoder_type(encoding):
        class Decoder:
            def __init__(self):
                self.decoder = bounded_decoder(encoding)

            def decode(self, data):
                chunks = [self.decoder.decompress(data)]
                while self.decoder.has_unconsumed_tail:
                    chunk = self.decoder.decompress(b"")
                    if not chunk and self.decoder.has_unconsumed_tail:
                        raise ValueError("output_limit")
                    chunks.append(chunk)
                return b"".join(chunks)

            def flush(self):
                return self.decoder.flush()
        return Decoder

    def decode_all(encoding):
        def decode(data):
            decoder = decoder_type(encoding)()
            return decoder.decode(data) + decoder.flush()
        return staticmethod(decode)

    def guarded_begin(response, *args, **kwargs):
        answer = begin(response, *args, **kwargs)
        if response.status in {301, 302, 303, 307, 308}:
            budget.redirect()
        return answer

    def bounded_read(fn):
        def run(response, amt=None):
            budget.check()
            remaining = max(1, MAX_NETWORK_BYTES - budget.bytes + 1)
            data = fn(response, min(amt, remaining) if amt is not None and amt >= 0 else remaining)
            budget.received(len(data))
            return data
        return run

    async def delegated(parser, url):
        validate_url(url)
        budget.check()
        active = stack.get()
        key = (type(parser).__name__, url)
        budget.delegations += 1
        if key in active or len(active) >= MAX_DEPTH or budget.delegations > MAX_DELEGATIONS:
            raise ValueError("delegation_limit")
        token = stack.set((*active, key))
        try:
            return await parse_original(parser, url)
        finally:
            stack.reset(token)

    class QuietLogger:
        def debug(self, *args, **kwargs): pass
        info = warning = error = debug

    class BoundedYDL(yt_dlp.YoutubeDL):
        def process_ie_result(self, info, download=True, extra_info=None):
            budget.check()
            kind = info.get("_type", "video")
            if kind in {"playlist", "multi_video"}:
                raise ValueError("scope_limited")
            if kind in {"url", "url_transparent"}:
                validate_url(info.get("url"))
                budget.delegations += 1
                if budget.delegations > MAX_DELEGATIONS:
                    raise ValueError("delegation_limit")
            return super().process_ie_result(info, download=False, extra_info=extra_info)

    async def extract_info(parser, url):
        # Keep the fixed upstream's yt-dlp delegation inside this guarded process.
        with BoundedYDL({"quiet": True, "no_warnings": True, "logger": QuietLogger(),
                         "skip_download": True, "noplaylist": True, "cachedir": False,
                         "cookiefile": None, "cookiesfrombrowser": None, "proxy": "",
                         "usenetrc": False, "enable_file_urls": False, "remote_components": set(),
                         "js_runtimes": {}, "external_downloader": None,
                         "format": "all", "check_formats": False,
                         "socket_timeout": max(.1, budget.deadline - time.monotonic()),
                         "retries": 0, "extractor_retries": 0, "extract_flat": False}) as ydl:
            restrict_ytdlp_transport(ydl)
            return ydl.extract_info(url, download=False)

    with contextlib.ExitStack() as patches:
        for obj, name, value in [(httpx, "AsyncClient", AsyncClient), (httpx, "Client", Client),
                                 (http.client.HTTPConnection, "putrequest", guarded_request),
                                 (http.client.HTTPConnection, "putheader", guarded_header),
                                 (http.client.HTTPResponse, "read", bounded_read(read)),
                                 (http.client.HTTPResponse, "read1", bounded_read(read1)),
                                 (http.client.HTTPResponse, "begin", guarded_begin),
                                 (urllib3.response, "_get_decoder", bounded_decoder),
                                 (HTTPHandler, "gz", decode_all("gzip")),
                                 (HTTPHandler, "deflate", decode_all("deflate")),
                                 (HTTPHandler, "brotli", decode_all("br")),
                                 (BaseParser, "parse", delegated), (YtParser, "_extract_info", extract_info)]:
            patches.enter_context(patch.object(obj, name, value))
        patches.enter_context(patch.dict(httpx._decoders.SUPPORTED_DECODERS, {
            encoding: decoder_type(encoding) for encoding in ("gzip", "deflate", "br", "zstd")
            if encoding in httpx._decoders.SUPPORTED_DECODERS}))
        yield


async def parse(source, timeout, parser_cls, budget=None, observe=None):
    from parsehub.utils.helpers import SecretCookie
    source = upstream_source(source)
    budget = budget or Budget(timeout)
    if parser_cls.__name__ == "ZhihuParser" and re.fullmatch(r"/question/\d+/?", urlsplit(source).path):
        # This pinned provider otherwise substitutes the first answer for a question.
        raise ValueError("scope_limited")
    exception, _ = source_identity(source)
    with execution_boundary(budget, source, parser_cls.__name__):
        async with asyncio.timeout(timeout):
            result = await parser_cls(proxy=None, cookie=SecretCookie(None)).parse(source)
        if observe is not None:
            observe(result)
        budget.check()
        if parser_cls.__name__ in {"CoolapkParser", "DoubanParser"}:
            actual_parser, actual_id = source_identity(getattr(result, "raw_url", ""))
            if not budget.identity or actual_parser != parser_cls.__name__ or actual_id not in budget.response_ids:
                raise ValueError("identity_mismatch")
        rows = normalize_result(result, source, extractor=parser_cls.__name__,
                                identity_evidence="upstream_response_id" if budget.identity else "upstream_parser_result")
        if exception == "CoolapkParser":
            urls = [row[1] for row in rows if row[0] == 3]
            if urls != (budget.body_media or [])[:len(urls)]:
                raise ValueError("identity_mismatch")
        return rows


def install_execution_guard(budget):
    import socket
    import ssl
    install_network_guard(offline=False)

    def counted(fn):
        def receive(sock, *args, **kwargs):
            budget.check()
            value = fn(sock, *args, **kwargs)
            budget.wire_bytes += len(value) if isinstance(value, bytes) else int(value or 0)
            if budget.wire_bytes > MAX_WIRE_BYTES:
                raise ValueError("output_limit")
            return value
        return receive

    # Also cover clients below httpx/stdlib, TLS plaintext, and recv_into paths.
    # Encrypted + plaintext reads may count twice; this is a separate wire budget.
    for cls, names in [(socket.socket, ("recv", "recv_into")), (ssl.SSLSocket, ("read",)),
                       (ssl.SSLObject, ("read",))]:
        for name in names:
            setattr(cls, name, counted(getattr(cls, name)))
    def audit(event, args):
        if event == "socket.connect":
            budget.check()
            budget.connections += 1
            if budget.connections > MAX_REQUESTS:
                raise ValueError("request_limit")
        elif event == "socket.getaddrinfo":
            budget.check()
    sys.addaudithook(audit)


def main():
    stage = "request"
    try:
        request = json.loads(sys.stdin.read(8193))
        if not isinstance(request, dict) or set(request) - {"action", "url", "match", "timeout_seconds"}:
            raise ValueError("invalid_request")
        for key in tuple(os.environ):
            if key not in {"PATH", "HOME", "LANG", "TMPDIR"}:
                os.environ.pop(key, None)
        # GMP discovery can spawn ldconfig on Linux; use Cryptodome's bundled fallback.
        os.environ["PYCRYPTODOME_DISABLE_GMP"] = "1"
        verify_install()
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_AS, (1536 * 1024 * 1024, 1536 * 1024 * 1024))
            resource.setrlimit(resource.RLIMIT_CPU, (40, 40))
        except (ImportError, ValueError, OSError):
            # Wall time/output/process-group cleanup are also enforced by parent.
            pass
        action = request.get("action")
        importing = [True]
        def import_guard(event, args):
            if importing[0] and event in {"socket.connect", "socket.getaddrinfo", "socket.sendto",
                                         "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}:
                raise ValueError("execution_denied")
        sys.addaudithook(import_guard)
        stage = "import"
        with open(os.devnull, "w") as silent, contextlib.redirect_stdout(silent), contextlib.redirect_stderr(silent):
            registry()
            import httpx  # noqa: F401
            import yt_dlp  # noqa: F401
        importing[0] = False
        if action in {"identity", "catalog", "match"}:
            install_network_guard(offline=True)
            if action == "identity":
                payload = {"provider_id": PROVIDER_ID, "version": PROVIDER_VERSION}
            elif action == "catalog":
                payload = {"provider_id": PROVIDER_ID, "version": PROVIDER_VERSION,
                           "entries": [entry(cls) for cls in registry()]}
            else:
                payload = match_url(request.get("url"))
        elif action == "parse":
            source = request.get("url")
            matches = match_url(source)
            if request.get("match") not in matches:
                raise ValueError("extractor_mismatch")
            timeout = timeout_value(request.get("timeout_seconds", 15))
            budget = Budget(timeout)
            install_execution_guard(budget)
            stage = "parse"
            with open(os.devnull, "w") as silent, contextlib.redirect_stdout(silent), contextlib.redirect_stderr(silent):
                payload = asyncio.run(parse(source, timeout, import_parser(request["match"]["extractor_id"]), budget))
        else:
            raise ValueError("invalid_request")
        output = json.dumps({"ok": True, "payload": payload})
        if len(output.encode()) > MAX_BYTES:
            raise ValueError("output_limit")
    except Exception as error:
        output = json.dumps(failure_summary(error, stage))
    sys.stdout.write(output)


if __name__ == "__main__":
    main()
