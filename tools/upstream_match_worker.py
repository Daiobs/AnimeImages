"""One-shot upstream API boundary. Never import this to install parent guards."""

import contextlib
import importlib.metadata
import ipaddress
import json
import os
from pathlib import Path
import re
import socket
import sys
import subprocess
from urllib.parse import urlsplit


PROVIDERS = {"yt_dlp_generic": "yt-dlp", "gallery_dl_generic": "gallery-dl"}
# Names are only an admission heuristic. Runtime result guards are authoritative.
YTDLP_EXCLUDED = re.compile(
    r"generic|unsupported|test|example|search|playlist|collection|channel|"
    r"user|profile|feed|timeline|album|season|series|category|archive|"
    r"favorites|favourites|bookmarks|stories|hashtag|:tab|:list|:set|:page|"
    r"history|recommended|subscriptions|watchlater|notif|truncated|"
    r"Tab$|List$|Set$|Page$|Base$|Embed$|Redirect$|Shortener$", re.I,
)
GALLERY_SINGLE = frozenset({"post", "tweet", "image", "video", "submission", "pin"})
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
    if not isinstance(url, str) or not url or len(url) > MAX_URL:
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


def eligible(provider, cls):
    if already_routed(provider, cls):
        return False
    if provider == "yt_dlp_generic":
        key = cls.ie_key()
        name = str(getattr(cls, "IE_NAME", key))
        return (getattr(cls, "IE_DESC", None) is not False and
                not YTDLP_EXCLUDED.search(key) and
                not YTDLP_EXCLUDED.search(name.lower()) and
                getattr(cls, "_RETURN_TYPE", None) != "playlist" and
                getattr(cls, "_VALID_URL", None) not in (None, False))
    return (cls.__name__ not in GALLERY_EXCLUDED and
            cls.subcategory in GALLERY_SINGLE and
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


def parse_ytdlp(cls, url, timeout):
    global _STAGE
    _STAGE = "ytdlp_import"
    from yt_dlp import YoutubeDL
    # Lazy proxies construct a different class in __new__, bypassing overrides.
    cls = getattr(cls, "real_class", cls)

    class ExactYoutubeDL(YoutubeDL):
        def extract_info(self, *args, **kwargs):
            raise ValueError("delegation_denied")

        def process_ie_result(self, *args, **kwargs):
            raise ValueError("delegation_denied")

        def get_info_extractor(self, *args, **kwargs):
            raise ValueError("delegation_denied")

    class ExactExtractor(cls):
        @classmethod
        def ie_key(inner_cls):
            return cls.ie_key()

        @staticmethod
        def playlist_result(*args, **kwargs):
            raise ValueError("collection_denied")

        @staticmethod
        def url_result(*args, **kwargs):
            raise ValueError("delegation_denied")

    _STAGE = "ytdlp_initialize"
    with ExactYoutubeDL({
        "quiet": True, "no_warnings": True, "skip_download": True,
        "noplaylist": True, "cookiefile": None, "cookiesfrombrowser": None,
        "usenetrc": False, "proxy": "", "nocheckcertificate": False,
        "cachedir": False, "socket_timeout": timeout, "retries": 0,
        "extractor_retries": 0, "enable_file_urls": False,
        "external_downloader": None, "remote_components": set(),
        "js_runtimes": {},
    }, auto_init=False) as ydl:
        _STAGE = "ytdlp_extract"
        payload = ExactExtractor(ydl).extract(url)
        _STAGE = "ytdlp_result"
        if (not isinstance(payload, dict) or payload.get("_type", "video") != "video"
                or "entries" in payload or payload.get("ie_key")):
            raise ValueError("non_item_result")
        if not payload.get("formats") and not payload.get("url"):
            raise ValueError("no_formats")
        # Keep the complete formats dictionary; do not invoke result processing/downloads.
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
    instance = cls(re.match(cls.pattern, url))
    result = []
    output_bytes = 0
    _STAGE = "gallery_extract"
    for message in instance:
        if len(result) >= MAX_ITEMS:
            raise ValueError("item_limit")
        if not message or message[0] not in {Message.Directory, Message.Url}:
            raise ValueError("collection_or_queue_denied")
        if message[0] == Message.Url:
            validate_url(message[1])
        # Snapshot metadata because upstream generators reuse mutable dictionaries.
        serialized = json.dumps(message, default=util.json_default)
        output_bytes += len(serialized.encode())
        if output_bytes > MAX_OUTPUT:
            raise ValueError("output_limit")
        result.append(json.loads(serialized))
    if not any(message[0] == Message.Url for message in result):
        raise ValueError("no_formats")
    return result


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
    admitted = [cls for cls in classes if eligible(provider, cls)]
    if action == "coverage":
        routed_count = sum(already_routed(provider, cls) for cls in classes)
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
    timeout = min(30.0, max(0.1, float(request["timeout_seconds"])))
    return (parse_ytdlp if provider == "yt_dlp_generic" else parse_gallery)(exact[0], url, timeout)


def main():
    try:
        request = json.loads(sys.stdin.read(16385))
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
