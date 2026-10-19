import http.client
import io
import struct
import time
import unittest
from contextlib import ExitStack
from unittest.mock import MagicMock, Mock, patch
from urllib.error import HTTPError

from tools.upstream_metadata_probe import MAX_BYTES, RangeRead, probe_iso_bmff_tracks, probe_in_guarded_child


def box(kind, data):
    return struct.pack(">I4s", len(data) + 8, kind) + data


def movie(*handlers):
    return box(b"moov", b"".join(box(b"trak", box(b"mdia", box(b"hdlr", b"\0" * 8 + h + b"\0" * 12))) for h in handlers))


class ProbeTests(unittest.TestCase):
    def probe(self, data):
        calls = []
        def read(url, *, start, length, deadline_monotonic):
            calls.append((start, length))
            self.assertLessEqual(length, MAX_BYTES)
            self.assertLessEqual(len(calls), 2)
            return RangeRead(start, len(data), data[start:start + length])
        result = probe_iso_bmff_tracks("https://example.org/a.mp4", guarded_range_reader=read,
                                       deadline_monotonic=time.monotonic() + 5)
        return result, calls

    def test_both_and_separate_tracks(self):
        for handlers, expected in (((b"vide", b"soun"), ("present", "present")),
                                   ((b"vide",), ("present", "absent")),
                                   ((b"soun",), ("absent", "present"))):
            result, _ = self.probe(movie(*handlers))
            self.assertTrue(result["complete_moov"])
            self.assertEqual((result["video"], result["audio"]), expected)
            self.assertNotIn("acodec", result)

    def test_tail_moov_uses_structural_offset(self):
        data = box(b"ftyp", b"isom\0\0\0\0") + box(b"mdat", b"x" * (MAX_BYTES * 2)) + movie(b"vide", b"soun")
        result, calls = self.probe(data)
        self.assertTrue(result["complete_moov"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1][0], 16 + 8 + MAX_BYTES * 2)

    def test_fake_strings_truncation_missing_handler_and_bad_size_are_unknown(self):
        for data in (box(b"mdat", movie(b"vide", b"soun")), movie(b"vide", b"soun")[:-1],
                     box(b"moov", box(b"trak", box(b"mdia", b""))), b"\0\0\0\4moov"):
            result, _ = self.probe(data)
            self.assertFalse(result["complete_moov"])
            self.assertEqual(result["audio"], "unknown")

    def test_budget_and_oversized_moov(self):
        result, calls = self.probe(box(b"moov", b"x" * (MAX_BYTES + 1)))
        self.assertEqual(result["reason"], "moov_over_budget")
        self.assertEqual(len(calls), 1)

    def test_two_requests_never_become_three(self):
        padding = box(b"mdat", b"x" * (MAX_BYTES * 2))
        result, calls = self.probe(padding + padding + movie(b"vide", b"soun"))
        self.assertEqual(len(calls), 2)
        self.assertEqual(result["reason"], "request_budget")
        self.assertFalse(result["complete_moov"])

    def test_extended_size_box(self):
        normal = movie(b"vide", b"soun")
        extended = struct.pack(">I4sQ", 1, b"moov", len(normal) + 8) + normal[8:]
        result, _ = self.probe(extended)
        self.assertTrue(result["complete_moov"])

    def test_deadline_no_request(self):
        def read(*args, **kw):
            self.fail("network must not run")
        result = probe_iso_bmff_tracks("https://example.org/a.mp4", guarded_range_reader=read,
                                       deadline_monotonic=time.monotonic() - 1)
        self.assertEqual(result["reason"], "deadline")

    def test_main_process_network_entry_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "installed_child_guard"):
            probe_in_guarded_child("https://example.org/a.mp4", deadline_monotonic=time.monotonic() + 5)


