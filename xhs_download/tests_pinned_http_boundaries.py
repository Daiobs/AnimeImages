"""Offline connect-boundary tests for the production helper and QualityProbe.

Requests, HTTPAdapter.send, urllib3 pools/connections and http.client stay real.
Only DNS, sockets and the final TLS transport are synthetic; no server is bound
and no packet is sent. TLS certificate tests perform a real SSL MemoryBIO
handshake before returning the synthetic HTTP transport. Proxy tests observe
the client-to-proxy connection, NOT the proxy's origin DNS or connection.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
import io
import ipaddress
import os
from pathlib import Path
import socket
import ssl
from tempfile import TemporaryDirectory
import threading
from unittest.mock import patch
from urllib.parse import urlsplit

from django.test import SimpleTestCase
import requests

from .services import common
from .services.quality import QualityProbe


# Public, synthetic test-only key/certificate, generated offline for the reserved
# pinned.example name. Valid 2020-2120; no service credentials or runtime crypto
# dependency. The key is deliberately public and must never secure a service.
_TEST_KEY = """-----BEGIN PRIVATE KEY-----
MIIEvAIBADANBgkqhkiG9w0BAQEFAASCBKYwggSiAgEAAoIBAQCbK21a/jOAPFXe
2dIgEu7G0U8f0vs5U7F4IQwW2szWrsBzTSJvRuIZr56G8xRKBhrAFTTfGFR/dnmN
YOd73kJIIciwFaBuc6HblqgNEHqBYUvXejEzMEssDizzPxqCyFrjaBcDWEKQpRDq
5TkhbDbXyGsY+tuBQsdE77yXfR0qIsVHLfqgB57Ii40vyIEhviidxp0q3sUk3efK
nMlkEwELK9vJX8DBRYn5ddADVyFCCqkJe37KF/buukWqJx0AG6wGoZmCUJfpHg0M
mM0wk3xsiMvv0aAQ74/xYI0lczZ4PWCicQ2WI8Bl71gFfaEE/Td39xckVTaGYRf2
74mv4RfrAgMBAAECggEAITlotlmEjHoextMr5zs2jjKF9yH1Mb6mWrr0vDYIMxWC
1o1XCK2MPwS39Fw65WiuNJ+1zOkIOqyS9T8hAYhC9+/n6TAQRGYU65A0FmwHVwmO
tCoRO80k4197p2/FDOi6cUO53bj5s7/sQedn1qy1h3l92QDpnCFAypVsc6FR69Hl
YnYtnhRjKOzq1W53scu1zCINzWkEc/0Eq2YYt279LtrCgI6jpEln0b+bAb5EGsg2
9mKvZGUz8ISPEnJhCdsNSP/Kg496+6/KB1jNtis1ZIwhECdL9Dsz1q7jVbxJ4Kus
KfWPBj/XDXTiIJTKs8bD+hXaHFxhI2sv6h/VHA5m4QKBgQDa8CRvKxObD+s07VEh
GmTE97qMQP9MnFkASh9DrrvkM6uUdZi7eE9XvSfXXcO2FqdbJoIrLz0sZ84akTRY
ROON3L02uWnN7JWTO8x1sla22Vlou7NwlDY9vB8vEakl/ig17Jmdp3o96u+GVrGl
TtyBsV7kNM3j69pIF9BfVYCwywKBgQC1b9QwyrASZsICFwFPli2fKU093amLwv1a
bXB8PVA97dKovbR2qOTA7q4w9SqrAuG+mfYXqjRn0TpP6ZblKesVKNwoavdvbrae
VhsD5LY50SX2eiN0s7c2dkLtTb8IwGabtRI+uWvedUWk3l4r2GYFZtHq2izH5yuP
zeeVMC3xYQKBgCSXJ++wallrAqtY0ktPBvJoWLQiFMjwBGvo92qvZFWSUSXBtaH3
TINkaKk00YW4KYYxXGXtQjMOskJ8bBwof0NxVeo0eLtiw+jUgKpXAOkUUD4RyL6S
9rHApBGQyQ1BeeTxQwc3O1Xn0tpH2O7AzFecljhbnuFtxJBx8nBMcPKbAoGAALeP
bB3w8jGrxJxYqfra9U0RfBQbLm032RqjnioT3hrJgVc2ll12gfivgYwGfiSFxd4P
5eoLSPm/tD8lFITE5BGERTie/7a9TATlGG/X9ISKFN5Is5ZeO/3dp9htz9gKz8S4
bL0I2xk1jEBAuQWi6ZSNcdMo6CVWu7vBlYpaNCECgYAaSTaJt5ufRnsFhpm9hsqQ
p9mtcO/cX2qQ6nNBN9GgljQ80AXgUHaNpr9o+jlJ8uZosUsfTtbev1Hf42T1JXWD
4mbq0jq325ReO/CkvLA2I4m0R4/wk/cRJ0AuTTt2yGWijQvXLCg21s/AxUAOJ/Ul
ETYm69Jkur8lG03gq4AXgw==
-----END PRIVATE KEY-----
"""
_TEST_CERT = """-----BEGIN CERTIFICATE-----
MIIDDDCCAfSgAwIBAgIBBjANBgkqhkiG9w0BAQsFADAZMRcwFQYDVQQDDA5waW5u
ZWQuZXhhbXBsZTAgFw0yMDAxMDEwMDAwMDBaGA8yMTIwMDEwMTAwMDAwMFowGTEX
MBUGA1UEAwwOcGlubmVkLmV4YW1wbGUwggEiMA0GCSqGSIb3DQEBAQUAA4IBDwAw
ggEKAoIBAQCbK21a/jOAPFXe2dIgEu7G0U8f0vs5U7F4IQwW2szWrsBzTSJvRuIZ
r56G8xRKBhrAFTTfGFR/dnmNYOd73kJIIciwFaBuc6HblqgNEHqBYUvXejEzMEss
DizzPxqCyFrjaBcDWEKQpRDq5TkhbDbXyGsY+tuBQsdE77yXfR0qIsVHLfqgB57I
i40vyIEhviidxp0q3sUk3efKnMlkEwELK9vJX8DBRYn5ddADVyFCCqkJe37KF/bu
ukWqJx0AG6wGoZmCUJfpHg0MmM0wk3xsiMvv0aAQ74/xYI0lczZ4PWCicQ2WI8Bl
71gFfaEE/Td39xckVTaGYRf274mv4RfrAgMBAAGjXTBbMBkGA1UdEQQSMBCCDnBp
bm5lZC5leGFtcGxlMA8GA1UdEwEB/wQFMAMBAf8wDgYDVR0PAQH/BAQDAgGmMB0G
A1UdDgQWBBSe2aDUpmcVpyzPoDkcaVlb1ES9GjANBgkqhkiG9w0BAQsFAAOCAQEA
S9DNhcNT6tk2ZhivtfV4b4xwoIHIdtWBCjyJwOIhJCHgozaQXm424IDdtFABfN/0
uNjmqLcgP+uJxNf27j4L8FKLGb2wFTyJjJR5naTTz1L9hHleLhegFXyiE8nCt1UB
BEQsz9NOxJmbqDXc9YR+4LK2vUdSml5lRM+CKlUJ1a+b8NkYTaDwwtJIkOQuAmiU
U/CAVt3cdkfM4M0ZXFZOOknEjM34qu0Ktov1SdlXAwjETX2oATv9XKulU2viqnAn
LAaT8ro20q1PDu76uJV2gnuk4RmH1VlzrPZQNIDTjoFeRXlpWVOKUd8BxapN4FPw
84JwcDvmXo0gWjVNTC9ZIg==
-----END CERTIFICATE-----
"""


class _SyntheticSocket:
    def __init__(self, network, family, *args, **kwargs):
        self.network = network
        self.family = family
        self.timeout = None
        self.closed = False
        self.sent = b""
        self.tunnel_replied = False
        self.peer_certificate = None
        self.connected_to = None

    def connect(self, address):
        self.connected_to = address
        self.network.connects.append((address, self.timeout))
        if self.network.barrier is not None:
            self.network.barrier.wait(timeout=5)
        if self.network.fail_connects:
            self.network.fail_connects -= 1
            self.network.now += self.network.failure_delay
            raise ConnectionRefusedError("synthetic connection refused")

    def sendall(self, data):
        self.sent += bytes(data)

    def makefile(self, *args, **kwargs):
        if self.sent.startswith(b"CONNECT ") and not self.tunnel_replied:
            self.network.tunnels.append(self.sent.decode("latin-1"))
            self.sent = b""
            self.tunnel_replied = True
            return io.BytesIO(b"HTTP/1.1 200 Connection established\r\n\r\n")
        lines = self.sent.decode("latin-1").split("\r\n")
        method, target, _version = lines[0].split(" ", 2)
        headers = dict(line.split(": ", 1) for line in lines[1:] if ": " in line)
        path = urlsplit(target).path
        request = {"method": method, "target": target, "path": path,
                   "headers": headers, "sockaddr": self.connected_to}
        self.network.requests.append(request)
        route = self.network.routes.get((method, headers["Host"], path))
        if callable(route):
            route = route(request)
        status, response_headers, body = route or (200, {"Content-Type": "image/png"}, b"abc")
        response_headers = {"Content-Length": str(len(body)), "Connection": "close", **response_headers}
        prefix = f"HTTP/1.1 {status} Synthetic\r\n"
        prefix += "".join(f"{key}: {value}\r\n" for key, value in response_headers.items())
        result = io.BytesIO(prefix.encode("latin-1") + b"\r\n" + (b"" if method == "HEAD" else body))
        self.network.response_files.append(result)
        return result

    def settimeout(self, value):
        self.timeout = value

    def gettimeout(self):
        return self.timeout

    def setsockopt(self, *args):
        pass

    def close(self):
        self.closed = True

    def shutdown(self, *args):
        self.close()

    def selected_alpn_protocol(self):
        return "http/1.1"

    def getpeercert(self, binary_form=False):
        return self.peer_certificate


class _SyntheticNetwork:
    def __init__(self):
        self.plans = {}
        self.resolutions = []
        self.counts = {}
        self.connects = []
        self.requests = []
        self.tunnels = []
        self.tls = []
        self.response_files = []
        self.routes = {}
        self.fail_connects = 0
        self.failure_delay = 0
        self.now = 1000.0
        self.barrier = None
        self.cert_file = None
        self.key_file = None
        self.server_names = []
        self.handshakes = 0
        self.tls_errors = []
        self.lock = threading.Lock()

    def resolve(self, host, port, *args, **kwargs):
        with self.lock:
            try:
                addresses = [str(ipaddress.ip_address(host))]
            except ValueError:
                count = self.counts.get(host, 0)
                answers = self.plans.get(host, [["8.8.8.8"]])
                addresses = answers[min(count, len(answers) - 1)]
                self.counts[host] = count + 1
            self.resolutions.append((host, tuple(addresses)))
        return [
            (socket.AF_INET6 if ":" in address else socket.AF_INET, socket.SOCK_STREAM,
             socket.IPPROTO_TCP, "", (address, port, 0, 0) if ":" in address else (address, port))
            for address in addresses
        ]

    def wrap_tls(self, *, sock, server_hostname, ssl_context, **kwargs):
        self.tls.append({"sni": server_hostname, "verify_mode": ssl_context.verify_mode,
                         "check_hostname": ssl_context.check_hostname,
                         "ca_certs": kwargs.get("ca_certs")})
        sock.peer_certificate = {"subjectAltName": (("DNS", server_hostname),)}
        if self.cert_file is None:
            return sock
        if kwargs.get("ca_certs") or kwargs.get("ca_cert_dir"):
            ssl_context.load_verify_locations(cafile=kwargs.get("ca_certs"), capath=kwargs.get("ca_cert_dir"))
        server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        server_context.load_cert_chain(self.cert_file, self.key_file)
        server_context.set_servername_callback(lambda _sock, name, _ctx: self.server_names.append(name))
        client_in, client_out, server_in, server_out = (ssl.MemoryBIO() for _ in range(4))
        client = ssl_context.wrap_bio(client_in, client_out, server_hostname=server_hostname)
        server = server_context.wrap_bio(server_in, server_out, server_side=True)
        client_done = server_done = False
        try:
            for _ in range(20):
                try:
                    client.do_handshake()
                    client_done = True
                except ssl.SSLWantReadError:
                    pass
                server_in.write(client_out.read())
                try:
                    server.do_handshake()
                    server_done = True
                except ssl.SSLWantReadError:
                    pass
                client_in.write(server_out.read())
                if client_done and server_done:
                    self.handshakes += 1
                    sock.peer_certificate = client.getpeercert()
                    return sock
        except ssl.SSLCertVerificationError as error:
            self.tls_errors.append(error.verify_message)
            raise
        raise AssertionError("in-memory TLS handshake did not complete")

    def advance(self, seconds):
        self.now += seconds

    def ips(self):
        return [address[0] for address, _timeout in self.connects]


class PinnedHTTPBoundaryTests(SimpleTestCase):
    def setUp(self):
        self.network = _SyntheticNetwork()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        # Clearing environment alone does not disable macOS system proxies.
        self.stack.enter_context(patch.dict(os.environ, {"NO_PROXY": "*"}, clear=True))
        self.stack.enter_context(patch("socket.getaddrinfo", side_effect=self.network.resolve))
        self.stack.enter_context(patch("socket.socket", side_effect=lambda *a, **kw: _SyntheticSocket(self.network, *a, **kw)))
        self.stack.enter_context(patch("urllib3.connection.ssl_wrap_socket", side_effect=self.network.wrap_tls))
        self.stack.enter_context(patch.object(common.time, "monotonic", side_effect=lambda: self.network.now))
        self.sleep = self.stack.enter_context(patch.object(common.time, "sleep", side_effect=self.network.advance))
        self.stack.enter_context(patch("requests.sessions.get_netrc_auth", side_effect=AssertionError("netrc must stay disabled")))

    def get(self, url="https://pinned.example/file", **kwargs):
        response = common.request_get_with_retry(url, retries=kwargs.pop("retries", 1), stream=True, **kwargs)
        self.addCleanup(response.close)
        return response

    def enable_real_tls(self):
        directory = self.stack.enter_context(TemporaryDirectory(prefix="pinned-http-test-"))
        self.network.cert_file = str(Path(directory) / "synthetic-cert.pem")
        self.network.key_file = str(Path(directory) / "public-test-key.pem")
        Path(self.network.cert_file).write_text(_TEST_CERT, encoding="ascii")
        Path(self.network.key_file).write_text(_TEST_KEY, encoding="ascii")
        return self.network.cert_file

    def test_rebinding_cannot_change_actual_connect_ipv4_ipv6_http_https(self):
        for address in ("8.8.8.8", "2606:4700:4700::1111"):
            for scheme in ("http", "https"):
                with self.subTest(address=address, scheme=scheme):
                    host = f"case{len(self.network.requests)}.example"
                    self.network.plans[host] = [[address], ["10.20.30.40"]]
                    response = self.get(f"{scheme}://{host}/file", headers={"Host": "wrong.example"})
                    # Session and its pool have already closed, but the final stream is live.
                    self.assertEqual(response.content, b"abc")
                    self.assertEqual(response.url, f"{scheme}://{host}/file")
                    self.assertEqual(self.network.ips()[-1], address)
                    self.assertEqual(self.network.counts[host], 1)
                    self.assertEqual(self.network.requests[-1]["headers"]["Host"], host)
                    if scheme == "https":
                        self.assertEqual(self.network.tls[-1]["sni"], host)
                        self.assertEqual(self.network.tls[-1]["verify_mode"], ssl.CERT_REQUIRED)

    def test_quality_head_and_range_use_the_pinned_connection(self):
        for strategy in ("head_first", "range_first"):
            with self.subTest(strategy=strategy):
                host = f"{strategy}.xhscdn.com"
                self.network.plans[host] = [["8.8.8.8"], ["10.20.30.40"]]
                result = QualityProbe().probe(f"https://{host}/file", strategy=strategy, allow_fallback=False)
                self.assertTrue(result.reachable, result.error)
                self.assertEqual(self.network.ips()[-1], "8.8.8.8")
                self.assertEqual(self.network.counts[host], 1)
                request = self.network.requests[-1]
                self.assertEqual(request["method"], "HEAD" if strategy == "head_first" else "GET")
                if strategy == "range_first":
                    self.assertEqual(request["headers"]["Range"], "bytes=0-0")
        self.assertTrue(all(response.closed for response in self.network.response_files))

    def test_private_dns_never_connects_for_helper_or_quality(self):
        self.network.plans["private.xhscdn.com"] = [["10.20.30.40"]]
        with self.assertRaises(common.XhsSecurityError):
            self.get("http://private.xhscdn.com/file")
        result = QualityProbe().probe("http://private.xhscdn.com/file")
        self.assertTrue(result.terminal)
        self.assertFalse(result.reachable)
        self.assertEqual(self.network.connects, [])

    def test_each_redirect_pins_its_own_host_and_strips_sensitive_headers(self):
        self.network.plans.update({"first.example": [["8.8.8.8"], ["10.1.1.1"]],
                                   "second.example": [["1.1.1.1"], ["10.2.2.2"]]})
        self.network.routes[("GET", "first.example", "/file")] = (
            302, {"Location": "https://second.example/final", "Set-Cookie": "secret=value"}, b"redirect")
        response = self.get("https://first.example/file", headers={"Authorization": "Bearer secret", "Cookie": "secret=value"})
        self.assertEqual(response.content, b"abc")
        self.assertEqual(self.network.ips(), ["8.8.8.8", "1.1.1.1"])
        self.assertEqual([event["sni"] for event in self.network.tls], ["first.example", "second.example"])
        final_headers = self.network.requests[-1]["headers"]
        self.assertEqual(final_headers["Host"], "second.example")
        self.assertNotIn("Authorization", final_headers)
        self.assertNotIn("Cookie", final_headers)
        self.assertTrue(self.network.response_files[0].closed)

    def test_connect_fallback_uses_admitted_addresses_without_http_retry(self):
        self.network.plans["pinned.example"] = [["8.8.8.8", "1.1.1.1"]]
        self.network.fail_connects = 1
        self.assertEqual(self.get(retries=2, sleep_seconds=0).content, b"abc")
        self.assertEqual(self.network.ips(), ["8.8.8.8", "1.1.1.1"])
        self.assertEqual(self.network.counts["pinned.example"], 1)
        self.assertEqual(len(self.network.requests), 1)
        self.sleep.assert_not_called()

    def test_address_fallback_shares_original_connect_timeout(self):
        self.network.plans["pinned.example"] = [["8.8.8.8", "1.1.1.1", "9.9.9.9"]]
        self.network.fail_connects = 10
        self.network.failure_delay = 2
        with self.assertRaises(requests.ConnectTimeout):
            self.get(retries=1, timeout=3, deadline_monotonic=1010)
        self.assertEqual(self.network.ips(), ["8.8.8.8", "1.1.1.1"])
        self.assertEqual([timeout for _address, timeout in self.network.connects], [3, 1])
        self.assertEqual(self.network.counts["pinned.example"], 1)
        self.assertEqual(self.network.requests, [])
        self.sleep.assert_not_called()

    def test_single_get_attempt_reaches_fourth_admitted_address(self):
        addresses = ["2606:4700:4700::1111", "2606:4700:4700::1001",
                     "2001:4860:4860::8888", "8.8.8.8"]
        self.network.plans["pinned.example"] = [addresses]
        connect = _SyntheticSocket.connect

        def connect_with_unreachable_ipv6(sock, address):
            connect(sock, address)
            if ":" in address[0]:
                raise OSError("synthetic IPv6 route unavailable")

        with patch.object(_SyntheticSocket, "connect", connect_with_unreachable_ipv6):
            response = self.get(retries=1)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"abc")
        self.assertEqual(self.network.ips(), addresses)
        self.assertEqual(self.network.counts["pinned.example"], 1)
        self.assertEqual(len(self.network.requests), 1)
        self.assertEqual(self.network.requests[0]["method"], "GET")
        self.sleep.assert_not_called()

    def test_quality_probe_reaches_fourth_admitted_address_with_one_dns_lookup(self):
        addresses = ["2606:4700:4700::1111", "2606:4700:4700::1001",
                     "2001:4860:4860::8888", "8.8.8.8"]
        connect = _SyntheticSocket.connect

        def connect_with_unreachable_ipv6(sock, address):
            connect(sock, address)
            if ":" in address[0]:
                raise OSError("synthetic IPv6 route unavailable")

        for strategy in ("head_first", "range_first"):
            with self.subTest(strategy=strategy):
                host = f"fourth-{strategy}.xhscdn.com"
                self.network.plans[host] = [addresses]
                before_connects = len(self.network.connects)
                before_requests = len(self.network.requests)
                with patch.object(_SyntheticSocket, "connect", connect_with_unreachable_ipv6):
                    result = QualityProbe().probe(f"https://{host}/file", strategy=strategy)
                self.assertTrue(result.reachable, result.error)
                self.assertEqual(result.size_bytes, 3)
                self.assertEqual(self.network.ips()[before_connects:], addresses)
                self.assertEqual(self.network.counts[host], 1)
                self.assertEqual(len(self.network.requests) - before_requests, 1)
                request = self.network.requests[-1]
                self.assertEqual(request["method"], "HEAD" if strategy == "head_first" else "GET")
                if strategy == "range_first":
                    self.assertEqual(request["headers"]["Range"], "bytes=0-0")
                self.sleep.assert_not_called()

    def test_retry_revalidates_dns_and_rejects_new_private_answer(self):
        self.network.plans["pinned.example"] = [["8.8.8.8"], ["10.20.30.40"]]
        self.network.fail_connects = 1
        with self.assertRaises(common.XhsSecurityError):
            self.get(retries=3, sleep_seconds=0)
        self.assertEqual(self.network.ips(), ["8.8.8.8"])
        self.assertEqual(self.network.requests, [])

    def test_connection_failures_obey_finite_retry_count(self):
        self.network.fail_connects = 10
        with self.assertRaises(requests.ConnectionError):
            self.get(retries=3, sleep_seconds=0)
        self.assertEqual(len(self.network.connects), 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_connection_failure_backoff_and_retry_share_one_deadline(self):
        self.network.fail_connects = 10
        self.network.failure_delay = 2
        with self.assertRaises(requests.Timeout):
            self.get(retries=10, sleep_seconds=1, deadline_monotonic=1005)
        self.assertEqual(len(self.network.connects), 2)
        self.assertEqual([timeout for _address, timeout in self.network.connects], [2.5, 1.0])
        self.sleep.assert_called_once_with(1)

    def test_custom_environment_ca_keeps_real_tls_sni_and_certificate_checks(self):
        ca = self.enable_real_tls()
        for key in ("REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE"):
            with self.subTest(key=key), patch.dict(os.environ, {key: ca}):
                self.assertEqual(self.get().content, b"abc")
                self.assertEqual(self.network.tls[-1]["ca_certs"], ca)
                self.assertEqual(self.network.tls[-1]["verify_mode"], ssl.CERT_REQUIRED)
                self.assertTrue(self.network.tls[-1]["check_hostname"])
        self.assertEqual(self.network.handshakes, 2)
        self.assertEqual(self.network.server_names, ["pinned.example"] * 2)

    def test_real_tls_wrong_hostname_is_rejected_before_http(self):
        ca = self.enable_real_tls()
        with patch.dict(os.environ, {"REQUESTS_CA_BUNDLE": ca}):
            with self.assertRaises(requests.exceptions.SSLError):
                self.get("https://wrong.example/file")
        self.assertEqual(self.network.requests, [])
        self.assertEqual(self.network.handshakes, 0)
        self.assertTrue(self.network.tls_errors)
        self.assertIn("hostname", self.network.tls_errors[0].lower())

    def test_real_tls_untrusted_certificate_is_rejected_before_http(self):
        self.enable_real_tls()
        with self.assertRaises(requests.exceptions.SSLError):
            self.get()
        self.assertEqual(self.network.requests, [])
        self.assertEqual(self.network.handshakes, 0)
        self.assertTrue(self.network.tls_errors)
        self.assertIn("self-signed", self.network.tls_errors[0].lower())

    def test_proxy_transport_is_stock_and_no_proxy_restores_direct_pinning(self):
        self.network.plans["proxy.example"] = [["9.9.9.9"]]
        ca = requests.certs.where()
        for scheme in ("http", "https"):
            for bypass in (False, True):
                with self.subTest(scheme=scheme, bypass=bypass):
                    environment = {scheme.upper() + "_PROXY": "http://proxy.example:3128",
                                   "NO_PROXY": "pinned.example" if bypass else "",
                                   "REQUESTS_CA_BUNDLE": ca}
                    with patch.dict(os.environ, environment):
                        self.assertEqual(self.get(f"{scheme}://pinned.example/file").content, b"abc")
                    self.assertEqual(self.network.ips()[-1], "8.8.8.8" if bypass else "9.9.9.9")
                    if scheme == "http" and not bypass:
                        self.assertEqual(self.network.requests[-1]["target"], "http://pinned.example/file")
                    if scheme == "https":
                        self.assertEqual(self.network.tls[-1]["ca_certs"], ca)
                        self.assertEqual(self.network.tls[-1]["sni"], "pinned.example")
                        self.assertEqual(self.network.tls[-1]["verify_mode"], ssl.CERT_REQUIRED)
        self.assertEqual(len(self.network.tunnels), 1)
        self.assertTrue(self.network.tunnels[0].startswith("CONNECT pinned.example:443 "))

    def test_concurrent_helpers_keep_origin_pools_host_and_sni_isolated(self):
        self.network.barrier = threading.Barrier(2)
        hosts = ("one.example", "two.example")
        for host in hosts:
            self.network.plans[host] = [["8.8.8.8"], ["10.20.30.40"]]
        admissions = []
        connect = common._PinnedAddressAdapter.get_connection_with_tls_context

        def observe(adapter, *args, **kwargs):
            pool = connect(adapter, *args, **kwargs)
            with self.network.lock:
                admissions.append((adapter, pool))
            return pool

        def fetch(host):
            with common.request_get_with_retry(f"https://{host}/file", retries=1, stream=True) as response:
                return response.content

        with patch.object(common._PinnedAddressAdapter, "get_connection_with_tls_context", observe):
            with ThreadPoolExecutor(max_workers=2) as executor:
                self.assertEqual(list(executor.map(fetch, hosts)), [b"abc", b"abc"])
        self.assertEqual(self.network.ips(), ["8.8.8.8"] * 2)
        self.assertCountEqual([request["headers"]["Host"] for request in self.network.requests], hosts)
        self.assertCountEqual([event["sni"] for event in self.network.tls], hosts)
        self.assertIsNot(admissions[0][0].poolmanager, admissions[1][0].poolmanager)
        self.assertIsNot(admissions[0][1], admissions[1][1])
        self.assertCountEqual([pool.conn_kw["server_hostname"] for _adapter, pool in admissions], hosts)
        self.assertTrue(all(pool.assert_hostname is None for _adapter, pool in admissions))

    def test_legacy_connection_entry_uses_same_origin_scoped_pin(self):
        # Exercises the 2.32.0/1 entry name; this is not an old-version runtime matrix.
        adapter = common._PinnedAddressAdapter("https://pinned.example/file", "8.8.8.8")
        self.addCleanup(adapter.close)
        prepared = requests.Request("GET", "https://pinned.example/file").prepare()
        pool = adapter._get_connection(prepared, True, proxies={})
        self.assertEqual(pool.host, "8.8.8.8")
        self.assertEqual(pool.conn_kw["server_hostname"], "pinned.example")
        self.assertIsNone(pool.assert_hostname)
        wrong = requests.Request("GET", "https://other.example/file").prepare()
        with self.assertRaises(common.XhsSecurityError):
            adapter._get_connection(wrong, True, proxies={})
        with self.assertRaises(common.XhsSecurityError):
            adapter._get_connection(prepared, False, proxies={})
        self.assertEqual(self.network.connects, [])

    def test_quality_head_range_share_remaining_connection_deadline(self):
        host = "pinned.xhscdn.com"

        def slow_head(_request):
            self.network.advance(4)
            return 405, {}, b""

        self.network.routes[("HEAD", host, "/file")] = slow_head
        result = QualityProbe().probe(f"http://{host}/file", timeout=6)
        self.assertTrue(result.reachable, result.error)
        self.assertEqual(result.method, "RANGE")
        self.assertEqual([timeout for _address, timeout in self.network.connects], [3.0, 1.0])
        self.assertEqual([request["method"] for request in self.network.requests], ["HEAD", "GET"])

    def test_quality_expired_head_budget_prevents_fallback_connection(self):
        host = "pinned.xhscdn.com"

        def slow_head(_request):
            self.network.advance(7)
            return 405, {}, b""

        self.network.routes[("HEAD", host, "/file")] = slow_head
        result = QualityProbe().probe(f"http://{host}/file", timeout=6)
        self.assertFalse(result.reachable)
        self.assertEqual(len(self.network.connects), 1)
        self.assertEqual([request["method"] for request in self.network.requests], ["HEAD"])
        self.assertTrue(all(response.closed for response in self.network.response_files))

    def test_quality_redirect_budget_is_shared_across_head_and_range(self):
        host = "pinned.xhscdn.com"
        for method in ("HEAD", "GET"):
            for index in range(3):
                self.network.routes[(method, host, f"/hop{index}")] = (
                    302, {"Location": f"/hop{index + 1}"}, b"")
            self.network.routes[(method, host, "/hop3")] = (405, {}, b"")
        result = QualityProbe().probe(f"http://{host}/hop0")
        self.assertTrue(result.terminal)
        self.assertFalse(result.reachable)
        self.assertEqual([request["method"] for request in self.network.requests], ["HEAD"] * 4 + ["GET"] * 3)
        self.assertEqual([request["path"] for request in self.network.requests],
                         ["/hop0", "/hop1", "/hop2", "/hop3", "/hop0", "/hop1", "/hop2"])
        self.assertEqual(len(self.network.connects), 7)
        self.assertTrue(all(response.closed for response in self.network.response_files))
