"""One-shot upstream API boundary. Never import this to install parent guards."""

import contextlib
import http.client
import importlib.metadata
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import socket
import signal
import sys
import subprocess
import time
import zlib
from urllib.parse import urljoin, urlsplit, urldefrag


PROVIDERS = {"yt_dlp_generic": "yt-dlp", "gallery_dl_generic": "gallery-dl"}
# Catch-all and synthetic engines are not independent site coverage.
YTDLP_EXCLUDED = frozenset({"Generic", "UnsupportedURL", "TestURL"})
# Its nominal image URL runs an artist/title search and can return other paintings.
GALLERY_EXCLUDED = frozenset({"WikiartImageExtractor"})
# Established routes own these families; they are not newly opened dynamic coverage.
# Keep this small override map separate from upstream's bulk extractor inventory.
ESTABLISHED_ROUTE_PREFIXES = {
    "youtube": ("youtube",),
    "bilibili": ("bilibili",),
    "xiaohongshu": ("xiaohongshu", "xhs"),
    "douyin": ("douyin",),
    "tiktok": ("tiktok",),
    "kuaishou": ("kwai", "kuaishou"),
    "instagram": ("instagram",),
    "facebook": ("facebook",),
    "twitter": ("twitter",),
    "imgur": ("imgur",),
    "reddit": ("reddit",),
    "tumblr": ("tumblr",),
    "flickr": ("flickr",),
    "deviantart": ("deviantart",),
}
_ROUTED_PREFIXES = tuple(prefix for prefixes in ESTABLISHED_ROUTE_PREFIXES.values()
                         for prefix in prefixes)
MAX_URL = 4096
MAX_ITEMS = 100
MAX_OUTPUT = 4 * 1024 * 1024
MAX_REQUESTS = 40
MAX_REDIRECTS = 10
MAX_HOPS = 16
MAX_FORMATS = 1000
MAX_BODY = 8 * 1024 * 1024
MAX_RETAINED_CANDIDATES = 1000
DESCRIPTOR_FIELDS = frozenset({
    "url", "id", "title", "uploader", "creator", "author", "extractor", "extractor_key",
    "webpage_url", "original_url", "duration", "width", "height", "fps", "tbr", "abr", "vbr",
    "bitrate", "filesize", "filesize_approx", "format_id", "format_note", "format", "resolution",
    "vcodec", "acodec", "audio_ext", "video_ext", "ext", "extension", "protocol", "mime_type",
    "media_type", "type", "audio_channels", "language", "quality", "preference", "source_preference",
    "format_index", "upstream_candidate_index", "dynamic_range", "has_drm", "manifest_url",
    "media_completeness", "candidate_kind", "work_label", "relationship_id", "relationship_role",
    "filename", "category", "subcategory", "num", "count", "date", "description",
    "requires_merge", "role", "audio_asset_present", "work_id", "upstream_asset_id", "asset_id",
    "audio_tracks", "video_tracks", "upstream_role",
})


def compact_descriptor(value):
    """Retain media evidence, not large extractor internals or duplicate payloads."""
    result = {}
    for key in DESCRIPTOR_FIELDS:
        item = value.get(key)
        if item is None or isinstance(item, (bool, int, float)):
            if key in value:
                result[key] = item
        elif isinstance(item, str):
            identity = key.endswith('url') or key in {
                'id', 'format_id', 'asset_id', 'upstream_asset_id', 'work_id', 'relationship_id'}
            result[key] = item if identity else item[:300]
    for key in ("upstream_track_evidence", "upstream_bmff_track_evidence", "upstream_scope",
                "http_headers", "request_context", "headers"):
        item = value.get(key)
        if isinstance(item, dict):
            result[key] = {k: v
                           for k, v in list(item.items())[:32]
                           if isinstance(k, str) and (v is None or isinstance(v, (str, bool, int, float)))}
    if isinstance(value.get('relationships'), (list, tuple)):
        result['relationships'] = [
            {key: relation[key] for key in
             ('relationship_type', 'source_asset_id', 'target_asset_id') if isinstance(relation.get(key), str)}
            for relation in value['relationships'][:16] if isinstance(relation, dict)
            and all(isinstance(relation.get(key), str) and 0 < len(relation[key]) <= 120
                    for key in ('relationship_type', 'source_asset_id', 'target_asset_id'))]
    return result