class GuardedReaderTests(unittest.TestCase):
    url = "https://example.org/a.mp4"

    def response(self, *, status=206, headers=None, body=None, url=None):
        data = movie(b"vide", b"soun") if body is None else body
        response = MagicMock()
        response.__enter__.return_value = response
        response.status = status
        response.headers = {"Content-Range": f"bytes 0-{len(data) - 1}/{len(data)}", **(headers or {})}
        response.geturl.return_value = url or self.url
        response.read.return_value = data
        return response

    def run_probe(self, response=None, *, error=None, deadline=105.0, during_open=None):
        # Mock both transport and guard identity: never install process-global socket guards in CI.
        def guarded_connection(*args, **kwargs):
            raise AssertionError("network forbidden")
        guarded_connection.__qualname__ = "install_network_guard.<locals>.create_connection"
        opener = Mock()
        opener.open.return_value = response
        opener.open.side_effect = error
        with ExitStack() as stack:
            stack.enter_context(patch("socket.create_connection", guarded_connection))
            stack.enter_context(patch("socket.socket.connect", side_effect=AssertionError("network forbidden")))
            stack.enter_context(patch("tools.upstream_metadata_probe.time.monotonic", return_value=100.0))
            stack.enter_context(patch("tools.upstream_metadata_probe.signal.getitimer", return_value=(0, 0)))
            alarm = stack.enter_context(patch("tools.upstream_metadata_probe.signal.signal", return_value="previous"))
            timer = stack.enter_context(patch("tools.upstream_metadata_probe.signal.setitimer"))
            build = stack.enter_context(patch("urllib.request.build_opener", return_value=opener))
            if during_open:
                opener.open.side_effect = lambda *args, **kwargs: during_open(alarm)
            result = probe_in_guarded_child(self.url, deadline_monotonic=deadline)
        return result, opener, build, alarm, timer

    def test_success_exact_range_no_proxy_and_restored_alarm(self):
        response = self.response()
        result, opener, build, alarm, timer = self.run_probe(response)
        self.assertTrue(result["complete_moov"])
        opener.open.assert_called_once()
        request = opener.open.call_args.args[0]
        self.assertEqual(request.get_header("Range"), f"bytes=0-{MAX_BYTES - 1}")
        self.assertEqual(request.get_header("Accept-encoding"), "identity")
        self.assertEqual(opener.open.call_args.kwargs["timeout"], 5.0)
        self.assertEqual(build.call_args.args[0].proxies, {})
        self.assertIsNone(build.call_args.args[1].redirect_request(None, None, 302, "", {}, self.url))
        response.read.assert_called_once_with(len(movie(b"vide", b"soun")))
        response.__exit__.assert_called_once()
        self.assertEqual(timer.call_args_list[0].args[1], 5.0)
        self.assertEqual(timer.call_args_list[-1].args[1], 0)
        self.assertEqual(alarm.call_args_list[-1].args[1], "previous")

    def test_bad_status_range_encoding_and_final_url_never_read_body(self):
        cases = [dict(status=200), dict(status=302),
                 dict(headers={"Content-Range": ""}),
                 dict(headers={"Content-Range": "bytes 1-20/21"}),
                 dict(headers={"Content-Range": f"bytes 0-{MAX_BYTES}/{MAX_BYTES + 1}"}),
                 dict(headers={"Content-Encoding": "gzip"}),
                 dict(url="https://other.example.org/a.mp4")]
        for case in cases:
            with self.subTest(case=case):
                response = self.response(**case)
                result, opener, *_ = self.run_probe(response)
                self.assertEqual(result["reason"], "range_rejected")
                self.assertEqual(result["bytes_read"], 0)
                self.assertEqual(result["audio"], "unknown")
                response.read.assert_not_called()
                response.__exit__.assert_called_once()
                opener.open.assert_called_once()

    def test_http_redirect_error_closed_without_retry(self):
        error = HTTPError(self.url, 302, "redirect", {}, None)
        with patch.object(error, "close") as close:
            result, opener, *_ = self.run_probe(error=error)
        self.assertEqual(result["reason"], "range_rejected")
        close.assert_called_once()
        opener.open.assert_called_once()

    def test_short_body_does_not_prove_tracks(self):
        response = self.response()
        response.read.return_value = movie(b"vide", b"soun")[:-1]
        result, *_ = self.run_probe(response)
        self.assertEqual(result["reason"], "range_rejected")
        self.assertFalse(result["complete_moov"])

    def test_hard_deadline_interrupt_restores_timer_without_retry(self):
        def interrupt(alarm):
            handler = alarm.call_args_list[0].args[1]
            handler(None, None)
        result, opener, _, alarm, timer = self.run_probe(during_open=interrupt, deadline=101.5)
        self.assertEqual(result["reason"], "deadline")
        self.assertFalse(result["complete_moov"])
        opener.open.assert_called_once()
        self.assertEqual(timer.call_args_list[0].args[1], 1.5)
        self.assertEqual(timer.call_args_list[-1].args[1], 0)
        self.assertEqual(alarm.call_args_list[-1].args[1], "previous")

    def test_expired_deadline_never_opens_connection(self):
        result, opener, _, _, timer = self.run_probe(deadline=99.0)
        self.assertEqual(result["reason"], "deadline")
        opener.open.assert_not_called()
        timer.assert_not_called()


