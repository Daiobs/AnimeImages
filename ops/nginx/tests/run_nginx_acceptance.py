#!/usr/bin/env python3
"""Run an isolated Nginx route acceptance test against a synthetic app."""

import argparse
import http.client
import http.server
import json
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlsplit


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MEDIA_SNIPPET = PROJECT_ROOT / "ops" / "nginx" / "snippets" / "animeimages-v1-media.conf"
SITE_TEMPLATE = PROJECT_ROOT / "ops" / "nginx" / "sites" / "download.cosflow.icu.conf.template"
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8000
NGINX_HOST = "127.0.0.1"
NGINX_PORT = 18080
REDIRECT_PORT = 18081
REQUIRED_MEDIA_PATHS = (
    "/media/upstream-candidate/",
    "/media/xhs-livephoto-motion-descriptor/",
    "/media/xhs-livephoto-pair-descriptor/",
    "/media/ytdlp-selected-format/",
    "/media/service-selected-format/",
)
VALID_TELEMETRY = {
    "schema_version": 1,
    "download_id": "0" * 32,
    "state": "failed",
    "error_code": "UPSTREAM_FETCH_FAILED",
}


class SyntheticApplication(http.server.BaseHTTPRequestHandler):
    seen_requests = []

    def log_message(self, _format, *_args):
        return

    def _response(self, status, body=b"", headers=None):
        self.send_response(status)
        for name, value in headers or ():
            self.send_header(name, value)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if self.command != "HEAD" and body:
            self.wfile.write(body)

    def do_GET(self):
        self.seen_requests.append((self.command, self.path))
        if urlsplit(self.path).path in REQUIRED_MEDIA_PATHS + ("/media/browser-download-telemetry/",):
            self._response(405, headers=(("Allow", "POST"),))
        elif self.path == "/health/ready/":
            self._response(200, b'{"status":"ready"}')
        elif self.path == "/ordinary-media-download-sw.js":
            self._response(
                200,
                b"self.addEventListener('fetch', () => {});",
                (("Content-Type", "text/javascript"), ("Service-Worker-Allowed", "/")),
            )
        elif self.path == "/":
            self._response(200, b"animeimages-home")
        elif self.path in (
            "/media/proxy-image/", "/media/download-images/", "/media/download-tasks/",
            "/media/download-tasks/synthetic/file/", "/media/douyin-continuation/",
            "/media/download-prepare/", "/media/download-status/synthetic/",
            "/media/download-stream/synthetic/",
        ):
            self._response(200, b"synthetic-existing-media-route")
        else:
            self._response(404)

    def do_POST(self):
        self.seen_requests.append((self.command, self.path))
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
        if urlsplit(self.path).path in REQUIRED_MEDIA_PATHS:
            # Routing fixture only; real Django guards are tested separately.
            self._response(403, b"synthetic-application-rejection")
            return
        if self.path != "/media/browser-download-telemetry/":
            self._response(404)
            return
        if not self.headers.get("X-CSRFToken"):
            self._response(403)
            return
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._response(400)
            return
        self._response(204 if payload == VALID_TELEMETRY else 400)


def _free(port):
    with socket.socket() as probe:
        # Match the servers' reuse policy between sequential deletion checks.
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            probe.bind(("127.0.0.1", port))
        except OSError:
            return False
    return True


def _request(method, path, body=None, headers=None):
    connection = http.client.HTTPConnection(NGINX_HOST, NGINX_PORT, timeout=5)
    connection.request(method, path, body=body, headers=headers or {})
    response = connection.getresponse()
    data = response.read()
    result = (response.status, dict(response.getheaders()), data)
    connection.close()
    return result


def _expect(label, actual, expected):
    if actual != expected:
        raise AssertionError(f"{label}: expected {expected!r}, received {actual!r}")
    print(f"{label}: PASS ({actual})")