_STAGE = "request"


def safe_error(error, stage=None, _depth=0):
    """Only return controlled codes and class names, never exception messages."""
    stage = stage or _STAGE
    name = type(error).__name__
    code = {
        "network_denied": "network_denied", "execution_denied": "execution_guard",
        "delegation_denied": "delegation_denied", "collection_denied": "playlist_denied",
        "collection_or_queue_denied": "playlist_denied", "non_item_result": "playlist_denied",
        "item_limit": "item_limit", "output_limit": "output_limit",
        "version_changed": "version_changed", "extractor_mismatch": "extractor_mismatch",
        "invalid_url": "invalid_url", "no_formats": "no_formats",
        "request_limit": "request_limit", "redirect_limit": "redirect_limit",
        "cycle_detected": "cycle_detected", "hop_limit": "hop_limit",
        "body_limit": "body_limit", "invalid_request": "invalid_request",
        "encoding_denied": "encoding_denied", "node_limit": "node_limit",
    }.get(str(error))
    cause = getattr(error, "cause", None) or error.__cause__
    if code is None and cause is not None and cause is not error and _depth < 4:
        child = safe_error(cause, stage, _depth + 1)
        if child["error"] != "worker_error":
            return child
    if code is None:
        if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) or "Timeout" in name:
            code = "network_timeout"
        elif isinstance(error, (ImportError, importlib.metadata.PackageNotFoundError)):
            code = "unavailable"
        elif name in {"SSLError", "SSLCertVerificationError", "CertificateVerifyError"}:
            code = "tls_error"
        elif name in {"LoginRequired", "AuthenticationError", "AuthorizationError"}:
            code = "auth_required"
        elif getattr(error, "status", None) in {401, 403}:
            code = "access_denied"
        elif name in {"NotFoundError", "NoExtractorError", "UnsupportedError"}:
            code = "upstream_unavailable"
        elif getattr(error, "expected", False):
            message = str(error).lower()
            if any(token in message for token in ("login", "log in", "sign in", "password", "authentication")):
                code = "auth_required"
            elif "no video formats" in message or "no formats" in message:
                code = "no_formats"
            else:
                code = "upstream_unavailable"
        else:
            code = "worker_error"
    return {"ok": False, "error": code, "stage": stage,
            "error_class": re.sub(r"[^A-Za-z0-9_]", "", name)[:80]}


def public_ip(value):
    address = ipaddress.ip_address(value)
    if (not address.is_global or address.is_multicast or
            getattr(address, "ipv4_mapped", None) or
            getattr(address, "sixtofour", None) or
            getattr(address, "teredo", None)):
        raise ValueError("network_denied")
    return str(address)


def validate_url(url):
    return _validate_public_url(url, MAX_URL)


def validate_media_url(url):
    return _validate_public_url(url, 8192)


def _validate_public_url(url, max_length):
    if not isinstance(url, str) or not url or len(url) > max_length:
        raise ValueError("invalid_url")
    if any(ord(char) <= 32 or ord(char) == 127 for char in url) or "\\" in url:
        raise ValueError("invalid_url")
    parts = urlsplit(url)
    if (parts.scheme not in {"http", "https"} or not parts.hostname or
            parts.username is not None or parts.password is not None or
            parts.port not in {None, 80, 443}):
        raise ValueError("invalid_url")
    host = parts.hostname.lower().rstrip(".")
    if host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        raise ValueError("invalid_url")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        public_ip(host)
    return url