class HTTPResponseReaderTests(unittest.TestCase):
    url = GuardedReaderTests.url
    run_probe = GuardedReaderTests.run_probe

    def response(self, interval, body, *, status=206, content_length=None):
        reads = []

        class FixtureSocket:
            def makefile(self, mode):
                return io.BytesIO(wire)

        class Response(http.client.HTTPResponse):
            def read(self, amount=None):
                data = super().read(amount)
                reads.append((amount, len(data)))
                return data

            def geturl(self):
                return HTTPResponseReaderTests.url

        length = str(len(body)) if content_length is None else content_length
        wire = (f"HTTP/1.1 {status} Fixture\r\nContent-Range: bytes {interval}\r\n"
                f"Content-Length: {length}\r\n\r\n").encode() + body
        response = Response(FixtureSocket())
        response.begin()
        return response, reads

    def test_reversed_second_range_rejected_before_stdlib_can_read_all(self):
        moov = movie(b"vide", b"soun")
        total = 2 * MAX_BYTES + len(moov)
        first, first_reads = self.response(f"0-{MAX_BYTES - 1}/{total}",
            (2 * MAX_BYTES).to_bytes(4, "big") + b"mdat" + b"x" * (MAX_BYTES - 8))
        second, second_reads = self.response("262144-0/1", b"x" * (1024 * 1024))
        result, opener, *_ = self.run_probe(error=[first, second])
        self.assertEqual(result["reason"], "range_rejected")
        self.assertEqual(first_reads, [(MAX_BYTES, MAX_BYTES)])
        self.assertEqual(second_reads, [])
        self.assertEqual(result["bytes_read"], MAX_BYTES)
        self.assertEqual(opener.open.call_count, 2)

    def test_invalid_headers_rejected_without_reading_body(self):
        cases = [("0-0/0", None, 206), ("1-0/2", None, 206),
                 ("-1-1/2", None, 206), ("0-2/2", None, 206),
                 (f"0-{MAX_BYTES}/{MAX_BYTES + 1}", None, 206),
                 ("0-0/" + "9" * 100, None, 206),
                 ("0-7/8", "-1", 206), ("0-7/8", "invalid", 206),
                 ("0-7/8", str(1024 * 1024), 206), ("0-7/8", None, 200)]
        for interval, length, status in cases:
            with self.subTest(interval=interval, length=length, status=status):
                response, reads = self.response(interval, b"x" * 8,
                                                status=status, content_length=length)
                result, *_ = self.run_probe(response)
                self.assertEqual(result["reason"], "range_rejected")
                self.assertEqual(result["bytes_read"], 0)
                self.assertEqual(reads, [])

    def test_second_response_cannot_exceed_range_or_change_total(self):
        for interval in ("131072-262144/262145", "131072-131175/131176"):
            with self.subTest(interval=interval):
                first, reads = self.response(f"0-{MAX_BYTES - 1}/{2 * MAX_BYTES}",
                    box(b"mdat", b"x" * (MAX_BYTES - 8)))
                second, second_reads = self.response(interval, b"x" * (MAX_BYTES + 1))
                result, *_ = self.run_probe(error=[first, second])
                self.assertEqual(result["reason"], "range_rejected")
                self.assertEqual(second_reads, [])
                self.assertEqual(reads, [(MAX_BYTES, MAX_BYTES)])

    def test_valid_two_ranges_use_exact_cumulative_budget_and_short_body_is_rejected(self):
        moov = movie(b"vide", b"soun")
        for truncated in (False, True):
            with self.subTest(truncated=truncated):
                first, first_reads = self.response(f"0-{MAX_BYTES - 1}/{2 * MAX_BYTES}",
                    box(b"mdat", b"x" * (MAX_BYTES - 8)))
                tail = moov + box(b"free", b"x" * (MAX_BYTES - len(moov) - 8))
                second, second_reads = self.response(f"{MAX_BYTES}-{2 * MAX_BYTES - 1}/{2 * MAX_BYTES}",
                    tail[:-1] if truncated else tail, content_length=str(MAX_BYTES))
                result, *_ = self.run_probe(error=[first, second])
                self.assertEqual(result["reason"], "range_rejected" if truncated else "complete_track_table")
                reads = first_reads + second_reads
                self.assertTrue(all(0 < amount <= MAX_BYTES for amount, _ in reads))
                self.assertEqual(result["bytes_read"], 2 * MAX_BYTES - int(truncated))
                self.assertLessEqual(sum(size for _, size in reads), 256 * 1024)


if __name__ == "__main__":
    unittest.main()