def _nginx_config(prefix, snippet_path, static_path):
    site = SITE_TEMPLATE.read_text().replace("{{ANIMEIMAGES_SERVER_IP}}", NGINX_HOST)
    substitutions = {
        "listen 443 ssl;": f"listen {NGINX_HOST}:{NGINX_PORT};",
        "listen 80;": f"listen {NGINX_HOST}:{REDIRECT_PORT};",
        "alias /srv/animeimages/staticfiles/;": f"alias {static_path}/;",
        "include /etc/nginx/snippets/animeimages-v1-media.conf;": f"include {snippet_path};",
        "ssl_certificate /etc/letsencrypt/live/download.cosflow.icu/fullchain.pem;": "",
        "ssl_certificate_key /etc/letsencrypt/live/download.cosflow.icu/privkey.pem;": "",
        "include /etc/letsencrypt/options-ssl-nginx.conf;": "",
        "ssl_dhparam /etc/letsencrypt/ssl-dhparams.pem;": "",
    }
    for original, replacement in substitutions.items():
        if site.count(original) != 1:
            raise RuntimeError(f"managed template substitution is ambiguous: {original}")
        site = site.replace(original, replacement, 1)
    return f"""
worker_processes 1;
pid {prefix / 'nginx.pid'};
error_log {prefix / 'error.log'} notice;

events {{
    worker_connections 64;
}}

http {{
    access_log off;
    client_body_temp_path {prefix / 'client_body_temp'};
    proxy_temp_path {prefix / 'proxy_temp'};

    {site}
}}
"""


def without_exact_location(snippet, path):
    # These managed locations are flat blocks; fail if that shape ever changes.
    changed, count = re.subn(
        r"(?m)^location = " + re.escape(path) + r" \{[^{}]*^\}\n?",
        "", snippet,
    )
    if count != 1:
        raise AssertionError(f"expected one removable exact location for {path}, found {count}")
    return changed


class RoutesNotReached(AssertionError):
    def __init__(self, paths):
        self.paths = paths
        super().__init__(f"required routes did not reach upstream: {paths}")


def _required_media_reachability():
    missing = []
    for path in REQUIRED_MEDIA_PATHS:
        before = len(SyntheticApplication.seen_requests)
        get = _request("GET", path)
        post_path = path + "?boundary=synthetic"
        post = _request("POST", post_path, body=b"{}", headers={"Content-Type": "application/json"})
        received = SyntheticApplication.seen_requests[before:]
        expected = [("GET", path), ("POST", post_path)]
        if received != expected:
            print(f"upstream_missing: {path} statuses={(get[0], post[0])} received={received}")
            missing.append(path)
            continue
        _expect(f"upstream_received {path}", received, expected)
        _expect(f"synthetic_method_rejection {path}", get[0], 405)
        _expect(f"synthetic_post_rejection {path}", post[0], 403)
    if missing:
        raise RoutesNotReached(missing)


