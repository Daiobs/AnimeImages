"""Offline capability matching and opt-in, isolated exact-extractor parsing."""

from copy import deepcopy
import ast
from hashlib import sha256
import json
from pathlib import Path
import shlex
import shutil
import threading
import time
import subprocess
from collections import OrderedDict

from django.conf import settings

from .platforms.external_tools import ExternalCommandOutputTooLarge, run_external_command
from tools.upstream_match_worker import validate_url


WORKER = Path(__file__).resolve().parents[2] / "tools/upstream_match_worker.py"
PROVIDERS = {
    "yt_dlp_generic": ("YTDLP_BINARY", "yt-dlp", "yt_dlp"),
    "gallery_dl_generic": ("GALLERYDL_BINARY", "gallery-dl", "gallery_dl"),
}
_CACHE = OrderedDict()
_IDENTITIES = OrderedDict()
_LOCK = threading.Lock()
MAX_CACHE = 256
IDENTITY_TTL_SECONDS = 3.0


class UpstreamMatchingError(RuntimeError):
    def __init__(self, code, *, stage="parent", error_class=""):
        super().__init__(code)
        self.code = code
        self.stage = stage
        self.error_class = error_class


def _executable(value):
    path = shutil.which(value)
    if not path:
        raise UpstreamMatchingError("executor_unavailable")
    # Do not resolve Python symlinks: their location selects the virtualenv.
    return str(Path(path).absolute())


def _executor(provider):
    setting, default, module = PROVIDERS[provider]
    args = shlex.split(str(getattr(settings, setting, "") or default))
    executable = _executable(args[0])
    if len(args) == 3 and args[1:] == ["-m", module]:
        python = executable
    elif len(args) == 1:
        # Only accept a standard Python console entry point, never infer a sibling Python.
        with open(executable, "rb") as source_file:
            source = source_file.read(8193).decode("utf-8")
        if len(source) > 8192:
            raise UpstreamMatchingError("unsupported_executor")
        lines = source.splitlines()
        shebang = shlex.split(lines[0][2:]) if lines and lines[0].startswith("#!") else []
        template = ("import sys\nfrom " + module + " import main\n"
                    "if __name__ == '__main__':\n"
                    "    sys.argv[0] = sys.argv[0].removesuffix('.exe')\n"
                    "    sys.exit(main())\n")
        if (len(shebang) != 1 or not Path(shebang[0]).is_absolute() or
                ast.dump(ast.parse(source)) != ast.dump(ast.parse(template))):
            raise UpstreamMatchingError("unsupported_executor")
        python = _executable(shebang[0])
    else:
        raise UpstreamMatchingError("unsupported_executor")
    signature = []
    for path in (executable, python, str(WORKER), str(WORKER.with_name('upstream_metadata_probe.py'))):
        stat = Path(path).stat()
        signature.append((path, stat.st_size, stat.st_mtime_ns, stat.st_ino))
    return [python, "-I", "-B", str(WORKER)], signature


def _call(command, request, timeout=5.0):
    try:
        completed = run_external_command(
            command, input_text=json.dumps(request), timeout_seconds=timeout,
            max_stdout_bytes=4 * 1024 * 1024, max_stderr_bytes=64 * 1024,
        )
        response = json.loads(completed.stdout)
        if completed.returncode:
            raise UpstreamMatchingError("worker_exit", stage="process")
        if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
            raise UpstreamMatchingError("invalid_protocol", stage="protocol")
        if response.get("ok") is not True:
            codes = {"network_denied", "execution_guard", "delegation_denied", "playlist_denied",
                     "item_limit", "output_limit", "version_changed", "extractor_mismatch",
                     "invalid_url", "no_formats", "network_timeout", "unavailable", "tls_error",
                     "auth_required", "access_denied", "upstream_unavailable", "worker_error",
                     "runtime_import_error"}
            code = response.get("error")
            stage = response.get("stage", "worker")
            error_class = response.get("error_class", "")
            if not isinstance(code, str) or code not in codes or not isinstance(stage, str) or not stage.replace("_", "").isalnum():
                raise UpstreamMatchingError("invalid_protocol", stage="protocol")
            if not isinstance(error_class, str) or not error_class.replace("_", "").isalnum():
                raise UpstreamMatchingError("invalid_protocol", stage="protocol")
            raise UpstreamMatchingError(code, stage=stage[:80], error_class=error_class[:80])
        if "payload" not in response:
            raise UpstreamMatchingError("invalid_protocol", stage="protocol")
        return response["payload"]
    except UpstreamMatchingError:
        raise
    except json.JSONDecodeError:
        raise UpstreamMatchingError("invalid_protocol", stage="protocol") from None
    except subprocess.TimeoutExpired:
        raise UpstreamMatchingError("execution_timeout", stage="process") from None
    except ExternalCommandOutputTooLarge:
        raise UpstreamMatchingError("output_limit", stage="process") from None
    except FileNotFoundError:
        raise UpstreamMatchingError("executor_unavailable", stage="process") from None
    except Exception:
        # Never expose worker stderr, source URLs, filesystem paths, or credentials.
        raise UpstreamMatchingError("upstream_worker_failed") from None