def install_network_guard(*, offline):
    """Resolve once and connect numerically; re-check every redirect/reconnect."""
    resolve = socket.getaddrinfo
    connect = socket.socket.connect
    connect_ex = socket.socket.connect_ex

    def checked_resolve(host, port, family=0, type=0, proto=0, flags=0):
        if offline or int(port) not in {80, 443}:
            raise ValueError("network_denied")
        answers = resolve(host, port, family, type, proto, flags)
        if not answers:
            raise ValueError("network_denied")
        for answer in answers:
            public_ip(answer[4][0])
        return answers

    def destination(sock, address):
        if (offline or sock.family not in {socket.AF_INET, socket.AF_INET6} or
                sock.type & 0xF != socket.SOCK_STREAM or not isinstance(address, tuple)):
            raise ValueError("network_denied")
        answers = checked_resolve(address[0], address[1], sock.family, socket.SOCK_STREAM)
        return answers[0][4]

    def guarded_connect(sock, address):
        return connect(sock, destination(sock, address))

    def guarded_connect_ex(sock, address):
        return connect_ex(sock, destination(sock, address))

    def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT,
                          source_address=None, *, all_errors=False):
        if source_address:
            raise ValueError("network_denied")
        last_error = None
        for family, kind, proto, _, target in checked_resolve(
                *address, type=socket.SOCK_STREAM):
            sock = socket.socket(family, kind, proto)
            try:
                if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                    sock.settimeout(timeout)
                # target is numeric; the audit hook also validates the actual connect.
                connect(sock, target)
                return sock
            except OSError as error:
                last_error = error
                sock.close()
        raise last_error or ValueError("network_denied")

    def audit(event, args):
        if event == "socket.getaddrinfo" and offline:
            raise ValueError("network_denied")
        if event == "socket.connect":
            sock, address = args
            if (offline or sock.family not in {socket.AF_INET, socket.AF_INET6} or
                    sock.type & 0xF != socket.SOCK_STREAM or int(address[1]) not in {80, 443}):
                raise ValueError("network_denied")
            public_ip(address[0])
        elif event in {"socket.sendto", "socket.sendmsg", "subprocess.Popen",
                       "os.system", "os.exec", "os.posix_spawn", "ctypes.dlopen"}:
            raise ValueError("execution_denied")

    socket.getaddrinfo = checked_resolve
    socket.socket.connect = guarded_connect
    socket.socket.connect_ex = guarded_connect_ex
    socket.create_connection = create_connection
    sys.addaudithook(audit)


def package_identity(provider):
    dist = importlib.metadata.distribution(PROVIDERS[provider])
    root = Path(dist.locate_file(""))
    module = "yt_dlp" if provider == "yt_dlp_generic" else "gallery_dl"
    watched = [root, root / module, root / module / "version.py",
               root / module / "__init__.py", root / module / "extractor"]
    watched.extend(dist.locate_file(path) for path in dist.files or ()
                   if str(path).endswith(".dist-info/METADATA"))
    return {"version": dist.version, "package_path": str(root),
            "watch_paths": [str(path) for path in watched]}


def extractor_classes(provider):
    if provider == "yt_dlp_generic":
        from yt_dlp.extractor import gen_extractor_classes
        return gen_extractor_classes()
    from gallery_dl.extractor import extractors
    return extractors()


def already_routed(provider, cls):
    names = ((cls.ie_key(), str(getattr(cls, "IE_NAME", ""))) if provider == "yt_dlp_generic"
             else (cls.__name__, cls.category))
    return any(name.lower().startswith(_ROUTED_PREFIXES) for name in names)


def eligible(provider, cls, *, allow_mature=False):
    if not allow_mature and already_routed(provider, cls):
        return False
    if provider == "yt_dlp_generic":
        key = cls.ie_key()
        return (getattr(cls, "IE_DESC", None) is not False and
                key not in YTDLP_EXCLUDED and
                getattr(cls, "_VALID_URL", None) not in (None, False))
    return (cls.__name__ not in GALLERY_EXCLUDED and
            bool(getattr(cls, "pattern", None)) and
            cls.category not in {"generic", "directlink", "recursive", "ytdl", "oauth"})


def entry(provider, cls, version):
    extractor_id = cls.ie_key() if provider == "yt_dlp_generic" else cls.__name__
    label = cls.IE_NAME if provider == "yt_dlp_generic" else f"{cls.category}:{cls.subcategory}"
    return {"provider_id": provider, "extractor_id": extractor_id,
            "label": str(label)[:160], "version": version}


def suitable(provider, cls, url):
    if provider == "yt_dlp_generic":
        return cls.suitable(url)
    return re.match(cls.pattern, url) is not None


