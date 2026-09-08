"""Actual fixed registry and result classes, strictly in a child interpreter."""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]


class InstalledParseHubCompatibilityTests(unittest.TestCase):
    def test_worker_import_does_not_probe_system_gmp(self):
        import json
        python = os.environ.get("PARSEHUB_COMPAT_PYTHON", "")
        self.assertTrue(python and Path(python).is_file())
        script = f'''import ctypes.util
import sys
sys.path.insert(0, {str(ROOT)!r})
from tools import upstream_parsehub_worker as worker
def denied_lookup(name):
    raise AssertionError("system_library_probe")
ctypes.util.find_library = denied_lookup
worker.main()
assert "Cryptodome.Math._IntegerGMP" not in sys.modules
'''
        done = subprocess.run(
            [python, "-I", "-B", "-c", script], input='{"action":"catalog"}',
            cwd=ROOT, capture_output=True, text=True, timeout=20,
        )
        self.assertEqual(done.returncode, 0)
        data = json.loads(done.stdout)
        self.assertTrue(data.get("ok"), {key: data.get(key) for key in ("error", "stage", "error_class")})
        self.assertEqual(len(data["payload"]["entries"]), 20)

    def test_quality_execution_uses_real_parsehub_wire_contract(self):
        import tempfile
        from unittest.mock import patch
        from django.test import override_settings
        from xhs_download.services import upstream_parsehub as bridge
        from xhs_download.services.quality_execution import _execute_upstream
        python = os.environ.get("PARSEHUB_COMPAT_PYTHON", "")
        self.assertTrue(python and Path(python).is_file())
        before = {k for k in sys.modules if k == "parsehub" or k.startswith("parsehub.")}
        real_call = bridge._call

        def diagnosed_call(request, timeout):
            try:
                return real_call(request, timeout)
            except bridge.ParseHubBridgeError as error:
                # Quality execution intentionally collapses these safe worker details.
                self.fail(
                    f"ParseHub wire contract: action={request['action']} "
                    f"code={error.code} stage={error.stage} error_class={error.error_class}"
                )

        script = f'''import sys
sys.path.insert(0, {str(ROOT)!r})
from tools import upstream_parsehub_worker as worker
from parsehub.types import RichTextParseResult, ImageRef
async def offline(self, url):
    return RichTextParseResult(markdown_content="offline quality fixture", media=[ImageRef(url="http://media.example/image.jpg", width=2160)])
worker.import_parser("WXParser")._do_parse = offline
worker.main()
'''
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "offline_worker.py"
            path.write_text(script)
            with override_settings(PARSEHUB_UPSTREAM_ENABLED=True, PARSEHUB_UPSTREAM_PYTHON=python), patch.object(bridge, "WORKER", path), patch.object(bridge, "_call", side_effect=diagnosed_call):
                rows, match, _ = _execute_upstream("parsehub_generic", "https://mp.weixin.qq.com/s/offline-fixture", 20)
        self.assertEqual(match["extractor_id"], "WXParser")
        self.assertEqual(rows[0][1], "http://media.example/image.jpg")
        self.assertEqual(rows[0][2]["width"], 2160)
        self.assertIn("insecure_transport", rows[0][2]["operation_restrictions"])
        self.assertEqual(before, {k for k in sys.modules if k == "parsehub" or k.startswith("parsehub.")})

    def test_real_types_in_isolated_runtime(self):
        python = os.environ.get("PARSEHUB_COMPAT_PYTHON")
        self.assertTrue(python and Path(python).is_file(), "Set PARSEHUB_COMPAT_PYTHON to the isolated runtime")
        before = {k for k in sys.modules if k == "parsehub" or k.startswith("parsehub.")}
        done = subprocess.run([python, "-I", "-B", str(Path(__file__).resolve()), "--isolated"],
                              cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stdout + done.stderr)
        self.assertIn("REGISTRY_CHECKS=400", done.stdout)
        self.assertEqual(before, {k for k in sys.modules if k == "parsehub" or k.startswith("parsehub.")})


