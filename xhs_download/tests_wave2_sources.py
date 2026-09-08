"""Retained local identity exceptions, exercised against the readonly runtime."""
import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
ISOLATED = __name__ == "__main__"
if ISOLATED:
    sys.path.insert(0, str(ROOT))
from tools import upstream_parsehub_worker as worker


if not ISOLATED:
    class Wave2SourceRuntimeTests(unittest.TestCase):
        def test_offline_source_contracts_in_isolated_runtime(self):
            python = os.environ.get("PARSEHUB_COMPAT_PYTHON", "")
            self.assertTrue(python and Path(python).is_file(), "Set PARSEHUB_COMPAT_PYTHON")
            done = subprocess.run([python, "-I", "-B", str(Path(__file__).resolve())],
                                  cwd=ROOT, capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stdout + done.stderr)


class Wave2SourceTests(unittest.TestCase if ISOLATED else object):
    def test_identity_is_path_not_query_policy(self):
        for host, path, name in [("coolapk.com", "feed", "CoolapkParser"),
                                 ("www.coolapk.com", "picture", "CoolapkParser"),
                                 ("douban.com", "topic", "DoubanParser"),
                                 ("m.douban.com", "group/topic", "DoubanParser")]:
            url = f"https://{host}/{path}/123?new_upstream_parameter=abc&shareKey=a%2Bb"
            self.assertEqual(worker.source_identity(url), (name, "123"))
            self.assertIn(name, [m["extractor_id"] for m in worker.match_url(url)])

    def test_coolapk_requires_unique_detail_and_unambiguous_script_identity(self):
        detail = '<div id="feed-detail"></div>'
        script = '<script>var intent="intent://www.coolapk.com/feed/123#Intent;";</script>'
        worker.verify_response_identity("CoolapkParser", "123", SimpleNamespace(text=detail + script + script))
        for html in [detail, script, detail + detail + script, detail + script.replace("123", "124"),
                     detail + script + script.replace("123", "124"),
                     detail + '<a href="intent://www.coolapk.com/feed/123#Intent;">open</a>',
                     detail + '<!-- intent://www.coolapk.com/feed/123#Intent; -->']:
            with self.assertRaisesRegex(ValueError, "identity_mismatch"):
                worker.verify_response_identity("CoolapkParser", "123", SimpleNamespace(text=html))

    def test_douban_top_level_id_exact(self):
        import httpx
        for value in [123, "123"]:
            worker.verify_response_identity("DoubanParser", "123", httpx.Response(200, json={"id": value}))
        for data in [{}, {"id": "0123"}, {"id": 124}, {"topic": {"id": 123}}, None, []]:
            with self.assertRaisesRegex(ValueError, "identity_mismatch"):
                worker.verify_response_identity("DoubanParser", "123", httpx.Response(200, json=data))

    def test_native_coolapk_scoped_media_and_access_parameters(self):
        import httpx
        from parsehub.parsers.parser.coolapk import CoolapkParser
        html = '''<div id="feed-detail"><div class="feed-message">hello</div>
        <div class="message-image-group"><img src="//media.example/one.jpg"><img src="//media.example/two.jpg"></div></div>
        <script>var intent="intent://www.coolapk.com/feed/123#Intent;";</script>'''
        original = httpx.AsyncClient._send_single_request
        seen = []
        async def send(client, request):
            seen.append(str(request.url))
            return httpx.Response(200, text=html, request=request)
        with patch.object(httpx.AsyncClient, "_send_single_request", send):
            rows = asyncio.run(worker.parse("https://coolapk.com/feed/123?shareKey=a%2Bb&s=xyz&utm_source=test", 5, CoolapkParser))
        self.assertIn("shareKey=a%2Bb", seen[0])
        self.assertNotIn("utm_source", seen[0])
        self.assertEqual([r[1] for r in rows], ["https://media.example/one.jpg", "https://media.example/two.jpg"])
        self.assertTrue(all(r[2]["identity_evidence"] == "upstream_response_id" for r in rows))
        self.assertIs(httpx.AsyncClient._send_single_request, original)

    def test_douban_shortlink_keeps_response_identity_exception(self):
        import httpx
        from parsehub.parsers.parser.douban import DoubanParser
        async def run(actual_id):
            async def send(client, request):
                if request.url.host == "douc.cc":
                    return httpx.Response(302, headers={"location": "https://www.douban.com/group/topic/123/"}, request=request)
                if request.url.host == "www.douban.com":
                    return httpx.Response(200, text="public topic", request=request)
                return httpx.Response(200, json={"id": actual_id, "title": "topic", "content": "<p>text</p>", "photos": []}, request=request)
            with patch.object(httpx.AsyncClient, "_send_single_request", send):
                return await worker.parse("https://douc.cc/synthetic", 5, DoubanParser)
        rows = asyncio.run(run("123"))
        self.assertEqual(rows[0][0], 2)
        self.assertEqual(rows[0][1]["source_id"], "123")
        self.assertEqual(rows[0][1]["identity_evidence"], "upstream_response_id")
        with self.assertRaises(Exception) as caught:
            asyncio.run(run("124"))
        self.assertEqual(worker.failure_summary(caught.exception, "parse")["error"], "identity_mismatch")


if ISOLATED:
    worker.verify_install()
    unittest.main()