@contextlib.contextmanager
def runtime_budget(timeout):
    """Meter stdlib HTTP (including urllib3) below upstream retry/redirect APIs."""
    from unittest.mock import patch
    deadline = time.monotonic() + timeout
    state = {"requests": 0, "redirects": 0, "bytes": 0, "decoded_bytes": 0}
    edges = {}
    putrequest = http.client.HTTPConnection.putrequest
    putheader = http.client.HTTPConnection.putheader
    getresponse = http.client.HTTPConnection.getresponse
    readinto = http.client.HTTPResponse.readinto

    def check():
        if time.monotonic() >= deadline:
            raise TimeoutError()

    def request(conn, method, target, *args, **kwargs):
        check()
        scheme = "https" if isinstance(conn, http.client.HTTPSConnection) or conn.port == 443 else "http"
        host = conn.host
        if ":" in host and not host.startswith("["):
            host = "[" + host + "]"
        url = validate_url(urljoin(f"{scheme}://{host}:{conn.port}/", target))
        state["requests"] += 1
        if state["requests"] > MAX_REQUESTS:
            raise ValueError("request_limit")
        conn._upstream_url = urldefrag(url)[0]
        return putrequest(conn, method, target, *args, **kwargs)

    def header(conn, name, *values):
        # Drop both caller-supplied credentials and ambient session cookies.
        if (name.decode("ascii") if isinstance(name, bytes) else name).lower() in {
                "cookie", "authorization", "proxy-authorization"}:
            return None
        if (name.decode("ascii") if isinstance(name, bytes) else name).lower() == "accept-encoding":
            values = ("gzip, deflate",)
        return putheader(conn, name, *values)

    def response(conn, *args, **kwargs):
        check()
        result = getresponse(conn, *args, **kwargs)
        if result.status in {301, 302, 303, 307, 308} and result.getheader("Location"):
            state["redirects"] += 1
            if state["redirects"] > MAX_REDIRECTS:
                raise ValueError("redirect_limit")
            source = conn._upstream_url
            dest = urldefrag(validate_url(urljoin(source, result.getheader("Location"))))[0]
            edges[source] = dest
            seen = {source}
            while dest in edges:
                if dest in seen:
                    raise ValueError("cycle_detected")
                seen.add(dest)
                dest = edges[dest]
        return result

    def limited_read(original):
        def read(response, amount=None, *args, **kwargs):
            check()
            remaining = MAX_BODY - state["bytes"]
            amount = min(remaining + 1, amount) if amount is not None and amount >= 0 else remaining + 1
            result = original(response, amount, *args, **kwargs)
            state["bytes"] += len(result)
            if state["bytes"] > MAX_BODY:
                raise ValueError("body_limit")
            return result
        return read

    def expired(signum, frame):
        raise TimeoutError()

    def decoded(data):
        check()
        state["decoded_bytes"] += len(data)
        if state["decoded_bytes"] > MAX_BODY:
            raise ValueError("body_limit")
        return data

    def inflate(data, wbits):
        check()
        # max_length bounds allocation before checking the decoded byte count.
        return decoded(zlib.decompressobj(wbits).decompress(
            data, MAX_BODY - state["decoded_bytes"] + 1))

    def deflate(data):
        try:
            return inflate(data, -zlib.MAX_WBITS)
        except zlib.error:
            return inflate(data, zlib.MAX_WBITS)

    def unsupported_encoding(*args, **kwargs):
        raise ValueError("encoding_denied")

    try:
        from urllib3.response import BaseHTTPResponse
    except ImportError:
        BaseHTTPResponse = None
    original_decode = BaseHTTPResponse._decode if BaseHTTPResponse else None

    def bounded_decode(response, data, decode_content, flush_decoder, max_length=None):
        check()
        encoding = response.headers.get("content-encoding", "").lower().strip()
        if decode_content and encoding not in {"", "identity", "gzip", "x-gzip", "deflate"}:
            raise ValueError("encoding_denied")
        # The pinned urllib3 decoder supports max_length. Do not use its flush
        # path, which resets max_length to unlimited; zlib emits data on decompress.
        remaining = MAX_BODY - state["decoded_bytes"] + 1
        if max_length is not None and max_length >= 0:
            remaining = min(remaining, max_length)
        return decoded(original_decode(response, data, decode_content, False, max_length=remaining))

    def limited_readinto(response, buffer):
        check()
        # HTTPResponse.readinto bypasses read() for non-chunked bodies.
        if response.chunked:
            data = response.read(len(buffer))
            buffer[:len(data)] = data
            return len(data)
        remaining = MAX_BODY - state["bytes"]
        size = readinto(response, memoryview(buffer)[:remaining + 1])
        state["bytes"] += size
        if state["bytes"] > MAX_BODY:
            raise ValueError("body_limit")
        return size

    with contextlib.ExitStack() as stack:
        try:
            from yt_dlp.networking._urllib import HTTPHandler
        except ImportError:
            HTTPHandler = None
        if HTTPHandler:
            stack.enter_context(patch.object(HTTPHandler, "gz", staticmethod(
                lambda data: inflate(data, zlib.MAX_WBITS | 16))))
            stack.enter_context(patch.object(HTTPHandler, "deflate", staticmethod(deflate)))
            stack.enter_context(patch.object(HTTPHandler, "brotli", staticmethod(unsupported_encoding)))
        if BaseHTTPResponse:
            stack.enter_context(patch.object(BaseHTTPResponse, "_decode", bounded_decode))
        stack.enter_context(patch.object(http.client.HTTPConnection, "putrequest", request))
        stack.enter_context(patch.object(http.client.HTTPConnection, "putheader", header))
        stack.enter_context(patch.object(http.client.HTTPConnection, "getresponse", response))
        stack.enter_context(patch.object(http.client.HTTPResponse, "readinto", limited_readinto))
        for name in ("read", "read1"):
            stack.enter_context(patch.object(http.client.HTTPResponse, name,
                                            limited_read(getattr(http.client.HTTPResponse, name))))
        previous = signal.signal(signal.SIGALRM, expired)
        signal.setitimer(signal.ITIMER_REAL, timeout)
        try:
            yield state
        finally:
            signal.setitimer(signal.ITIMER_REAL, 0)
            signal.signal(signal.SIGALRM, previous)