def isolated_suite():
    import asyncio
    import json
    from types import SimpleNamespace
    from unittest.mock import patch
    sys.path.insert(0, str(ROOT))
    from tools import upstream_parsehub_worker as worker
    worker.verify_install()
    classes = worker.registry()
    import httpx
    from parsehub.parsers.base import BaseParser
    from parsehub.types.media_ref import AniRef, ImageRef, LivePhotoRef, MediaRef, VideoRef
    from parsehub.types.result import ParseResult, ImageParseResult, RichTextParseResult, MultimediaParseResult
    source = "https://example.com/post/123"
    image = "https://media.example/image.jpg"
    video = "https://media.example/video.mp4"

    class RealContracts(unittest.TestCase):
        def normalize(self, result):
            result.raw_url = source
            return worker.normalize_result(result, source)

        def test_actual_registry_hundreds_of_offline_matches(self):
            # Synthetic single-content shapes follow the installed __match__ rules.
            # Counts are input checks, not live successes or independent websites.
            seeds = {
                "BiliParse": "https://www.bilibili.com/video/BV1xx411c7mD",
                "CoolapkParser": "https://www.coolapk.com/feed/123",
                "DoubanParser": "https://www.douban.com/group/topic/123",
                "DouyinParser": "https://www.douyin.com/video/123",
                "FacebookParse": "https://www.facebook.com/reel/123",
                "InstagramParser": "https://www.instagram.com/p/abc123/",
                "ThreadsParser": "https://www.threads.com/@example/post/abc123",
                "TieBaParser": "https://tieba.baidu.com/p/123",
                "TwitterParser": "https://x.com/example/status/123",
                "WeiboParser": "https://weibo.com/123/Abc123456",
                "WXParser": "https://mp.weixin.qq.com/s/example",
                "XHSParser": "https://www.xiaohongshu.com/explore/123",
                "XiaoHeiHeParser": "https://www.xiaoheihe.cn/app/bbs/link/123",
                "YtbParse": "https://www.youtube.com/watch?v=abcdefghijk",
                "ZuiYouParser": "https://share.xiaochuankeji.cn/hybrid/share/post?pid=123",
                "KuaiShouParser": "https://www.kuaishou.com/short-video/abc123",
                "PipixParser": "https://h5.pipix.com/ppx/item/123",
                "Snapchatarse": "https://www.snapchat.com/@example/spotlight/abc123",
                "TikTokParser": "https://www.tiktok.com/@example/video/123",
                "ZhihuParser": "https://www.zhihu.com/question/123/answer/456",
            }
            self.assertEqual({c.__name__ for c in classes}, set(seeds))
            count = 0
            with patch("socket.socket.connect", side_effect=AssertionError("offline")):
                for name, seed in seeds.items():
                    for index in range(20):
                        url = seed + ("&" if "?" in seed else "?") + f"upstream_parameter={index}"
                        with self.subTest(parser=name, case=index):
                            actual = worker.match_url(url)
                            expected = tuple(worker.entry(c) for c in classes if c.match(url))
                            self.assertEqual(actual, expected)
                            self.assertIn(name, [m["extractor_id"] for m in actual])
                        count += 1
            print(f"REGISTRY_CHECKS={count}; REGISTRY_ENTRIES={len(classes)}; LIVE_PARSES=0")

        def test_registered_unknown_subclasses_and_livephoto_order(self):
            result = ParseResult(media=[MediaRef(url=image), LivePhotoRef(url=image, video_url=video), AniRef(url=video)])
            rows = self.normalize(result)
            self.assertEqual([r[1] for r in rows], [image, image, video, video])
            self.assertEqual(rows[0][2]["media_type"], "unknown")
            self.assertEqual(rows[1][2]["relationship_id"], rows[2][2]["relationship_id"])
            self.assertEqual(rows[3][2]["media_type"], "video")
            for fake in [object(), SimpleNamespace(media=[MediaRef(url=image)])]:
                with self.assertRaisesRegex(ValueError, "unsupported_result"):
                    worker.normalize_result(fake, source)

        def test_http_hls_dash_audio_missing_codec_all_retained(self):
            formats = [
                {"url": "http://media.example/a.m3u8", "ext": "mp4", "protocol": "m3u8_native", "height": 2160},
                {"url": "https://media.example/a.mpd", "ext": "mp4", "vcodec": "avc1", "acodec": "none"},
                {"url": "https://media.example/a.m4a", "ext": "m4a", "vcodec": "none", "acodec": "aac"},
                {"url": video, "vcodec": "avc1", "acodec": "aac", "filesize_approx": 1234},
                {"url": "https://media.example/unknown", "vcodec": "avc1"},
            ]
            result = ParseResult(media=VideoRef(url=source))
            result.dl = SimpleNamespace(info_json={"id": "123", "formats": formats})
            rows = self.normalize(result)
            self.assertEqual([r[1] for r in rows], [f["url"] for f in formats])
            self.assertEqual([r[2]["candidate_kind"] for r in rows], ["hls", "dash", "audio_only", "complete", "unknown"])
            self.assertIn("insecure_transport", rows[0][2]["operation_restrictions"])
            self.assertNotIn("acodec", rows[-1][2])
            self.assertEqual(rows[3][2]["filesize_approx"], 1234)

        def test_conflicts_are_restricted_not_removed(self):
            rows = self.normalize(ParseResult(media=[VideoRef(url=image), ImageRef(url=video)]))
            self.assertEqual(len(rows), 2)
            self.assertTrue(all("media_type_conflict" in r[2]["operation_restrictions"] for r in rows))

        def test_summary_has_no_html_or_recursive_urls(self):
            value = RichTextParseResult(markdown_content="hello <script>secret()</script> ![](https://media.example/hidden.jpg)")
            rows = self.normalize(value)
            self.assertEqual(rows[0][0], 2)
            self.assertNotIn("secret()", rows[0][1]["content_summary"])
            self.assertNotIn("https://", rows[0][1]["content_summary"])
            self.assertNotIn("<script", json.dumps(rows))

        def test_limits_keep_prefix_and_validate_unsafe_tail(self):
            photos = [ImageRef(url=image, width=index + 1) for index in range(worker.MAX_ASSETS + 1)]
            rows = self.normalize(ImageParseResult(photo=photos))
            self.assertEqual(len(rows), worker.MAX_ASSETS)
            self.assertEqual(rows[-1][2]['width'], worker.MAX_ASSETS)
            self.assertEqual(rows[0][2]["candidate_total"], worker.MAX_ASSETS + 1)
            self.assertEqual(rows[0][2]['upstream_scope']['retained_count'], worker.MAX_ASSETS)
            self.assertEqual(rows[0][2]['upstream_scope']['unexpanded_count'], 1)
            for url in ["https://127.0.0.1/a", "file:///etc/passwd", "https://user:password@example.com/a"]:
                with self.assertRaises(ValueError):
                    self.normalize(ImageParseResult(photo=[image] * worker.MAX_ASSETS + [url]))
            with self.assertRaisesRegex(ValueError, "no_formats"):
                self.normalize(ParseResult())

        def test_http_redirect_guard_and_credentials_every_hop(self):
            async def run(target):
                seen = []
                def respond(request):
                    seen.append(request)
                    if request.url.path == "/first":
                        return httpx.Response(302, headers={"location": target, "set-cookie": "session=secret"})
                    return httpx.Response(200, text="ok")
                budget = worker.Budget(5)
                with worker.execution_boundary(budget, source, "Example"):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True,
                                                cookies={"embedded": "secret"}) as client:
                        await client.get("https://example.com/first", headers={"Authorization": "secret"})
                self.assertEqual(budget.requests, 2)
                self.assertTrue(all("cookie" not in r.headers and "authorization" not in r.headers for r in seen))
            asyncio.run(run("https://another.example/second"))
            with self.assertRaises(ValueError):
                asyncio.run(run("https://127.0.0.1/private"))

        def test_response_request_and_redirect_budgets(self):
            async def run(mode):
                def respond(request):
                    return httpx.Response(302, headers={"location": "/loop"}) if mode == "redirect" else httpx.Response(200, content=b"12345")
                budget = worker.Budget(5)
                with worker.execution_boundary(budget, source, "Example"):
                    async with httpx.AsyncClient(transport=httpx.MockTransport(respond), follow_redirects=True) as client:
                        await client.get(source)
            with patch.object(worker, "MAX_NETWORK_BYTES", 4), self.assertRaisesRegex(ValueError, "output_limit"):
                asyncio.run(run("bytes"))
            with patch.object(worker, "MAX_REDIRECTS", 1), self.assertRaises((ValueError, httpx.TooManyRedirects)):
                asyncio.run(run("redirect"))
            budget = worker.Budget(5)
            for _ in range(worker.MAX_REQUESTS):
                budget.request(source)
            with self.assertRaisesRegex(ValueError, "request_limit"):
                budget.request(source)

        def test_normal_delegation_allowed_loop_denied(self):
            class Inner(BaseParser, register=False):
                async def _do_parse(self, url):
                    return ImageParseResult(photo=[image])
            class Outer(BaseParser, register=False):
                async def _do_parse(self, url):
                    return await Inner().parse(url)
            class Loop(BaseParser, register=False):
                async def _do_parse(self, url):
                    return await self.parse(url)
            async def run(cls):
                with worker.execution_boundary(worker.Budget(5), source, "Example"):
                    return await cls().parse(source)
            self.assertIsInstance(asyncio.run(run(Outer)), ImageParseResult)
            with self.assertRaisesRegex(ValueError, "delegation_limit"):
                asyncio.run(run(Loop))

        def test_stdlib_url_and_read_boundary(self):
            import http.client
            with worker.execution_boundary(worker.Budget(5), source, "Example"):
                with self.assertRaises(ValueError):
                    http.client.HTTPConnection("127.0.0.1").putrequest("GET", "/")
            budget = worker.Budget(5)
            budget.deadline = 0
            with self.assertRaisesRegex(ValueError, "network_timeout"):
                budget.request(source)

        def test_bytes_and_string_credential_headers_are_removed(self):
            import http.client
            with patch.object(http.client.HTTPConnection, "putheader") as sent:
                with worker.execution_boundary(worker.Budget(5), source, "Example"):
                    conn = http.client.HTTPConnection("example.com")
                    for header in ["Cookie", b"Cookie", "Authorization", b"AUTHORIZATION",
                                   "Proxy-Authorization", b"Proxy-Authorization"]:
                        conn.putheader(header, "synthetic-secret")
                    conn.putheader(b"Accept", "application/json")
            sent.assert_called_once_with(conn, b"Accept", "application/json")

        def test_embedded_ytdlp_removes_foreign_handlers_before_extracting(self):
            from parsehub.parsers.base.ytdlp import YtParser
            from yt_dlp import YoutubeDL
            foreign = SimpleNamespace(closed=False)
            def close():
                foreign.closed = True
            foreign.close = close
            enter = YoutubeDL.__enter__
            seen = []
            def with_foreign(ydl):
                result = enter(ydl)
                ydl._request_director.handlers["CurlCFFI"] = foreign
                return result
            def extract(ydl, url, download=False):
                seen.append(set(ydl._request_director.handlers))
                self.assertTrue(foreign.closed)
                return {"id": "fixture"}
            async def run():
                with worker.execution_boundary(worker.Budget(5), source, "Example"):
                    return await YtParser()._extract_info(source)
            with patch.object(YoutubeDL, "__enter__", with_foreign), patch.object(YoutubeDL, "extract_info", extract):
                self.assertEqual(asyncio.run(run()), {"id": "fixture"})
            self.assertEqual(seen, [{"Urllib"}])
            with self.assertRaisesRegex(ValueError, "network_denied"):
                worker.restrict_ytdlp_transport(SimpleNamespace(_request_director=SimpleNamespace(handlers={})))

        def test_decoded_body_budgets_for_urllib3_ytdlp_and_httpx(self):
            import gzip
            import io
            import http.client
            import urllib3.response
            from yt_dlp.networking._urllib import HTTPHandler
            bomb = gzip.compress(b"x" * 1000000)
            class Socket:
                def makefile(self, *args):
                    return io.BytesIO(b"HTTP/1.1 200 OK\r\nContent-Length: " + str(len(bomb)).encode()
                                      + b"\r\nContent-Encoding: gzip\r\n\r\n" + bomb)
            with patch.object(worker, "MAX_NETWORK_BYTES", 2048):
                for mode in ("urllib3", "ytdlp", "httpx"):
                    with self.subTest(mode=mode), worker.execution_boundary(worker.Budget(5), source, "Example"):
                        with self.assertRaisesRegex(ValueError, "output_limit"):
                            if mode == "urllib3":
                                raw = http.client.HTTPResponse(Socket())
                                raw.begin()
                                urllib3.response.HTTPResponse(body=raw, headers={"content-encoding": "gzip"}, preload_content=False).data
                            elif mode == "ytdlp":
                                HTTPHandler.gz(bomb)
                            else:
                                httpx.Response(200, content=bomb, headers={"content-encoding": "gzip"})
            for encoding in ("gzip", "deflate", "br"):
                import zlib
                import brotli
                text = b"bounded public metadata"
                encoded = {"gzip": gzip.compress, "deflate": zlib.compress, "br": brotli.compress}[encoding](text)
                with worker.execution_boundary(worker.Budget(5), source, "Example"):
                    self.assertEqual(httpx.Response(200, content=encoded, headers={"content-encoding": encoding}).content, text)

    return unittest.defaultTestLoader.loadTestsFromTestCase(RealContracts)


if __name__ == "__main__":
    result = unittest.TextTestRunner(verbosity=2).run(isolated_suite())
    raise SystemExit(not result.wasSuccessful())