def run(nginx_binary, omitted_route=None):
    if not MEDIA_SNIPPET.is_file():
        raise RuntimeError("canonical media snippet is missing")
    if not all(_free(port) for port in (UPSTREAM_PORT, NGINX_PORT, REDIRECT_PORT)):
        raise RuntimeError("isolated acceptance ports 8000, 18080 and 18081 must be available")

    SyntheticApplication.seen_requests = []
    upstream = http.server.ThreadingHTTPServer(
        (UPSTREAM_HOST, UPSTREAM_PORT), SyntheticApplication
    )
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    nginx = None
    try:
        with tempfile.TemporaryDirectory(prefix="animeimages-nginx-") as directory:
            prefix = Path(directory)
            prefix.chmod(0o755)
            (prefix / "client_body_temp").mkdir()
            (prefix / "proxy_temp").mkdir()
            static_path = prefix / "static"
            static_path.mkdir()
            (static_path / "baseline.txt").write_text("static-ok")
            config_path = prefix / "nginx.conf"
            snippet_path = MEDIA_SNIPPET
            if omitted_route is not None:
                snippet_path = prefix / "mutated-media.conf"
                snippet_path.write_text(without_exact_location(MEDIA_SNIPPET.read_text(), omitted_route))
            config_path.write_text(_nginx_config(prefix, snippet_path, static_path))

            test_result = subprocess.run(
                [nginx_binary, "-p", f"{prefix}/", "-c", str(config_path), "-t"],
                check=False,
                text=True,
                capture_output=True,
            )
            if test_result.returncode != 0:
                raise RuntimeError("isolated nginx -t failed: " + test_result.stderr.strip())
            print("nginx_syntax: PASS")

            nginx = subprocess.Popen(
                [nginx_binary, "-p", f"{prefix}/", "-c", str(config_path), "-g", "daemon off;"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
                text=True,
            )
            for _ in range(50):
                if nginx.poll() is not None:
                    error = nginx.stderr.read().strip() if nginx.stderr else ""
                    raise RuntimeError("isolated Nginx exited early: " + error)
                try:
                    if _request("GET", "/")[0] == 200:
                        break
                except OSError:
                    time.sleep(0.05)
            else:
                raise RuntimeError("isolated Nginx did not become ready")

            _required_media_reachability()
            _expect("homepage", _request("GET", "/")[0], 200)
            _expect("readiness", _request("GET", "/health/ready/")[0], 200)
            telemetry_get = _request("GET", "/media/browser-download-telemetry/")
            _expect("telemetry_get", telemetry_get[0], 405)
            _expect("telemetry_allow", telemetry_get[1].get("Allow"), "POST")
            _expect(
                "telemetry_no_csrf",
                _request(
                    "POST",
                    "/media/browser-download-telemetry/",
                    body=b"{}",
                    headers={"Content-Type": "application/json"},
                )[0],
                403,
            )
            _expect(
                "telemetry_invalid_schema",
                _request(
                    "POST",
                    "/media/browser-download-telemetry/",
                    body=b"{}",
                    headers={"Content-Type": "application/json", "X-CSRFToken": "synthetic"},
                )[0],
                400,
            )
            _expect(
                "telemetry_valid_synthetic",
                _request(
                    "POST",
                    "/media/browser-download-telemetry/",
                    body=json.dumps(VALID_TELEMETRY).encode("utf-8"),
                    headers={"Content-Type": "application/json", "X-CSRFToken": "synthetic"},
                )[0],
                204,
            )
            blocked_paths = ["/media/", "/media/test", "/media/unknown/", "/media/upload/",
                             "/media/uploads/private.jpg", "/media/2026/10/private.jpg"]
            blocked_paths.extend(path + "subpath/" for path in REQUIRED_MEDIA_PATHS)
            blocked_paths.append("/media/browser-download-telemetry/subpath/")
            for path in blocked_paths:
                for method in ("GET", "POST"):
                    before = len(SyntheticApplication.seen_requests)
                    _expect(f"nginx_deny {method} {path}", _request(method, path)[0], 404)
                    _expect("no_upstream_request", SyntheticApplication.seen_requests[before:], [])
            for path in REQUIRED_MEDIA_PATHS:
                for method in ("GET", "POST"):
                    before = len(SyntheticApplication.seen_requests)
                    redirect = _request(method, path.rstrip("/"))
                    _expect(f"nginx_slash_redirect {method} {path}", redirect[0], 301)
                    _expect("canonical_redirect_path", urlsplit(redirect[1]["Location"]).path, path)
                    _expect("no_upstream_request", SyntheticApplication.seen_requests[before:], [])
            for path in (
                "/media/proxy-image/", "/media/download-images/", "/media/download-tasks/",
                "/media/download-tasks/synthetic/file/", "/media/douyin-continuation/",
                "/media/download-prepare/", "/media/download-status/synthetic/",
                "/media/download-stream/synthetic/",
            ):
                before = len(SyntheticApplication.seen_requests)
                _expect(f"existing_media {path}", _request("GET", path)[0], 200)
                _expect("existing_media_upstream", SyntheticApplication.seen_requests[before:], [("GET", path)])
            worker = _request("GET", "/ordinary-media-download-sw.js")
            _expect("service_worker", worker[0], 200)
            _expect("service_worker_scope", worker[1].get("Service-Worker-Allowed"), "/")
            _expect("static", _request("GET", "/static/baseline.txt")[0], 200)
            print("ordinary_provider_proxy: PASS (none configured)")
    finally:
        if nginx is not None and nginx.poll() is None:
            nginx.terminate()
            try:
                nginx.wait(timeout=5)
            except subprocess.TimeoutExpired:
                nginx.kill()
                nginx.wait(timeout=5)
        upstream.shutdown()
        upstream.server_close()
        upstream_thread.join(timeout=5)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--nginx", default=shutil.which("nginx"))
    args = parser.parse_args()
    if not args.nginx:
        print("CANNOT_VERIFY: nginx executable is unavailable", file=sys.stderr)
        return 2
    try:
        run(args.nginx)
        for path in REQUIRED_MEDIA_PATHS:
            try:
                run(args.nginx, omitted_route=path)
            except RoutesNotReached as error:
                _expect(f"deletion_countertest {path}", error.paths, [path])
            else:
                raise AssertionError(f"removing {path} did not fail reachability")
    except (AssertionError, OSError, RuntimeError) as error:
        print(f"ACCEPTANCE_FAILED: {error}", file=sys.stderr)
        return 1
    print("NGINX_ACCEPTANCE_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