def parse_ytdlp(cls, url, timeout):
    global _STAGE
    _STAGE = "ytdlp_import"
    from yt_dlp import YoutubeDL
    cls = getattr(cls, "real_class", cls)

    class ExactYoutubeDL(YoutubeDL):
        hops = 0
        items = 0
        nodes = 0
        depth = 0
        limit_reason = None
        output_bytes = 0
        retained_formats = 0

        def account_output(self, result):
            for chunk in json.JSONEncoder().iterencode(self.sanitize_info(result)):
                self.output_bytes += len(chunk.encode())
                if self.output_bytes > MAX_OUTPUT - 16384:
                    self.limit_reason = "output_limit"
                    raise ValueError("output_limit")

        def extract_info(self, url, download=False, *args, **kwargs):
            validate_url(url)
            self.hops += 1
            if self.hops > MAX_HOPS:
                raise ValueError("hop_limit")
            key = (urldefrag(url)[0], kwargs.get("ie_key"))
            active = getattr(self, "_upstream_active", set())
            self._upstream_active = active
            if key in active:
                raise ValueError("cycle_detected")
            active.add(key)
            try:
                return super().extract_info(url, False, *args, **kwargs)
            finally:
                active.remove(key)

        def process_ie_result(self, result, download=False, extra_info=None):
            # One root plus MAX_ITEMS result nodes, including empty collections
            # and delegations. Video count alone does not bound a playlist tree.
            if self.nodes >= MAX_ITEMS + 1 or self.depth >= MAX_HOPS:
                self.limit_reason = "node_limit" if self.nodes >= MAX_ITEMS + 1 else "hop_limit"
                raise ValueError(self.limit_reason)
            self.nodes += 1
            self.depth += 1
            try:
                return self.process_bounded_result(result, extra_info)
            finally:
                self.depth -= 1

        def process_bounded_result(self, result, extra_info):
            if result.get("_type") in {"playlist", "multi_video", "compat_list"}:
                result = {**compact_descriptor(result), '_type': result['_type'],
                          'entries': result.get('entries')}
                entries = []
                reason = "item_limit"
                self.account_output({key: value for key, value in result.items() if key != "entries"})
                child_extra = {**(extra_info or {}), **{
                    key: result[key] for key in ("extractor", "extractor_key", "webpage_url")
                    if key in result}}
                try:
                    iterator = iter(result.get("entries") or ())
                    for _ in range(MAX_ITEMS):
                        if self.limit_reason or self.nodes >= MAX_ITEMS + 1:
                            reason = self.limit_reason or "node_limit"
                            break
                        item = next(iterator, None)
                        if item is None:
                            break
                        if item:
                            entries.append(self.process_ie_result(item, False, child_extra))
                except Exception as error:
                    reason = safe_error(error)["error"]
                    if not entries and reason not in {"node_limit", "hop_limit", "output_limit"}:
                        raise
                result["entries"] = entries
                result["upstream_scope"] = {"bounded": True, "limit": MAX_ITEMS,
                                             "node_limit": MAX_ITEMS + 1, "depth_limit": MAX_HOPS,
                                             "returned_count": len(entries), "retained_count": len(entries),
                                             "unexpanded_count": None,
                                             "completion": "unknown", "reason": reason}
                return result
            return super().process_ie_result(result, False, extra_info)

        def process_video_result(self, payload, download=False):
            self.items += 1
            if self.items > MAX_ITEMS:
                raise ValueError("item_limit")
            formats = payload.get("formats") or [payload]
            if not isinstance(formats, list):
                raise ValueError("item_limit")
            total = len(formats)
            if total > MAX_FORMATS:
                formats = payload["formats"] = formats[:MAX_FORMATS]
                payload["upstream_scope"] = {
                    "bounded": True, "completion": "unknown", "reason": "format_limit",
                    "format_total": total, "format_retained": len(formats), "limit": MAX_FORMATS}
            for index, item in enumerate(formats):
                if isinstance(item, dict):
                    item['upstream_candidate_index'] = index
                    item.setdefault("upstream_track_evidence", {
                        key: item.get(key) for key in ("acodec", "vcodec", "audio_ext", "video_ext")})
            result = super().process_video_result(payload, download=False)
            processed = result.get('formats') or [result]
            retained = []
            used = 0
            for item in sorted(processed, key=lambda item: item.get('upstream_candidate_index', 0)):
                if self.retained_formats + len(retained) >= MAX_RETAINED_CANDIDATES:
                    break
                descriptor = compact_descriptor(item)
                size = len(json.dumps(descriptor).encode()) + 2
                if self.output_bytes + used + size > MAX_OUTPUT - 32768:
                    break
                retained.append(descriptor)
                used += size
            result = compact_descriptor(result)
            result['formats'] = retained
            self.retained_formats += len(retained)
            if not retained:
                result.pop('url', None)
            if len(retained) < total:
                result['upstream_scope'] = {
                    'bounded': True, 'completion': 'unknown',
                    'reason': 'format_limit' if total > MAX_FORMATS else 'output_limit',
                    'format_total': total, 'format_retained': len(retained), 'limit': MAX_FORMATS,
                    'returned_count': total, 'retained_count': len(retained),
                    'declared_count': total, 'examined_count': len(processed), 'rejected_count': 0,
                    'unexpanded_count': total - len(retained)}
            self.account_output(result)
            return result

        def process_info(self, *args, **kwargs):
            raise ValueError("execution_denied")

        def dl(self, *args, **kwargs):
            raise ValueError("execution_denied")

    _STAGE = "ytdlp_initialize"
    with ExactYoutubeDL({
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "cookiefile": None, "cookiesfrombrowser": None,
        "usenetrc": False, "proxy": "", "nocheckcertificate": False,
        "cachedir": False, "socket_timeout": timeout, "retries": 0,
        "extractor_retries": 0, "enable_file_urls": False,
        "external_downloader": None, "remote_components": set(),
        "js_runtimes": {}, "format": "all", "check_formats": False,
        "ignore_no_formats_error": True,
    }) as ydl:
        # Force the transport metered above; native curl is outside that boundary.
        for key in list(ydl._request_director.handlers):
            if key != "Urllib":
                ydl._request_director.handlers.pop(key).close()
        ydl.add_info_extractor(cls(ydl))
        _STAGE = "ytdlp_extract"
        payload = ydl.extract_info(url, download=False, ie_key=cls.ie_key())
        _STAGE = "ytdlp_result"
        return ydl.sanitize_info(payload)


