"""Explicitly opt-in, anonymous HTTP smoke. Never imports Django or loads .env.

Control bodies and credentials stay in memory. Local image ZIPs verify HTTP
delivery, not the browser ZIP implementation, CORS, Service Worker, or UI saves.
Browser-native and streaming descriptors authorize HTTP resource validation here,
not proof of browser handoff or execution of either browser transport.
An existing round is never resumed, including after an unknown request outcome.

Use --samples-file with a private 0600 JSON file:
{"version": 1, "samples": [{"id": "photo", "platform": "flickr", "kind": "image",
                           "url": "https://public.example/work", "max_bytes": 52428800}]}
Kinds are image (one image), gallery (all ordinary images), and video (the checked
default variant). --list prints only IDs, hosts and types. Confirm the list before
--run. --max-file-bytes caps each file; image groups also retain the 128 MiB total
cap. Reports never contain source/media URLs, titles, filenames or credentials.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import time
from urllib.parse import urljoin, urlsplit
import uuid
import zipfile

from bs4 import BeautifulSoup
from PIL import Image
import requests


SAMPLES = (
    ("xhs_images", "https://xhslink.cn/o/4FAHoHJEYmd"),
    ("xhs_video", "https://xhslink.cn/o/1TuINFpX7Sz"),
    ("douyin", "https://www.douyin.com/video/7680195272341982066"),
    ("bilibili", "https://www.bilibili.com/video/BV1J4JX6PEMV/"),
    ("youtube", "https://www.youtube.com/watch?v=jNQXAC9IVRw"),
)
IMAGE_LIMIT = 50 * 1024 * 1024
ZIP_LIMIT = 128 * 1024 * 1024
VIDEO_LIMIT = 1024 * 1024 * 1024
STATUSES = {"success", "error", "failed", "timeout", "unchanged", "continuation_pending",
            "issued", "connecting", "refreshing", "streaming", "completed", "expired",
            "queued", "running", "pending", "routed"}
STRATEGIES = {"browser_native", "browser_streaming", "browser_blob", "browser_direct", "browser_direct_images",
              "browser_direct_provider_video", "server_attachment", "server_single_attachment",
              "server_merge", "server_zip", "none", "browser_download_required",
              "browser_image_download_required", "blocked_by_download_cost_guard"}
REMOTE_CODES = {"RATE_LIMITED", "PROVIDER_TIMEOUT", "PROVIDER_ACCESS_DENIED", "ASYNC_TASKS_DISABLED",
                "PROVIDER_MANIFEST_REJECTED", "THIRD_PARTY_ROLLOUT_MISS", "RATE_LIMIT_CACHE_ERROR",
                "DOUYIN_CONTINUATION_EXPIRED", "DOUYIN_CONTINUATION_CLIENT_MISMATCH",
                "DOUYIN_CONTINUATION_FAILED", "DOUYIN_CONTINUATION_RESULT_REJECTED",
                "DOWNLOAD_HANDOFF_EXPIRED", "DOWNLOAD_HANDOFF_ALREADY_USED", "DOWNLOAD_HANDOFF_INVALID",
                "DOWNLOAD_HANDOFF_CLIENT_MISMATCH", "VISITOR_VIDEO_DOWNLOAD_DISABLED",
                "THIRD_PARTY_PLATFORM_NOT_ALLOWLISTED", "THIRD_PARTY_PROVIDER_DISABLED",
                "PROVIDER_UNAVAILABLE", "URL_POLICY_DENIED", "SERVICE_EMPTY_RESULT"}
BINDING = ("asset_id", "provider", "provider_resource_id", "provider_cache_key", "provider_manifest_token",
           "source_platform")
PROVIDERS = {"native", "xhs_native", "douyin_native", "bilibili_native", "tiantianjiexi", "kukutool",
             "tiantianjiexi_generic", "kukutool_generic", "ytdlp", "ytdlp_generic", "gallerydl_generic",
             "gallery_dl", "gallery_dl_generic", "yt_dlp_generic", "yt_dlp_merge",
             "parsehub", "parsehub_generic", "parsehub_xhs_images", "videodl", "videodl_repo"}


def now():
    return datetime.now(timezone.utc).isoformat()


def digest(value):
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


class SmokeError(Exception):
    def __init__(self, code, *, insufficient=False):
        self.code = code
        self.insufficient = insufficient
        super().__init__(code)


def require(condition, code):
    if not condition:
        raise SmokeError(code)


def remote_summary(payload):
    # Do not persist arbitrary server text, even when it is named error_code.
    return {"status": payload.get("status") if payload.get("status") in STATUSES else "unknown",
            "message_code": "REMOTE_MESSAGE_PRESENT" if payload.get("message") else "NO_MESSAGE",
            "error_code": payload.get("error_code") if (payload.get("error_code") in REMOTE_CODES or
                re.fullmatch(r"SERVICE_BUSINESS_CODE_-?[0-9]{1,10}", str(payload.get("error_code", ""))))
            else ("REMOTE_ERROR_REDACTED" if payload.get("error_code") else "")}


def hostname(value):
    """Diagnostics retain only a validated hostname, never path/query/userinfo."""
    try:
        host = urlsplit(str(value)).hostname or ""
        return host.lower() if re.fullmatch(r"[A-Za-z0-9._:-]{1,253}", host) else ""
    except ValueError:
        return ""


def load_samples(path):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd) as source:
        info = os.fstat(source.fileno())
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and stat.S_IMODE(info.st_mode) == 0o600, "SAMPLES_NOT_PRIVATE")
        require(info.st_size <= 128 * 1024, "SAMPLES_TOO_LARGE")
        try:
            data = json.load(source)
        except (ValueError, UnicodeError):
            raise SmokeError("INVALID_SAMPLES_FILE") from None
    require(isinstance(data, dict) and set(data) == {"version", "samples"}
            and type(data["version"]) is int and data["version"] == 1, "INVALID_SAMPLES_FILE")
    rows = data["samples"]
    require(isinstance(rows, list) and 0 < len(rows) <= 64, "INVALID_SAMPLES_FILE")
    seen = set()
    for row in rows:
        require(isinstance(row, dict) and {"id", "platform", "kind", "url"} <= set(row)
                and set(row) <= {"id", "platform", "kind", "url", "max_bytes"}, "INVALID_SAMPLE")
        for key in ("id", "platform"):
            require(isinstance(row[key], str) and bool(re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", row[key])), "INVALID_SAMPLE")
        require(row["id"] not in seen, "DUPLICATE_SAMPLE")
        seen.add(row["id"])
        require(isinstance(row["kind"], str) and row["kind"] in {"video", "image", "gallery"}, "INVALID_SAMPLE_KIND")
        public_url(row["url"])
        limit = row.get("max_bytes", VIDEO_LIMIT)
        require(type(limit) is int and 0 < limit <= VIDEO_LIMIT, "INVALID_SAMPLE_LIMIT")
    return rows


def target_origin(value):
    # Only the daily local entry is exempt from the public HTTPS origin rule.
    if value not in {"http://127.0.0.1:8000", "http://127.0.0.1:8000/",
                     "http://localhost:8000", "http://localhost:8000/",
                     "http://[::1]:8000", "http://[::1]:8000/"}:
        public_url(value)
    parsed = urlsplit(value)
    require(parsed.path in ("", "/") and not parsed.query, "BASE_MUST_BE_ORIGIN")
    return value.rstrip("/")


def label(value):
    text = str(value or "")
    if re.fullmatch(r"[0-9]{2,4}[pPkK]\+?", text) or text in {"原画", "超清", "超高清", "高清", "标清"}:
        return text
    return "not_exposed" if not text else "label_redacted"


def private_file(path, mode="wb"):
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    return os.fdopen(fd, mode)


def write_json(path, value):
    with private_file(path, "w") as out:
        json.dump(value, out, ensure_ascii=True, indent=2)
        out.flush()
        os.fsync(out.fileno())


def create_round(output, name):
    require(bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", name)), "INVALID_ROUND")
    root = Path(output).expanduser().absolute()
    old_umask = os.umask(0o077)
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not root.is_symlink() and root.stat().st_uid == os.getuid()
                and root.stat().st_mode & 0o777 == 0o700, "OUTPUT_NOT_PRIVATE")
        target = root / name
        try:
            target.mkdir(mode=0o700)
        except FileExistsError:
            raise SmokeError("ROUND_EXISTS_NO_RESUME") from None
        return target
    finally:
        os.umask(old_umask)


class Journal:
    def __init__(self, directory):
        self.file = private_file(directory / "attempts.jsonl", "w")
        self.counts = {}

    def started(self, sample, phase):
        key = (sample, phase)
        count = self.counts.get(key, 0) + 1
        require(phase != "parse" or count == 1, "PARSE_RETRY_FORBIDDEN")
        self.counts[key] = count
        self.file.write(json.dumps({"sample": sample, "phase": phase, "count": count,
                                    "event": "attempt_started", "time": now()}) + "\n")
        self.file.flush()
        os.fsync(self.file.fileno())


def public_url(value):
    require(isinstance(value, str) and not re.search(r"[\s\\\x00-\x1f\x7f]", value), "INVALID_URL")
    try:
        parsed = urlsplit(value)
        valid = (parsed.scheme == "https" and parsed.hostname and not parsed.username and not parsed.password
                 and not parsed.fragment and parsed.port in (None, 443))
    except ValueError:
        raise SmokeError("INVALID_URL") from None
    require(valid and parsed.hostname not in {"localhost", "localhost.localdomain"}, "INVALID_URL")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError:
        address = None
    require(address is None or address.is_global, "NON_PUBLIC_MEDIA_URL")
    return value


class PublicHTTP:
    def __init__(self, base, journal, *, session_factory=requests.Session):
        self.base = target_origin(base)
        self.site, self.media = session_factory(), session_factory()
        for session in (self.site, self.media):
            session.trust_env = False  # No netrc, ambient credentials, or proxy configuration.
            session.mount("https://", requests.adapters.HTTPAdapter(max_retries=0))
            session.mount("http://", requests.adapters.HTTPAdapter(max_retries=0))
        self.csrf = ""
        self.journal = journal
        self.sample = "bootstrap"
        self.events = []

    def site_path(self, value, pattern=None):
        absolute = urljoin(self.base + "/", value)
        parsed = urlsplit(absolute)
        require((parsed.scheme + "://" + parsed.netloc) == self.base and not parsed.fragment
                and not parsed.query, "CROSS_ORIGIN_CONTROL_URL")
        if pattern:
            require(bool(re.fullmatch(pattern, parsed.path)), "UNEXPECTED_CONTROL_PATH")
        return absolute

    def send(self, method, value, phase, *, data=None, params=None, media=False):
        url = public_url(value) if media else self.site_path(value)
        headers = {"Accept-Encoding": "identity", "User-Agent": "DokiTool-Anonymous-HTTP-Smoke/1.0"}
        session = self.media if media else self.site
        if media:
            session.cookies.clear()
        else:
            for cookie in list(session.cookies):
                if cookie.name != "csrftoken":
                    session.cookies.clear(cookie.domain, cookie.path, cookie.name)
            headers["Accept"] = "application/json" if phase != "home" else "text/html"
            if phase == "parse":
                headers["X-Requested-With"] = "XMLHttpRequest"
            if method == "POST":
                require(bool(self.csrf), "CSRF_MISSING")
                headers.update({"X-CSRFToken": self.csrf, "Origin": self.base, "Referer": self.base + "/"})
        self.journal.started(self.sample, phase)
        try:
            response = session.request(method, url, params=params, json=data, headers=headers,
                                       timeout=(15, 120), allow_redirects=False, stream=True)
        except requests.RequestException:
            self.events.append({"phase": phase, "host": hostname(url), "error_code": "HTTP_OUTCOME_UNKNOWN"})
            raise SmokeError("HTTP_OUTCOME_UNKNOWN") from None
        self.events.append({"phase": phase, "host": hostname(url), "http_status": response.status_code})
        header_code = remote_summary({"error_code": response.headers.get("X-Error-Code", "")})["error_code"]
        if header_code:
            self.events[-1]["error_code"] = header_code
        request_id = response.headers.get("X-Request-ID", "")
        if not media and re.fullmatch(r"[0-9a-f-]{16,64}", request_id):
            self.events[-1]["request_id"] = request_id
        return response

    def control(self, method, path, phase, *, data=None, params=None, html=False):
        response = self.send(method, path, phase, data=data, params=params)
        try:
            chunks, size = [], 0
            for chunk in response.iter_content(65536):
                size += len(chunk)
                require(size <= 4 * 1024 * 1024, "CONTROL_RESPONSE_TOO_LARGE")
                chunks.append(chunk)
            body = b"".join(chunks)
            if html:
                require(response.status_code == 200, "HOME_HTTP_FAILED")
                return body.decode("utf-8")
            try:
                payload = json.loads(body)
            except (ValueError, UnicodeError):
                raise SmokeError("CONTROL_NOT_JSON") from None
            require(isinstance(payload, dict), "CONTROL_NOT_OBJECT")
            summary = remote_summary(payload)
            if not summary["error_code"]:
                summary["error_code"] = self.events[-1].get("error_code", "")
            self.events[-1].update(summary)
            require(200 <= response.status_code < 300, "CONTROL_HTTP_FAILED")
            return payload
        except requests.RequestException:
            raise SmokeError("CONTROL_BODY_UNKNOWN") from None
        finally:
            response.close()

    def close(self):
        self.site.close()
        self.media.close()


def card_item(card):
    item = {key: card.get("data-" + key.replace("_", "-"), "") for key in
            ("type", "url", "cover_url", *BINDING, "download_strategy", "fallback_strategy")}
    if card.get("data-download-filename"):
        item["filename"] = card["data-download-filename"]
    item["size_bytes"] = int(card.get("data-size-bytes", "0"))
    item["logical_type"] = item["type"]
    if item["type"] == "image":
        item["display_ordinal"] = int(card.get("data-display-ordinal", "0"))
        item["filenameStem"] = card.get("data-download-filename-stem", "")
        item["extensionHint"] = card.get("data-download-extension-hint", "")
        item.pop("cover_url", None)
    for key in ("size_is_estimate", "cors_expected", "browser_direct_fetch_safe", "requires_merge", "requires_cookie"):
        item[key] = card.get("data-" + key.replace("_", "-")) == "true"
    return item


def select_result(html, images=False):
    soup = BeautifulSoup(html, "html.parser")
    cards = [c for c in soup.select(".asset-card[data-type]") if not c.find_parent(attrs={"data-ytdlp-picker": True})]
    if images:
        selected = [c for c in cards if c.get("data-type") == "image"]
        require(bool(selected), "NO_IMAGES")
        require(all(c.get("data-download-selectable") != "false" and
                    c.get("data-logical-type", "image") == "image" for c in selected), "UNSUPPORTED_IMAGE_GROUP")
        return [card_item(c) for c in selected], None
    picks = []
    for root in soup.select("[data-ytdlp-picker]"):
        videos = root.select('[data-picker-radio][data-kind="video"]')
        if not videos:
            continue
        checked = [r for r in videos if r.has_attr("checked")]
        require(len(checked) == 1, "AMBIGUOUS_CHECKED_FORMAT")
        picks.append((root, checked[0]))
    videos = [c for c in cards if c.get("data-type") == "video"]
    require(len(picks) + len(videos) == 1, "AMBIGUOUS_VIDEO_DEFAULT")
    return ([card_item(videos[0])], None) if videos else ([], picks[0])


def media_magic(prefix, kind):
    if kind == "zip":
        return prefix.startswith(b"PK\x03\x04")
    if kind == "image":
        return (prefix.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a"))
                or prefix[:4] == b"RIFF" and prefix[8:12] == b"WEBP"
                or prefix[4:8] == b"ftyp" and prefix[8:12] in {b"avif", b"avis", b"heic", b"heix", b"mif1"})
    return (prefix[4:8] == b"ftyp" or prefix.startswith((b"\x1aE\xdf\xa3", b"FLV"))
            or prefix[:4] == b"RIFF" and prefix[8:12] == b"AVI ")


def receive(http, url, path, kind, *, site=False, method="GET", data=None, max_bytes=VIDEO_LIMIT):
    record = {"bytes": 0, "declared_bytes": None, "length_matches": False, "hosts": []}
    response = None
    try:
        for redirect in range(6):
            record["hosts"].append(hostname(url))
            response = http.send(method, url, "media", data=data, media=not site)
            if response.status_code not in {301, 302, 303, 307, 308}:
                break
            require(not site and method == "GET" and redirect < 5, "MEDIA_REDIRECT_REJECTED")
            target = urljoin(url, response.headers.get("Location", ""))
            record["redirect_host"] = hostname(target)
            require(target != url, "MEDIA_REDIRECT_REJECTED")
            response.close()
            url = public_url(target)
        record["http_status"] = response.status_code
        require(response.status_code == 200, "MEDIA_HTTP_FAILED")
        require(response.headers.get("Content-Encoding", "identity").lower() in {"", "identity"}, "ENCODED_MEDIA")
        raw_length = response.headers.get("Content-Length")
        if raw_length is not None:
            require(bool(re.fullmatch(r"[0-9]+", raw_length)), "INVALID_CONTENT_LENGTH")
            record["declared_bytes"] = int(raw_length)
            require(0 < record["declared_bytes"] <= max_bytes, "MEDIA_SIZE_LIMIT")
        chunks = iter(response.iter_content(65536))
        first = next((chunk for chunk in chunks if chunk), b"")
        record["bytes"] = len(first)
        require(record["bytes"] <= max_bytes, "MEDIA_SIZE_LIMIT")
        require(media_magic(first, kind), "WRONG_MEDIA_CONTAINER")
        sha = hashlib.sha256()
        with private_file(path) as output:
            sha.update(first)
            output.write(first)
            for chunk in chunks:
                record["bytes"] += len(chunk)
                require(record["bytes"] <= max_bytes, "MEDIA_SIZE_LIMIT")
                sha.update(chunk)
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        record["sha256"] = sha.hexdigest()
        record["length_matches"] = record["bytes"] == record["declared_bytes"]
        if raw_length is None:
            record["evidence_code"] = "CONTENT_LENGTH_MISSING"
        else:
            require(record["length_matches"], "CONTENT_LENGTH_MISMATCH")
        return record
    except requests.RequestException:
        record["error_code"] = "MEDIA_BODY_UNKNOWN"
        return record
    except SmokeError as error:
        record["error_code"] = error.code
        return record
    finally:
        if response is not None:
            response.close()


def inspect_image(path):
    try:
        with Image.open(path) as image:
            info = {"format": image.format, "width": image.width, "height": image.height}
            require(image.format in {"JPEG", "PNG", "GIF", "WEBP", "AVIF", "HEIF"}, "WRONG_IMAGE_FORMAT")
            image.verify()
        with Image.open(path) as image:
            image.load()
        return info
    except SmokeError:
        raise
    except Exception:
        raise SmokeError("INVALID_IMAGE") from None


def inspect_video(path, ffprobe):
    try:
        completed = subprocess.run([ffprobe, "-v", "error", "-protocol_whitelist", "file,pipe",
                                    "-show_entries", "format=format_name,duration:stream=codec_type,codec_name,width,height",
                                    "-of", "json", str(path)], capture_output=True, timeout=60, check=False)
        require(completed.returncode == 0, "FFPROBE_FAILED")
        payload = json.loads(completed.stdout)
        formats = set(payload.get("format", {}).get("format_name", "").split(","))
        require(bool(formats & {"mov", "mp4", "matroska", "webm", "flv", "avi"}), "WRONG_VIDEO_FORMAT")
        streams = payload.get("streams", [])
        videos = [s for s in streams if s.get("codec_type") == "video"]
        audios = [s for s in streams if s.get("codec_type") == "audio"]
        require(bool(videos) and all(int(s.get("width", 0)) > 0 and int(s.get("height", 0)) > 0 for s in videos), "VIDEO_TRACK_MISSING")
        codecs = {"h264", "hevc", "av1", "vp8", "vp9", "mpeg4", "aac", "mp3", "opus", "vorbis", "flac", "pcm_s16le"}
        return {"container": sorted(formats & {"mov", "mp4", "matroska", "webm", "flv", "avi"}),
                "video_tracks": len(videos), "audio_tracks": len(audios),
                "width": int(videos[0]["width"]), "height": int(videos[0]["height"]),
                "codecs": [s.get("codec_name") if s.get("codec_name") in codecs else "other" for s in streams]}
    except SmokeError:
        raise
    except Exception:
        raise SmokeError("FFPROBE_UNAVAILABLE_OR_INVALID") from None


class SmokeRunner:
    def __init__(self, http, directory, *, ffprobe="ffprobe", sleep=time.sleep, clock=time.monotonic):
        self.http, self.directory, self.ffprobe = http, directory, ffprobe
        self.sleep, self.clock = sleep, clock
        self.home = {}

    def bootstrap(self):
        soup = BeautifulSoup(self.http.control("GET", "/", "home", html=True), "html.parser")
        csrf = soup.select_one('input[name="csrfmiddlewaretoken"]')
        self.http.csrf = csrf.get("value", "") if csrf else self.http.site.cookies.get("csrftoken", "")
        require(bool(self.http.csrf), "CSRF_MISSING")
        page = soup.select_one(".xhs-download-page")
        youtube = soup.select_one('input[name="youtube_merge"]')
        ultra = soup.select_one('input[name="ultra_hd"]')
        require(page is not None and youtube is not None and ultra is not None, "HOME_FIELDS_MISSING")
        require(youtube.get("value") in {"0", "1"} and ultra.get("value") in {"0", "1"}, "HOME_DEFAULT_INVALID")
        self.home = {"youtube_merge": youtube["value"], "ultra_hd": ultra["value"],
                     "async_tasks": page.get("data-download-async-enabled") == "true",
                     "ultra_available": page.get("data-ultra-hd-available") == "true",
                     "youtube_available": page.get("data-youtube-merge-available") == "true"}

    def parse(self, name, source, report):
        report["stage"] = "parse"
        params = {"q": source, "_partial": "1", "ultra_hd": self.home["ultra_hd"],
                  "youtube_merge": self.home["youtube_merge"]}
        report["parse_parameters"] = {k: v for k, v in params.items() if k != "q"}
        payload = self.http.control("GET", "/results/", "parse", params=params)
        report["parse"] = remote_summary(payload)
        if payload.get("status") == "continuation_pending":
            initial, token = payload, payload.get("continuation_token")
            require(isinstance(token, str) and bool(token), "CONTINUATION_TOKEN_MISSING")
            ttl = min(17, max(0, float(payload.get("expires_in_seconds", 15))))
            deadline = self.clock() + ttl
            for _ in range(14):
                delay = min(3, max(.5, float(payload.get("poll_after_ms", 1500)) / 1000))
                if self.clock() + delay >= deadline:
                    payload = {"status": "timeout", "error_code": "DOUYIN_CONTINUATION_EXPIRED"}
                    break
                self.sleep(delay)
                payload = self.http.control("POST", "/media/douyin-continuation/", "continuation",
                                            data={"continuation_token": token})
                if payload.get("status") != "continuation_pending":
                    break
            report["continuation"] = remote_summary(payload)
            if payload.get("status") != "success":
                require(initial.get("has_results") is True, "CONTINUATION_NO_RESULT")
                report["degraded"] = True
                payload = initial
        require(payload.get("status") in {"success", "continuation_pending"} and payload.get("has_results") is True
                and isinstance(payload.get("html"), str), "PARSE_NO_USABLE_RESULT")
        return payload

    @staticmethod
    def resource(item, report):
        host = hostname(item.get("url", ""))
        if host and host not in report.setdefault("media_hosts", []):
            report["media_hosts"].append(host)
        provider = item.get("provider")
        if provider in PROVIDERS:
            report["provider"] = provider
        strategy = item.get("download_strategy")
        if strategy in STRATEGIES and strategy not in {"", "none"}:
            report["download_route"] = strategy

    def descriptor(self, pick, report):
        report["stage"] = "descriptor"
        root, radio = pick
        action = "download" if radio.get("data-can-download") == "true" else "legacy"
        require(action == "download" or radio.get("data-can-legacy") == "true", "DEFAULT_NOT_DOWNLOADABLE")
        path = root.get("data-selection-url", "")
        self.http.site_path(path, r"/media/(?:service|ytdlp)-selected-format/")
        result = self.http.control("POST", path, "descriptor", data={"candidate_id": radio.get("value"),
                                   "resource_token": radio.get("data-token"), "action": action})
        require(result.get("status") == "success" and isinstance(result.get("item"), dict), "DESCRIPTOR_FAILED")
        item = result["item"]
        self.resource(item, report)
        require(item.get("candidate_id") == radio.get("value") and item.get("selected_format_id") == radio.get("data-format-id")
                and item.get("selection_schema") == root.get("data-selection-schema")
                and item.get("provider") == root.get("data-provider") and item.get("type") == "video", "SELECTION_BINDING_MISMATCH")
        if action == "legacy":
            require(item.get("selection_delivery") == "legacy_manifest"
                    and all(isinstance(item.get(k), str) and item[k] for k in BINDING)
                    and item["asset_id"] == radio.get("data-legacy-asset-id"), "LEGACY_BINDING_MISMATCH")
        else:
            require(item.get("selection_delivery", "direct") == "direct" and item.get("browser_upstream_bound") is True
                    and item.get("browser_direct_fetch_safe") is True and item.get("requires_merge") is False
                    and item.get("requires_cookie") is not True, "DIRECT_BINDING_MISMATCH")
            require(float(item.get("selection_expires_at", 0)) > time.time() * 1000, "SELECTION_EXPIRED")
            if item.get("provider") in {"tiantianjiexi", "kukutool"}:
                require(all(item.get(k) for k in BINDING) and item["asset_id"] == radio.get("data-legacy-asset-id"), "DEDICATED_BINDING_MISMATCH")
        report["selection"] = {"label": label(radio.get("data-label")), "action": action,
                               "resource_sha256": digest(item["url"])}
        self.policy(item, report)
        return item, action

    @staticmethod
    def policy(payload, report):
        policy = {}
        for key in ("route_type", "download_strategy", "fallback_strategy"):
            if key not in payload:
                continue
            allowed = STRATEGIES | {"direct", "proxy", "zip", "blocked", "server_stream"}
            require(payload[key] in allowed or payload[key] == "", "UNRECOGNIZED_ROUTE_POLICY")
            policy[key] = payload[key]
        report.setdefault("policies", []).append(policy)
        if "route_type" in policy:
            report["route_type"] = policy["route_type"]
        if "download_strategy" in policy:
            report["download_route"] = policy["download_strategy"]
        require(payload.get("route_type") != "blocked"
                and payload.get("download_strategy") != "blocked_by_download_cost_guard", "DOWNLOAD_POLICY_BLOCKED")

    def route(self, items, base, report):
        report["stage"] = "route"
        for item in items:
            self.resource(item, report)
        body = {"media_items": items, "base_filename": base, "download_mode": "browser", "intent": "download",
                "save_format": "original", "quality": 95, "video_save_mode": "video"}
        route = self.http.control("POST", "/media/download-images/", "route", data={**body, "route_only": True})
        require(route.get("status") == "routed", "ROUTE_FAILED")
        self.policy(route, report)
        report["route_type"] = route.get("route_type", "")
        report["download_route"] = route.get("download_strategy", "")
        for current in route.get("items", []):
            self.resource(current, report)
        if route.get("route_type") != "direct":
            report["download_route"] = route.get("download_strategy", "")
        return body, route

    @staticmethod
    def validate_browser_video(item):
        require(item.get("browser_direct_fetch_safe") is True
                and not item.get("requires_merge") and not item.get("requires_cookie"), "DIRECT_ROUTE_UNAUTHORIZED")
        now_ms = time.time() * 1000

        def live_deadline(value):
            return (type(value) in (int, float) and now_ms < value <= 2 ** 53 - 1
                    and value == int(value))

        for key in ("selection_expires_at", "expiresAt"):
            if key in item:
                require(live_deadline(item[key]), "SELECTION_EXPIRED")
        if item.get("download_strategy") == "browser_native":
            require(live_deadline(item.get("native_download_expires_at")), "NATIVE_DESCRIPTOR_EXPIRED")

    def photos(self, items, base, directory, report, *, max_bytes=VIDEO_LIMIT):
        require(bool(items), "NO_IMAGES")
        for item in items:
            self.resource(item, report)
        if any(str(item.get("asset_id", "")).startswith("upstream:") for item in items):
            _, route = self.route(items, base, report)
            bound = route.get("items", [])
            require(route.get("route_type") == "direct" and len(bound) == len(items), "IMAGE_ROUTE_REJECTED")
            for original, current in zip(items, bound):
                require(all(current.get(k) == original.get(k) for k in ("url", "type"))
                        and all(current.get(k) == original.get(k)
                                or (current.get(k) in (None, "") and original.get(k) in (None, ""))
                                for k in ("asset_id", "provider"))
                        and current.get("browser_upstream_bound") is True, "IMAGE_BINDING_MISMATCH")
                require(current.get("browser_direct_fetch_safe") is True, "IMAGE_CONTEXT_REQUIRED")
            items = bound
        # Match the page's ordinary photo branch: no server route or archive request.
        for item in items:
            self.policy(item, report)
            require(not item.get("requires_merge") and not item.get("requires_cookie"), "IMAGE_CONTEXT_REQUIRED")
        report.update(transport="http_direct_image" if len(items) == 1 else "http_images_local_store_zip",
                      expected_count=len(items), images=[])
        entries, total = [], 0
        for index, item in enumerate(items, 1):
            row = {"ordinal": index, "resource_sha256": digest(item["url"])}
            report["images"].append(row)
            path = directory / f"image_{index:02d}.media"
            try:
                report["stage"] = "media"
                require(not item.get("requires_merge") and not item.get("requires_cookie"), "IMAGE_CONTEXT_REQUIRED")
                require(total < ZIP_LIMIT, "IMAGE_GROUP_SIZE_LIMIT")
                row.update(receive(self.http, item["url"], path, "image", max_bytes=min(max_bytes, IMAGE_LIMIT, ZIP_LIMIT - total)))
                total += row["bytes"]
                if row.get("error_code"):
                    row["failure_stage"] = "media"
                    continue
                report["stage"] = "container"
                row.update(inspect_image(path))
                extension = {"JPEG": "jpg", "PNG": "png", "GIF": "gif", "WEBP": "webp", "AVIF": "avif", "HEIF": "heic"}[row["format"]]
                entries.append((path, f"image_{index:02d}.{extension}"))
            except SmokeError as error:
                row["error_code"] = error.code
                row["failure_stage"] = report["stage"]
        require(bool(entries), "NO_VALID_IMAGES")
        report.update(saved_count=len(entries), failed_count=len(items) - len(entries))
        if len(items) == 1:
            if not report["images"][0]["length_matches"]:
                raise SmokeError("CONTENT_LENGTH_MISSING", insufficient=True)
            return
        archive = directory / "images.zip"
        with private_file(archive) as output, zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as zipped:
            for path, name in entries:
                zipped.write(path, name)
            failures = [r for r in report["images"] if r.get("error_code")]
            if failures:
                zipped.writestr("failed.txt", "\n".join(f"{r['ordinal']}: {r['error_code']}" for r in failures))
        with zipfile.ZipFile(archive) as zipped:
            require(zipped.testzip() is None, "ZIP_CRC_FAILED")
            require(len([i for i in zipped.infolist() if i.filename != "failed.txt"]) == len(entries), "ZIP_COUNT_MISMATCH")
        report["archive"] = {"bytes": archive.stat().st_size, "sha256": file_digest(archive), "crc_verified": True}
        failed = next((r for r in report["images"] if r.get("error_code")), None)
        if failed:
            report["stage"] = failed["failure_stage"]
        require(len(entries) == len(items), "IMAGE_GROUP_INCOMPLETE")
        if any(not r["length_matches"] for r in report["images"]):
            raise SmokeError("CONTENT_LENGTH_MISSING", insufficient=True)

    def video(self, item, action, base, directory, report, *, max_bytes=VIDEO_LIMIT):
        self.resource(item, report)
        body = None
        if action == "download":
            self.validate_browser_video(item)
            kind, url, site, method, data = "video", item["url"], False, "GET", None
            report["transport"] = "http_direct_media"
        else:
            body, route = self.route([item], base, report)
            route_type, strategy = route.get("route_type"), route.get("download_strategy")
            if route_type == "direct":
                bound = route.get("items", [])
                require(len(bound) == 1
                        and all(bound[0].get(k) == item.get(k) for k in ("url", "type"))
                        and all(bound[0].get(k) == item.get(k)
                                or (bound[0].get(k) in (None, "") and item.get(k) in (None, ""))
                                for k in ("asset_id", "provider")), "ROUTE_BINDING_MISMATCH")
                self.policy(bound[0], report)
                self.validate_browser_video(bound[0])
                kind, url, site, method, data = "video", bound[0]["url"], False, "GET", None
                report["transport"] = "http_direct_media"
            elif route_type == "proxy" and strategy == "server_single_attachment":
                report["stage"] = "prepare"
                report["handoff_item_string_lengths"] = {
                    key: len(item[key]) for key in ("type", "url", "filename", *BINDING)
                    if isinstance(item.get(key), str)}
                prepared = self.http.control("POST", "/media/download-prepare/", "prepare",
                                             data={**body, "download_mode": "server_single"})
                require(prepared.get("ok") is True and prepared.get("status") == "issued", "PREPARE_FAILED")
                url = self.http.site_path(prepared.get("download_url", ""), r"/media/download-stream/[^/?#]+/")
                status_url = self.http.site_path(prepared.get("status_url", ""), r"/media/download-status/[^/?#]+/")
                require(float(prepared.get("expires_in_seconds", 0)) > 0, "HANDOFF_EXPIRED")
                kind, site, method, data = "video", True, "GET", None
                report["transport"] = "http_server_attachment"
            elif route_type == "zip":
                kind, site = "zip", True
                report["transport"] = "http_server_zip"
                if self.home["async_tasks"]:
                    report["stage"] = "task_create"
                    created = self.http.control("POST", "/media/download-tasks/", "task_create", data=body)
                    require(created.get("status") == "success", "TASK_CREATE_FAILED")
                    status_url = self.http.site_path(created.get("status_url", ""), r"/media/download-tasks/[^/?#]+/")
                    completed = self.poll_status(status_url, report, task=True)
                    require(completed.get("saved_count") == 1 and completed.get("failed_count") == 0, "TASK_PARTIAL_RESULT")
                    url = self.http.site_path(completed.get("file_url", created.get("file_url", "")), r"/media/download-tasks/[^/?#]+/file/")
                    method, data = "GET", None
                else:
                    url, method, data = "/media/download-images/", "POST", body
            else:
                raise SmokeError("UNSUPPORTED_DOWNLOAD_ROUTE")
        path = directory / ("video.zip" if kind == "zip" else "video.media")
        report["stage"] = "media"
        saved = receive(self.http, url, path, kind, site=site, method=method, data=data, max_bytes=max_bytes)
        report["download"] = saved
        require(not saved.get("error_code"), saved.get("error_code", "MEDIA_FAILED"))
        if report["transport"] == "http_server_attachment":
            self.poll_status(status_url, report)
        report["stage"] = "container"
        if kind == "zip":
            with zipfile.ZipFile(path) as zipped:
                members = zipped.infolist()
                require(len(members) == 1 and not members[0].is_dir() and not members[0].flag_bits & 1
                        and 0 < members[0].file_size <= max_bytes, "VIDEO_ZIP_NOT_SINGLE_COMPLETE_FILE")
                extracted = directory / "video.media"
                with zipped.open(members[0]) as source, private_file(extracted) as output:
                    first = source.read(65536)
                    require(media_magic(first, "video"), "WRONG_MEDIA_CONTAINER")
                    output.write(first)
                    while chunk := source.read(65536):
                        output.write(chunk)
                require(extracted.stat().st_size == members[0].file_size, "ZIP_SIZE_MISMATCH")
                saved["zip_crc_verified"] = True  # ZipExtFile checks CRC at EOF.
                path = extracted
        saved.update(inspect_video(path, self.ffprobe))
        saved["media_sha256"] = file_digest(path)
        if not saved["length_matches"]:
            raise SmokeError("CONTENT_LENGTH_MISSING", insufficient=True)

    def poll_status(self, path, report, task=False):
        report["stage"] = "task_status" if task else "attachment_status"
        for _ in range(240 if task else 20):
            result = self.http.control("GET", path, "task_status" if task else "attachment_status")
            report["delivery_status"] = remote_summary(result)
            if result.get("status") == "completed":
                return result
            require(result.get("status") in {"issued", "connecting", "refreshing", "streaming", "queued", "running", "pending"}, "DELIVERY_STATUS_FAILED")
            self.sleep(1 if task else .5)
        raise SmokeError("DELIVERY_STATUS_TIMEOUT")

    def run_sample(self, name, source, *, kind=None, platform="", max_bytes=VIDEO_LIMIT):
        report = {"sample": name, "started_at": now(), "status": "failed", "scope": "anonymous_http_smoke",
                  "platform": platform, "kind": kind or ("gallery" if name == "xhs_images" else "video"),
                  "source_host": hostname(source), "source_sha256": digest(source),
                  "provider": "not_exposed", "media_hosts": [],
                  "download_route": "not_reached", "max_file_bytes": max_bytes,
                  "browser_native_verified": False, "browser_sw_verified": False}
        self.http.sample, self.http.events = name, []
        directory = self.directory / name
        directory.mkdir(mode=0o700)
        try:
            result = self.parse(name, source, report)
            if result.get("selected_provider") in PROVIDERS:
                report["provider"] = result["selected_provider"]
            report["stage"] = "selection"
            images = report["kind"] in {"image", "gallery"}
            items, pick = select_result(result["html"], images=images)
            if images:
                require(report["kind"] != "image" or len(items) == 1, "EXPECTED_SINGLE_IMAGE")
                require(report["kind"] != "gallery" or len(items) >= 2, "EXPECTED_IMAGE_GALLERY")
                self.photos(items, result.get("base_filename", "images"), directory, report, max_bytes=max_bytes)
            else:
                if pick:
                    item, action = self.descriptor(pick, report)
                else:
                    item, action = items[0], "card"
                    report["selection"] = {"label": "not_exposed", "action": action, "resource_sha256": digest(item["url"])}
                self.video(item, action, result.get("base_filename", "video"), directory, report, max_bytes=max_bytes)
                report.update(expected_count=1, saved_count=1, failed_count=0)
            report["status"] = "degraded" if report.get("degraded") else "passed"
        except SmokeError as error:
            report.update(status="insufficient_evidence" if error.insufficient else "failed", error_code=error.code)
        except Exception:
            report.update(status="failed", error_code="UNEXPECTED_LOCAL_FAILURE")
        stage = report.pop("stage", "local")
        report["failure_stage"] = stage if report["status"] not in {"passed", "degraded"} else ""
        report["remote_error_code"] = next((event["error_code"] for event in reversed(self.http.events)
                                            if event.get("error_code")), "")
        report["bytes"] = report.get("download", {}).get("bytes", 0) + sum(row.get("bytes", 0) for row in report.get("images", []))
        report["containers"] = report.get("download", {}).get("container", []) or sorted({row["format"] for row in report.get("images", []) if row.get("format")})
        report.update(ended_at=now(), requests=self.http.events,
                      parse_attempts=self.http.journal.counts.get((name, "parse"), 0),
                      media_http_attempts=self.http.journal.counts.get((name, "media"), 0))
        write_json(directory / "summary.json", report)
        return report


def file_digest(path):
    sha = hashlib.sha256()
    with open(path, "rb") as source:
        while chunk := source.read(65536):
            sha.update(chunk)
    return sha.hexdigest()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--run", action="store_true", help="Send one anonymous round; never retry or resume")
    mode.add_argument("--dry-run", "--dryrun", action="store_true", help="List the plan without HTTP or output files")
    mode.add_argument("--list", action="store_true", help="List sample metadata without HTTP or source URLs")
    parser.add_argument("--samples-file", help="Private 0600 JSON sample list; never copied into reports")
    parser.add_argument("--max-file-bytes", type=int, default=VIDEO_LIMIT,
                        help="Maximum bytes per saved file (also applies to extracted video)")
    parser.add_argument("--youtube-not-public", action="store_true",
                        help="Operator-confirmed exclusion: YouTube is not publicly enabled; require only the other four samples")
    parser.add_argument("--base-url", help="Explicit public HTTPS origin or local http://127.0.0.1:8000")
    parser.add_argument("--output", default=str(Path.home() / ".local/state/dokitool/production-smoke"))
    parser.add_argument("--round", default=datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ_") + uuid.uuid4().hex[:8])
    parser.add_argument("--ffprobe", default=shutil.which("ffprobe") or "ffprobe")
    args = parser.parse_args(argv)
    try:
        require(0 < args.max_file_bytes <= VIDEO_LIMIT, "INVALID_SAMPLE_LIMIT")
        require(not (args.samples_file and args.youtube_not_public), "FILE_SAMPLES_CANNOT_BE_AUTO_EXCLUDED")
        samples = load_samples(args.samples_file) if args.samples_file else [
            {"id": name, "platform": "xiaohongshu" if name.startswith("xhs_") else name,
             "kind": "gallery" if name == "xhs_images" else "video", "url": source}
            for name, source in SAMPLES]
    except (SmokeError, OSError) as error:
        print(error.code if isinstance(error, SmokeError) else "SAMPLES_FILE_UNREADABLE")
        return 2
    required_samples = [row for row in samples if not (args.youtube_not_public and row["id"] == "youtube")]
    selection = {
        "youtube_not_public": args.youtube_not_public,
        "required_samples": [row["id"] for row in required_samples],
        "excluded": [{"sample": "youtube", "status": "excluded",
                      "reason_code": "OPERATOR_CONFIRMED_NOT_PUBLIC",
                      "reason": "YouTube is not publicly enabled; explicitly excluded by --youtube-not-public.",
                      "parse_attempts": 0, "media_http_attempts": 0}] if args.youtube_not_public else [],
    }
    if not args.run:
        print(json.dumps({"mode": "dry_run", "network_requests": 0, "samples": [
            {"id": row["id"], "platform": row["platform"], "kind": row["kind"],
             "source_host": hostname(row["url"]), "max_bytes": min(row.get("max_bytes", VIDEO_LIMIT), args.max_file_bytes)}
            for row in samples],
                          **selection,
                          "youtube_merge": "read_anonymous_home_default", "ultra_hd": "read_anonymous_home_default",
                          "images": "all_images_http_local_store_zip", "scope": "http_only_not_browser"}, indent=2))
        return 0
    journal = http = None
    try:
        require(bool(args.base_url), "BASE_URL_REQUIRED")
        target_origin(args.base_url)
        if any(row["kind"] == "video" for row in required_samples):
            require(Path(args.ffprobe).is_file() and os.access(args.ffprobe, os.X_OK), "FFPROBE_UNAVAILABLE")
        directory = create_round(args.output, args.round)
        journal = Journal(directory)
        http = PublicHTTP(args.base_url, journal)
        report = {"scope": "anonymous_http_smoke", "started_at": now(),
                  "tool_sha256": file_digest(Path(__file__)), "target_host": hostname(args.base_url),
                  "sample_list_sha256": digest(json.dumps(required_samples, sort_keys=True)),
                  **selection,
                  "browser_native_verified": False, "browser_sw_verified": False, "ui_auto_replace_verified": False,
                  "browser_zip_verified": False, "samples": []}
        runner = SmokeRunner(http, directory, ffprobe=args.ffprobe)
        try:
            runner.bootstrap()
        except SmokeError as error:
            report["bootstrap_error_code"] = error.code
        else:
            report["home_defaults"] = runner.home
            for row in required_samples:
                result = runner.run_sample(row["id"], row["url"], platform=row["platform"], kind=row["kind"],
                                           max_bytes=min(row.get("max_bytes", VIDEO_LIMIT), args.max_file_bytes))
                report["samples"].append(result)
                print(row["id"] + ": " + result["status"])
        report["ended_at"] = now()
        report["passed"] = len(report["samples"]) == len(required_samples) and all(s["status"] == "passed" for s in report["samples"])
        write_json(directory / "report.json", report)
        print("Round report written; HTTP evidence only.")
        return 0 if report["passed"] else 1
    except SmokeError as error:
        print(error.code)
        return 2
    except KeyboardInterrupt:
        print("INTERRUPTED_NO_RESUME_CHECK_ATTEMPT_JOURNAL")
        return 130
    except Exception:
        print("LOCAL_FAILURE_NO_RESUME_CHECK_ATTEMPT_JOURNAL")
        return 2
    finally:
        if http:
            http.close()
        if journal:
            journal.file.close()


if __name__ == "__main__":
    raise SystemExit(main())
