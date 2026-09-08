"""Exact-pin, anonymous ParseHub single-post boundary; never a downloader."""

import asyncio
import contextlib
import importlib
from importlib import metadata
import json
import math
import os
from pathlib import Path
import re
import sys
from urllib.parse import parse_qs, unquote, urlsplit

try:
    from .upstream_match_worker import install_network_guard, safe_error, validate_url
except ImportError:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from upstream_match_worker import install_network_guard, safe_error, validate_url

PIN = "144260f4349c5bb57415f7555a572fcd48c3b8ba"
VERSION = "2.2.0"
PROVIDER_ID = "parsehub_generic"
PROVIDER_VERSION = f"parsehub@{VERSION}+{PIN}"
MAX_ASSETS = 30
MAX_BYTES = 2 * 1024 * 1024
IMAGE_EXTENSIONS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff", "avif"}
VIDEO_EXTENSIONS = {"mp4", "m4v", "mov", "webm"}
PARSERS = {
    "TieBaParser": ("tieba", "Tieba"),
    "PipixParser": ("pipix", "Pipix"),
    "ZuiYouParser": ("zuiyou", "Zuiyou"),
}


def source_identity(url):
    validate_url(url)
    p = urlsplit(url)
    if p.scheme != "https" or p.fragment or p.netloc != p.hostname:
        raise ValueError("invalid_url")
    if p.hostname == "tieba.baidu.com" and not p.query:
        match = re.fullmatch(r"/p/([1-9][0-9]{0,19})/?", p.path)
        if match:
            return "TieBaParser", match[1]
    if p.hostname == "h5.pipix.com" and not p.query:
        match = re.fullmatch(r"/ppx/item/([1-9][0-9]{0,19})/?", p.path)
        if match:
            return "PipixParser", match[1]
    if (p.hostname == "share.xiaochuankeji.cn" and
            p.path == "/hybrid/share/post" and
            re.fullmatch(r"pid=[1-9][0-9]{0,19}", p.query)):
        return "ZuiYouParser", parse_qs(p.query)["pid"][0]
    raise ValueError("extractor_mismatch")


def match_url(url):
    try:
        extractor, _ = source_identity(url)
    except (ValueError, TypeError):
        return ()
    return ({"provider_id": PROVIDER_ID, "extractor_id": extractor,
             "label": PARSERS[extractor][1], "version": PROVIDER_VERSION},)


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
        if code in {"identity_mismatch", "unsupported_result", "insecure_media", "media_type_conflict",
                    "request_limit", "invalid_request", "output_limit"}:
            return {"ok": False, "error": code, "stage": stage,
                    "error_class": type(current).__name__}
        response = getattr(current, "response", None)
        if response is not None and getattr(response, "status_code", 0) >= 400:
            return {"ok": False, "error": "access_denied" if response.status_code in {401, 403} else "upstream_unavailable",
                    "stage": stage, "error_class": type(current).__name__}
        current = getattr(current, "cause", None) or current.__cause__
        if current is None:
            break
    return safe_error(error, stage)


def normalize_result(result, source):
    extractor, content_id = source_identity(source)
    if source_identity(getattr(result, "raw_url", "")) != (extractor, content_id):
        raise ValueError("identity_mismatch")
    if type(result).__name__ not in {"ImageParseResult", "VideoParseResult", "MultimediaParseResult"}:
        raise ValueError("unsupported_result")
    media = getattr(result, "media", None)
    media = list(media) if isinstance(media, (list, tuple)) else [media] if media else []
    if not media or len(media) > MAX_ASSETS:
        raise ValueError("item_limit" if media else "no_formats")
    messages = []
    for item in media:
        kind = type(item).__name__
        if kind not in {"ImageRef", "AniRef", "VideoRef"}:
            raise ValueError("unsupported_result")
        url = validate_url(getattr(item, "url", ""))
        if urlsplit(url).scheme != "https":
            raise ValueError("insecure_media")
        video = kind == "VideoRef"
        ext = str(getattr(item, "ext", "") or "").lower().lstrip(".")
        url_ext = Path(urlsplit(url).path).suffix.lower().lstrip(".")
        conflicting = IMAGE_EXTENSIONS if video else VIDEO_EXTENSIONS
        if ext in conflicting or url_ext in conflicting:
            raise ValueError("media_type_conflict")
        messages.append([3, url, {
            "extension": ext, "media_type": "video" if video else "image",
            # A reference-type hint for the existing gallery mapper, not a MIME probe.
            "mime_type": "video/unknown" if video else "image/unknown",
            "mime_evidence": "upstream_reference_type",
            "title": str(getattr(result, "title", "") or "")[:200],
            "source_id": content_id, "extractor_id": extractor,
            "identity_evidence": "upstream_response_id",
            "media_completeness": "unknown" if video else "image_candidate",
            "requires_completeness_check": video,
        }])
    return messages