def _context(provider):
    command, signature = _executor(provider)
    key = (provider, json.dumps(signature))
    now = time.monotonic()
    with _LOCK:
        previous = _IDENTITIES.get(key)
    if previous is not None:
        expiry, watched, identity, fingerprint = previous
        if now < expiry and _watch_signature(identity.get("watch_paths", [])) == watched:
            return command, identity["version"], fingerprint
    identity = _call(command, {"action": "identity", "provider_id": provider})
    if not isinstance(identity, dict) or not isinstance(identity.get("version"), str) or not identity["version"]:
        raise UpstreamMatchingError("invalid_identity")
    watched = _watch_signature(identity.get("watch_paths", []))
    fingerprint = sha256(json.dumps([signature, identity, watched], sort_keys=True).encode()).hexdigest()
    with _LOCK:
        _IDENTITIES[key] = (now + IDENTITY_TTL_SECONDS, watched, identity, fingerprint)
        while len(_IDENTITIES) > 8:
            _IDENTITIES.popitem(last=False)
    return command, identity["version"], fingerprint


def _watch_signature(paths):
    result = []
    for path in paths:
        try:
            stat = Path(path).stat()
            result.append((str(path), stat.st_size, stat.st_mtime_ns, stat.st_ino))
        except OSError:
            result.append((str(path), None))
    return result


def _cached(provider, action, url=None):
    command, version, fingerprint = _context(provider)
    key = (provider, action, fingerprint, sha256((url or "").encode()).hexdigest())
    with _LOCK:
        if key in _CACHE:
            _CACHE.move_to_end(key)
            return deepcopy(_CACHE[key])
    request = {"action": action, "provider_id": provider, "version": version}
    if url is not None:
        request["url"] = url
    payload = _call(command, request)
    with _LOCK:
        _CACHE[key] = deepcopy(payload)
        while len(_CACHE) > MAX_CACHE:
            _CACHE.popitem(last=False)
    return payload


def match_upstream_url(url):
    """Return specific single-item matches, never domain-only coverage claims."""
    try:
        validate_url(url)
    except (ValueError, TypeError):
        return ()
    matches = []
    for provider in PROVIDERS:
        try:
            matches.extend(_cached(provider, "match", url))
        except (UpstreamMatchingError, OSError, ValueError, SyntaxError):
            continue
    from .upstream_parsehub import match_upstream_url as match_parsehub
    matches.extend(match_parsehub(url))
    return tuple(matches)


def coverage_snapshot():
    """Return dynamic inventories, excluding established-route families.

    Counts.total is raw upstream inventory, not newly supported platforms.
    Counts.eligible excludes already_routed and other_excluded extractors.
    Availability and URL matching do not imply successful extraction.
    """
    result = []
    for provider in PROVIDERS:
        try:
            result.append(_cached(provider, "coverage"))
        except (UpstreamMatchingError, OSError, ValueError, SyntaxError):
            result.append({"provider_id": provider, "entries": [], "version": "",
                           "available": False,
                           "counts": {"total": 0, "eligible": 0, "excluded": 0,
                                      "already_routed": 0, "other_excluded": 0}})
    from .upstream_parsehub import coverage_snapshot as parsehub_coverage
    result.append(parsehub_coverage())
    return result


def parse_upstream_url(match, url, timeout_seconds=15.0):
    """Execute one freshly verified match; return raw normalizer-compatible payload."""
    if isinstance(match, dict) and match.get("provider_id") == "parsehub_generic":
        from .upstream_parsehub import parse_upstream_url as parse_parsehub, ParseHubBridgeError
        try:
            return parse_parsehub(match, url, timeout_seconds)
        except ParseHubBridgeError as error:
            raise UpstreamMatchingError(error.code, stage=error.stage, error_class=error.error_class) from None
    try:
        validate_url(url)
        provider = match["provider_id"]
        if provider not in PROVIDERS:
            raise ValueError()
        timeout = min(30.0, max(0.1, float(timeout_seconds)))
        command, version, _ = _context(provider)
        if version != match["version"]:
            raise UpstreamMatchingError("version_changed")
        return _call(command, {"action": "parse", "provider_id": provider,
                              "version": version, "extractor_id": match["extractor_id"],
                              "url": url, "timeout_seconds": timeout}, timeout)
    except UpstreamMatchingError:
        raise
    except (ValueError, TypeError, KeyError, OSError, SyntaxError):
        raise UpstreamMatchingError("invalid_parse_request") from None