def parse_gallery(cls, url, timeout):
    global _STAGE
    _STAGE = "gallery_import"
    from gallery_dl import config, util
    from gallery_dl.extractor.message import Message
    config.clear()
    for key, value in {"cookies": None, "cookies-browser": None, "proxy": None,
                       "proxy-env": False, "netrc": False,
                       "verify": True, "timeout": timeout, "retries": 0}.items():
        config.set(("extractor",), key, value)
    _STAGE = "gallery_initialize"
    result = []
    output_bytes = 0
    media_count = 0
    hops = 0
    seen = set()
    registry = extractor_classes("gallery_dl_generic")

    def messages(extractor_cls, target):
        nonlocal hops
        validate_url(target)
        hops += 1
        if hops > MAX_HOPS:
            raise ValueError("hop_limit")
        key = (extractor_cls.__name__, urldefrag(target)[0])
        if key in seen:
            raise ValueError("cycle_detected")
        seen.add(key)
        instance = extractor_cls(re.match(extractor_cls.pattern, target))
        for message in instance:
            if message and message[0] == Message.Queue:
                target = validate_url(message[1])
                hinted = message[2].get("_extractor")
                candidates = [hinted] if hinted in registry else registry
                child = next((candidate for candidate in candidates
                              if eligible("gallery_dl_generic", candidate, allow_mature=True)
                              and suitable("gallery_dl_generic", candidate, target)), None)
                if child is None:
                    raise ValueError("extractor_mismatch")
                yield from messages(child, target)
            else:
                yield message

    _STAGE = "gallery_extract"
    reason = None
    try:
        for message in messages(cls, url):
            if not message or message[0] not in {Message.Directory, Message.Url}:
                raise ValueError("collection_or_queue_denied")
            if message[0] == Message.Url:
                validate_media_url(message[1])
            elif len(result) - media_count >= MAX_ITEMS:
                raise ValueError("item_limit")
            message = [*message[:-1], compact_descriptor(message[-1])]
            serialized = json.dumps(message, default=util.json_default)
            output_bytes += len(serialized.encode())
            if output_bytes > MAX_OUTPUT - 4096:
                raise ValueError("output_limit")
            result.append(json.loads(serialized))
            if message[0] == Message.Url:
                media_count += 1
            if media_count >= MAX_RETAINED_CANDIDATES:
                reason = "item_limit"
                break
    except Exception as error:
        if not any(message[0] == Message.Url for message in result):
            raise
        reason = safe_error(error)["error"]
    if reason:
        first_media = next((message for message in result if message[0] == Message.Url), result[0])
        for message in (result[0], first_media, result[-1]):
            message[-1]["upstream_scope"] = {
                "bounded": True, "completion": "unknown", "reason": reason,
                "limit": MAX_RETAINED_CANDIDATES, "returned_count": media_count,
                "retained_count": media_count, "unexpanded_count": None}
    if not any(message[0] == Message.Url for message in result):
        raise ValueError("no_formats")
    return result


