#!/usr/bin/env python3
"""Run an isolated Nginx route acceptance test against a synthetic app."""

import argparse
import http.client
import http.server
import json
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[3]
MEDIA_SNIPPET = PROJECT_ROOT / "ops" / "nginx" / "snippets" / "animeimages-v1-media.conf"
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 8000
NGINX_HOST = "127.0.0.1"
NGINX_PORT = 18080
VALID_TELEMETRY = {
    "schema_version": 1,
    "download_id": "0" * 32,
    "state": "failed",
    "error_code": "UPSTREAM_FETCH_FAILED",
}


class SyntheticApplication(http.server.BaseHTTPRequestHandler):
    seen_paths = []

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
        self.seen_paths.append(self.path)
        if self.path == "/media/browser-download-telemetry/":
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
        else:
            self._response(404)

    def do_POST(self):
        self.seen_paths.append(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length)
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

    server {{
        listen {NGINX_HOST}:{NGINX_PORT};
        server_name localhost;

        location /static/ {{
            alias {static_path}/;
        }}

        location /media/proxy-image/ {{
            proxy_pass http://127.0.0.1:8000;
        }}

        location /media/download-images/ {{
            proxy_pass http://127.0.0.1:8000;
        }}

        location /media/download-tasks/ {{
            proxy_pass http://127.0.0.1:8000;
        }}

        include {snippet_path};

        location /media/ {{
            return 404;
        }}

        location / {{
            proxy_pass http://127.0.0.1:8000;
            proxy_set_header Host $host;
            proxy_set_header X-Forwarded-Proto $scheme;
            proxy_set_header X-Request-ID $request_id;
        }}
    }}
}}
"""


def run(nginx_binary):
    if not MEDIA_SNIPPET.is_file():
        raise RuntimeError("canonical media snippet is missing")
    if not _free(UPSTREAM_PORT) or not _free(NGINX_PORT):
        raise RuntimeError("acceptance ports 8000 and 18080 must be available")

    SyntheticApplication.seen_paths = []
    upstream = http.server.ThreadingHTTPServer(
        (UPSTREAM_HOST, UPSTREAM_PORT), SyntheticApplication
    )
    upstream_thread = threading.Thread(target=upstream.serve_forever, daemon=True)
    upstream_thread.start()

    nginx = None
    try:
        with tempfile.TemporaryDirectory(prefix="animeimages-nginx-") as directory:
            prefix = Path(directory)
            (prefix / "client_body_temp").mkdir()
            (prefix / "proxy_temp").mkdir()
            static_path = prefix / "static"
            static_path.mkdir()
            (static_path / "baseline.txt").write_text("static-ok")
            config_path = prefix / "nginx.conf"
            config_path.write_text(_nginx_config(prefix, MEDIA_SNIPPET, static_path))

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
            _expect("generic_media_guard", _request("GET", "/media/test")[0], 404)
            worker = _request("GET", "/ordinary-media-download-sw.js")
            _expect("service_worker", worker[0], 200)
            _expect("service_worker_scope", worker[1].get("Service-Worker-Allowed"), "/")
            _expect("static", _request("GET", "/static/baseline.txt")[0], 200)
            if "/media/test" in SyntheticApplication.seen_paths:
                raise AssertionError("broad /media request reached the application upstream")
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
    except (AssertionError, OSError, RuntimeError) as error:
        print(f"ACCEPTANCE_FAILED: {error}", file=sys.stderr)
        return 1
    print("NGINX_ACCEPTANCE_PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