def import_parser(extractor):
    module, _ = PARSERS[extractor]
    return getattr(importlib.import_module(f"parsehub.parsers.parser.{module}"), extractor)


def verify_response_identity(extractor, content_id, response):
    if extractor == "TieBaParser":
        actual = response.json().get("thread", {}).get("id")
    elif extractor == "ZuiYouParser":
        actual = response.json().get("data", {}).get("post", {}).get("id")
    else:
        from bs4 import BeautifulSoup
        node = BeautifulSoup(response.text, "html.parser").find("script", id="RENDER_DATA")
        payload = json.loads(unquote(node.text)) if node else {}
        # The numeric item_id is rounded in page JSON; the string is authoritative.
        actual = payload.get("ppxItemDetail", {}).get("item", {}).get("item_id_str")
    if str(actual) != content_id:
        raise ValueError("identity_mismatch")


def bounded_http(extractor, content_id, source, timeout):
    """Constrain the reviewed upstream HTTP clients, including streaming bodies."""
    import httpx
    original = httpx.AsyncClient
    state = {"requests": 0, "bytes": 0, "identity": False}
    endpoints = {
        "TieBaParser": {("GET", "http://tieba.baidu.com/dc/common/tbs"),
                        ("POST", "https://tieba.baidu.com/c/f/pb/page_pc")},
        "PipixParser": {("GET", source)},
        "ZuiYouParser": {("POST", "https://share.xiaochuankeji.cn/planck/share/post/detail_h5")},
    }[extractor]

    class Client(original):
        def __init__(self, *args, **kwargs):
            kwargs.update(trust_env=False, proxy=None, cookies=None, auth=None,
                          verify=True, timeout=timeout, follow_redirects=False)
            super().__init__(*args, **kwargs)

        async def send(self, request, **kwargs):
            state["requests"] += 1
            if state["requests"] > MAX_ASSETS + 2:
                raise ValueError("request_limit")
            target = str(request.url)
            is_head = extractor == "TieBaParser" and request.method == "HEAD"
            if is_head:
                validate_url(target)
                if urlsplit(target).scheme != "https":
                    raise ValueError("network_denied")
            elif (request.method, target) not in endpoints:
                raise ValueError("network_denied")
            for name in ("cookie", "authorization", "proxy-authorization"):
                request.headers.pop(name, None)
            response = await super().send(request, stream=True, follow_redirects=False)
            try:
                response.raise_for_status()
                chunks = []
                if not is_head:
                    async for chunk in response.aiter_bytes():
                        state["bytes"] += len(chunk)
                        if state["bytes"] > MAX_BYTES:
                            raise ValueError("output_limit")
                        chunks.append(chunk)
                response._content = b"".join(chunks)
                if not is_head and not target.endswith("/dc/common/tbs"):
                    verify_response_identity(extractor, content_id, response)
                    state["identity"] = True
                return response
            finally:
                await response.aclose()

    return original, Client, state


async def parse(source, timeout, parser_cls):
    import httpx
    from parsehub.utils.helpers import SecretCookie
    extractor, content_id = source_identity(source)
    original, client, state = bounded_http(extractor, content_id, source, timeout)
    httpx.AsyncClient = client
    try:
        async with asyncio.timeout(timeout):
            result = await parser_cls(proxy=None, cookie=SecretCookie(None)).parse(source)
        if not state["identity"]:
            raise ValueError("identity_mismatch")
        return normalize_result(result, source)
    finally:
        httpx.AsyncClient = original


def main():
    stage = "request"
    try:
        request = json.loads(sys.stdin.read(8193))
        if not isinstance(request, dict) or set(request) - {"action", "url", "match", "timeout_seconds"}:
            raise ValueError("invalid_request")
        for key in tuple(os.environ):
            if key not in {"PATH", "HOME", "LANG", "TMPDIR"}:
                os.environ.pop(key, None)
        verify_install()
        action = request.get("action")
        if action == "identity":
            payload = {"provider_id": PROVIDER_ID, "version": PROVIDER_VERSION}
        elif action == "match":
            payload = match_url(request.get("url"))
        elif action == "parse":
            source = request.get("url")
            matches = match_url(source)
            if len(matches) != 1 or request.get("match") != matches[0]:
                raise ValueError("extractor_mismatch")
            timeout = timeout_value(request.get("timeout_seconds", 15))
            # Native library initialization is needed by ParseHub's result imports.
            # It may not access the network or start a process, even before guards.
            importing = [True]
            def import_guard(event, args):
                if importing[0] and (event.startswith("socket.") or event in {
                        "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn"}):
                    raise ValueError("execution_denied")
            sys.addaudithook(import_guard)
            stage = "import"
            with contextlib.redirect_stdout(sys.stderr):
                parser_cls = import_parser(matches[0]["extractor_id"])
            importing[0] = False
            install_network_guard(offline=False)
            stage = "parse"
            with contextlib.redirect_stdout(sys.stderr):
                payload = asyncio.run(parse(source, timeout, parser_cls))
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