def probe_ytdlp_result(payload, *, deadline, budget):
    # Only the single-work result is eligible, never every entry in a collection.
    if not isinstance(payload, dict) or payload.get('_type', 'video') not in ('video', None):
        return
    try:
        from tools.upstream_metadata_probe import MAX_BYTES, MAX_REQUESTS as PROBE_REQUESTS, probe_in_guarded_child
    except ModuleNotFoundError:
        from upstream_metadata_probe import MAX_BYTES, MAX_REQUESTS as PROBE_REQUESTS, probe_in_guarded_child
    if (time.monotonic() >= deadline or budget['requests'] + PROBE_REQUESTS > MAX_REQUESTS
            or max(budget['bytes'], budget['decoded_bytes']) + PROBE_REQUESTS * MAX_BYTES > MAX_BODY):
        return
    candidates = []
    for item in payload.get('formats') or [payload]:
        evidence = item.get('upstream_track_evidence', {})
        codecs = [str(evidence.get(key) or '').lower() for key in ('acodec', 'vcodec')]
        if (item.get('ext') == 'mp4' and item.get('protocol') == 'https'
                and urlsplit(item.get('url') or '').scheme == 'https'
                and 'none' not in codecs and not all(codecs)
                and not item.get('upstream_bmff_track_evidence')):
            candidates.append(item)
    if candidates:
        candidate = max(candidates, key=lambda item: item.get('height') or 0)
        candidate['upstream_bmff_track_evidence'] = probe_in_guarded_child(
            candidate['url'], deadline_monotonic=deadline)


