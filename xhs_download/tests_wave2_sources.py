"""Auto-discovered offline contracts using the separate pinned ParseHub runtime.

CI must bootstrap that runtime and set PARSEHUB_COMPAT_PYTHON; missing runtimes
fail rather than silently skipping these contracts.
"""

import asyncio
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parents[1]
ISOLATED = __name__ == "__main__"
if ISOLATED:
    sys.path.insert(0, str(ROOT))

from tools import upstream_parsehub_worker as worker


if not ISOLATED:
    class Wave2SourceRuntimeTests(unittest.TestCase):
        def test_offline_source_contracts_in_isolated_runtime(self):
            python = os.environ.get("PARSEHUB_COMPAT_PYTHON", "")
            self.assertTrue(python, "Set PARSEHUB_COMPAT_PYTHON to the isolated ParseHub CI runtime")
            self.assertTrue(Path(python).is_file(), "Bootstrap the isolated ParseHub CI runtime")
            completed = subprocess.run(
                [python, "-I", "-B", str(Path(__file__).resolve())],
                cwd=ROOT, env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"},
                capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertIn("Ran 13 tests", completed.stderr)


class Wave2SourceTests(unittest.TestCase if ISOLATED else object):
    def test_catalog_is_exactly_five(self):
        self.assertEqual(set(worker.PARSERS), {
            "TieBaParser", "PipixParser", "ZuiYouParser", "DoubanParser", "CoolapkParser",
        })

    def test_coolapk_direct_sources_and_tracking(self):
        for host in ("coolapk.com", "www.coolapk.com"):
            for path in ("feed", "picture"):
                for suffix in ("", "/", "?shareKey=a%2Bb&s=token&shareFrom=app&shareUid=1&shareType=1&utm_source=app"):
                    url = f"https://{host}/{path}/123{suffix}"
                    with self.subTest(url=url):
                        self.assertEqual(worker.source_identity(url), ("CoolapkParser", "123"))
                        self.assertEqual(worker.match_url(url)[0]["extractor_id"], "CoolapkParser")

    def test_douban_direct_sources_and_tracking(self):
        for host in ("douban.com", "www.douban.com", "m.douban.com"):
            for path in ("topic", "group/topic"):
                for suffix in ("", "/", "?_i=abc&utm_source=app&utm_medium=share&utm_campaign=test&from=group"):
                    url = f"https://{host}/{path}/123{suffix}"
                    with self.subTest(url=url):
                        self.assertEqual(worker.source_identity(url), ("DoubanParser", "123"))

    def test_douban_tracking_only_does_not_supply_or_replace_identity(self):
        base = "https://www.douban.com/group/topic/123/"
        tracking = "_i=456&utm_source=app&utm_medium=share&utm_campaign=789&from=group"
        self.assertEqual(worker.source_identity(base + "?" + tracking), worker.source_identity(base))
        self.assertEqual(worker.match_url(base + "?" + tracking), worker.match_url(base))
        for query in ("id=456", "topic_id=456", "topic=456", "group_id=456", "url=other"):
            with self.subTest(query=query), self.assertRaises(ValueError):
                worker.source_identity(base + "?" + tracking + "&" + query)
        with self.assertRaises(ValueError):
            worker.source_identity("https://www.douban.com/group/topic/?" + tracking)

    def test_unknown_queries_and_non_single_content_are_rejected(self):
        urls = [
            "https://www.coolapk.com/feed/123?unknown=1",
            "https://www.coolapk.com/feed/123?shareKey=a&shareKey=b",
            "https://www.coolapk.com/feed/123?s=a&s=b",
            "https://www.douban.com/group/topic/123?unknown=1",
            "https://www.douban.com/group/topic/123?shareKey=a",
            "https://www.coolapk.com/u/123", "https://www.douban.com/group/123",
            "https://douc.cc/abc", "https://mp.weixin.qq.com/s/opaque",
        ]
        for base in ("https://www.coolapk.com/feed/", "https://www.douban.com/group/topic/"):
            urls.extend(base + value for value in ("0", "0123", "abc", "-1", "1/extra", "1#fragment"))
            urls.extend((base.replace("https:", "http:") + "123",
                         base.replace(".com/", ".com.evil.example/") + "123"))
        for url in urls:
            with self.subTest(url=url):
                with self.assertRaises(ValueError):
                    worker.source_identity(url)
                self.assertEqual(worker.match_url(url), ())

    def test_original_three_sources_keep_their_identities(self):
        for url, identity in (
            ("https://tieba.baidu.com/p/123", ("TieBaParser", "123")),
            ("https://h5.pipix.com/ppx/item/123", ("PipixParser", "123")),
            ("https://share.xiaochuankeji.cn/hybrid/share/post?pid=123", ("ZuiYouParser", "123")),
        ):
            self.assertEqual(worker.source_identity(url), identity)

    def test_coolapk_exact_script_intent_and_feed_detail_required(self):
        for path in ("feed", "picture"):
            intent = f"intent://www.coolapk.com/{path}/123#Intent;"
            worker.verify_response_identity("CoolapkParser", "123", SimpleNamespace(
                text=f'<div id="feed-detail"></div><script>var intent = "{intent}";</script>'))
        invalid = (
            '<div id="feed-detail"></div>',
            '<script>var intent = "intent://www.coolapk.com/feed/123#Intent;";</script>',
        )
        invalid += tuple(
            f'<div id="feed-detail"></div><script>var intent = "{intent}";</script>'
            for intent in (
                'intent://www.coolapk.com/feed/124#Intent;',
                'intent://www.coolapk.com/feed/1234#Intent;',
                'intent://evil.example/feed/123#Intent;',
                'intent://www.coolapk.com/feed/123?x=1#Intent;',
            )
        )
        for html in invalid:
            with self.subTest(html=html), self.assertRaisesRegex(ValueError, "identity_mismatch"):
                worker.verify_response_identity("CoolapkParser", "123", SimpleNamespace(text=html))

    def test_coolapk_arbitrary_text_and_attributes_are_not_identity_evidence(self):
        intent = "intent://www.coolapk.com/feed/123#Intent;"
        for fragment in (intent, f'<p>{intent}</p>', f'<a href="{intent}">open</a>',
                         f'<!-- {intent} -->', f'<div data-intent="{intent}"></div>'):
            with self.subTest(fragment=fragment), self.assertRaisesRegex(ValueError, "identity_mismatch"):
                worker.verify_response_identity("CoolapkParser", "123", SimpleNamespace(
                    text=f'<div id="feed-detail"></div>{fragment}'))

    def test_coolapk_requires_one_detail_and_one_distinct_script_identity(self):
        detail = '<div id="feed-detail"></div>'
        script = '<script>var intent = "intent://www.coolapk.com/feed/123#Intent;";</script>'
        other = '<script>var intent = "intent://www.coolapk.com/feed/124#Intent;";</script>'
        for html in (detail + detail + script, detail + '<section id="feed-detail"></section>' + script,
                     detail + script + other, detail + script.replace('</script>',
                         'var other = "intent://www.coolapk.com/picture/124#Intent;";</script>')):
            with self.subTest(html=html), self.assertRaisesRegex(ValueError, "identity_mismatch"):
                worker.verify_response_identity("CoolapkParser", "123", SimpleNamespace(text=html))
        # Repeated same-ID script references are unambiguous; body text is not evidence.
        worker.verify_response_identity("CoolapkParser", "123", SimpleNamespace(
            text=detail + script + script + '<p>intent://www.coolapk.com/feed/124#Intent;</p>'))

    def test_douban_response_id_is_authoritative(self):
        import httpx
        for value in (123, "123"):
            worker.verify_response_identity("DoubanParser", "123", httpx.Response(200, json={"id": value}))
        for payload in ({}, {"id": 124}, {"id": None}, {"id": "0123"}, {"topic": {"id": 123}}):
            with self.subTest(payload=payload), self.assertRaisesRegex(ValueError, "identity_mismatch"):
                worker.verify_response_identity("DoubanParser", "123", httpx.Response(200, json=payload))

    def test_coolapk_parser_receives_access_parameters_without_tracking(self):
        import httpx
        captured = []

        class StopAtParser(Exception):
            pass

        class Parser:
            def __init__(self, **kwargs):
                pass

            async def parse(self, source):
                captured.append(source)
                raise StopAtParser()

        original = httpx.AsyncClient
        for path in ("feed", "picture"):
            with self.assertRaises(StopAtParser):
                asyncio.run(worker.parse(
                    f"https://coolapk.com/{path}/123?shareKey=a%2Bb&s=x%2Fy&shareFrom=app&utm_source=app", 5, Parser))
            parsed = urlsplit(captured[-1])
            self.assertEqual(parsed.netloc, "www.coolapk.com")
            self.assertEqual(parsed.path, f"/{path}/123")
            self.assertEqual(parse_qs(parsed.query), {"shareKey": ["a+b"], "s": ["x/y"]})
            self.assertIs(httpx.AsyncClient, original)
        with self.assertRaises(StopAtParser):
            asyncio.run(worker.parse("https://www.coolapk.com/feed/123?shareKey=a%2Bb&s=", 5, Parser))
        self.assertEqual(urlsplit(captured[-1]).query, "shareKey=a%2Bb")

    def test_probe_has_single_candidate_and_original_deadline(self):
        import httpx

        class Parser:
            def __init__(self, **kwargs):
                pass

            async def parse(self, source):
                return object()

        rows = [[3, f"https://media.example/{index}.mp4", {"media_type": "video", "extension": "mp4"}]
                for index in range(2)]
        with mock.patch.object(worker, "bounded_http", return_value=(httpx.AsyncClient, httpx.AsyncClient, {"identity": True})), \
                mock.patch.object(worker, "normalize_result", return_value=rows), \
                mock.patch.object(worker.time, "monotonic", return_value=100), \
                mock.patch("tools.upstream_metadata_probe.probe_in_guarded_child", return_value={"proof": True}) as probe:
            result = asyncio.run(worker.parse("https://h5.pipix.com/ppx/item/123", 5, Parser))
        probe.assert_called_once_with("https://media.example/0.mp4", deadline_monotonic=105)
        self.assertEqual(result[0][2]["upstream_bmff_track_evidence"], {"proof": True})
        self.assertNotIn("upstream_bmff_track_evidence", result[1][2])

    def test_coolapk_media_must_match_body_scope_and_order(self):
        import httpx

        class Parser:
            def __init__(self, **kwargs):
                pass

            async def parse(self, source):
                return object()

        urls = ["https://media.example/first.jpg", "https://media.example/second.jpg"]
        for received, accepted in [(urls, True), (urls[::-1], False),
                                   (["https://media.example/avatar.jpg"], False)]:
            rows = [[3, url, {"media_type": "image"}] for url in received]
            state = {"identity": True, "body_media": urls}
            with mock.patch.object(worker, "bounded_http", return_value=(httpx.AsyncClient, httpx.AsyncClient, state)), \
                    mock.patch.object(worker, "normalize_result", return_value=rows):
                if accepted:
                    self.assertEqual(asyncio.run(worker.parse("https://www.coolapk.com/feed/123", 5, Parser)), rows)
                else:
                    with self.assertRaisesRegex(ValueError, "identity_mismatch"):
                        asyncio.run(worker.parse("https://www.coolapk.com/feed/123", 5, Parser))


if __name__ == "__main__":
    worker.verify_install()
    unittest.main()