def handle(request):
    global _STAGE
    _STAGE = "identity"
    provider = request["provider_id"]
    identity = package_identity(provider)
    action = request["action"]
    if action == "identity":
        return identity
    if request.get("version") != identity["version"]:
        raise ValueError("version_changed")
    _STAGE = "extractor_inventory"
    classes = extractor_classes(provider)
    allow_mature = request.get("allow_mature", False)
    if not isinstance(allow_mature, bool):
        raise ValueError("invalid_request")
    admitted = [cls for cls in classes if eligible(provider, cls, allow_mature=allow_mature)]
    if action == "coverage":
        routed_count = sum(already_routed(provider, cls) for cls in classes) if not allow_mature else 0
        # total is the raw installed inventory. The exclusion buckets are disjoint:
        # total = eligible + already_routed + other_excluded.
        return {"provider_id": provider, "version": identity["version"],
                "available": True,
                "entries": [entry(provider, cls, identity["version"]) for cls in admitted],
                "counts": {"total": len(classes), "eligible": len(admitted),
                           "excluded": len(classes) - len(admitted),
                           "already_routed": routed_count,
                           "other_excluded": len(classes) - len(admitted) - routed_count}}
    _STAGE = "url_match"
    url = validate_url(request["url"])
    matched = [cls for cls in admitted if suitable(provider, cls, url)]
    if action == "match":
        return [entry(provider, cls, identity["version"]) for cls in matched]
    if action != "parse":
        raise ValueError("invalid_action")
    exact = [cls for cls in matched if entry(provider, cls, identity["version"])[
        "extractor_id"] == request["extractor_id"]]
    if len(exact) != 1:
        raise ValueError("extractor_mismatch")
    timeout = float(request["timeout_seconds"])
    if not math.isfinite(timeout):
        raise ValueError("invalid_request")
    timeout = min(30.0, max(0.1, timeout))
    deadline = time.monotonic() + timeout
    with runtime_budget(timeout) as budget:
        payload = (parse_ytdlp if provider == "yt_dlp_generic" else parse_gallery)(exact[0], url, timeout)
    if provider == "yt_dlp_generic":
        # The probe owns its hard timer; the child network guard remains installed.
        probe_ytdlp_result(payload, deadline=deadline, budget=budget)
    return payload


def main():
    try:
        request = json.loads(sys.stdin.read(16385))
        try:
            import resource
            ceiling = 1536 * 1024 * 1024
            soft, hard = resource.getrlimit(resource.RLIMIT_AS)
            ceiling = min(ceiling, hard) if hard != resource.RLIM_INFINITY else ceiling
            ceiling = min(ceiling, soft) if soft != resource.RLIM_INFINITY else ceiling
            resource.setrlimit(resource.RLIMIT_AS, (ceiling, hard))
        except (ImportError, ValueError, OSError):
            # Optional platform backstop; decoded/output/time limits stay active.
            pass
        # No credential-bearing environment or user configuration reaches imports.
        for key in tuple(os.environ):
            if key not in {"PATH", "HOME", "LANG", "PYTHONNOUSERSITE", "YTDLP_NO_PLUGINS"}:
                os.environ.pop(key, None)
        os.environ["YTDLP_NO_PLUGINS"] = "1"
        install_network_guard(offline=request.get("action") != "parse")
        with contextlib.redirect_stdout(sys.stderr):
            payload = handle(request)
        output = json.dumps({"ok": True, "payload": payload}, ensure_ascii=True)
        if len(output.encode()) > MAX_OUTPUT:
            raise ValueError("output_limit")
    except Exception as error:
        output = json.dumps(safe_error(error))
    sys.stdout.write(output)


if __name__ == "__main__":
    main()
