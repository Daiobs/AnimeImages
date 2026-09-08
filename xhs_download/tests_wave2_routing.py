"""Offline routing contracts; exact URL admission remains worker-owned."""

from unittest import mock

from django.test import SimpleTestCase

from xhs_download.services.common import XhsParseError, extract_url_from_text
from xhs_download.services.platforms.third_party import GenericThirdPartyAdapter
from xhs_download.services.registry import PlatformRegistry


class Wave2RoutingTests(SimpleTestCase):
    candidates = (
        ("CoolapkParser", "https://www.coolapk.com/feed/123456"),
        ("CoolapkParser", "https://www.coolapk.com/picture/123456"),
        ("DoubanParser", "https://www.douban.com/group/topic/123456/"),
        ("WeiXinParser", "https://mp.weixin.qq.com/s/opaque_token"),
        ("WeiXinParser", "https://mp.weixin.qq.com/s?__biz=abc&mid=123&idx=1&sn=def"),
    )

    def setUp(self):
        self.adapter = GenericThirdPartyAdapter()
        self.registry = PlatformRegistry()

    def match(self, extractor):
        return {"provider_id": "parsehub_generic", "extractor_id": extractor,
                "label": extractor, "version": "synthetic"}

    def test_share_text_preserves_direct_url_and_reaches_exact_matcher(self):
        for extractor, url in self.candidates:
            for text in (url, f"Shared post: {url}", f"Shared post: ({url})",
                         f"\u5206\u4eab\uff1a{url}\u3002 \u67e5\u770b\u8be6\u60c5"):
                with self.subTest(text=text), mock.patch(
                    "xhs_download.services.upstream_matching.match_upstream_url",
                    return_value=(self.match(extractor),),
                ) as matcher:
                    self.assertEqual(extract_url_from_text(text), url)
                    adapter = self.registry.get_adapter_for_url(text)
                    self.assertIsInstance(adapter, GenericThirdPartyAdapter)
                    rule = adapter.get_rule_for_url(text)
                    self.assertEqual(rule.platform_id, "upstream")
                    self.assertEqual(rule.provider_ids, ("parsehub_generic",))
                    self.assertEqual(rule.upstream_matches, (self.match(extractor),))
                    self.assertTrue(all(call.args == (url,) for call in matcher.call_args_list))

    def test_six_families_are_not_claimed_by_old11_rules(self):
        for host in ("weibo.com", "www.coolapk.com", "www.xiaoheihe.cn",
                     "www.douban.com", "www.zhihu.com", "mp.weixin.qq.com"):
            with self.subTest(host=host):
                self.assertFalse(any(rule.supports_hostname(host)
                                     for rule in self.adapter.platform_rules))

    def test_unmatched_candidates_do_not_become_domain_only_routes(self):
        with mock.patch("xhs_download.services.upstream_matching.match_upstream_url",
                        return_value=()):
            for _, url in self.candidates:
                with self.subTest(url=url):
                    self.assertIsNone(self.adapter.get_rule_for_url(url))
            self.assertIsNone(self.adapter.get_rule_for_url("https://douc.cc/example"))

    def test_all_old11_rules_retain_priority_without_upstream_probe(self):
        with mock.patch("xhs_download.services.upstream_matching.match_upstream_url") as matcher:
            self.assertEqual(len(self.adapter.platform_rules), 11)
            for rule in self.adapter.platform_rules:
                for path in ("/", "/unsupported-path", "/post/123"):
                    url = "https://" + rule.host_suffixes[0] + path
                    with self.subTest(url=url):
                        self.assertIs(self.adapter.get_rule_for_url(url), rule)
            matcher.assert_not_called()

    def test_mature_adapters_retain_priority(self):
        with mock.patch("xhs_download.services.upstream_matching.match_upstream_url") as matcher:
            for platform, url in (
                ("xiaohongshu", "https://www.xiaohongshu.com/explore/123"),
                ("douyin", "https://www.douyin.com/video/123"),
                ("bilibili", "https://www.bilibili.com/video/BV123"),
            ):
                with self.subTest(platform=platform):
                    self.assertEqual(self.registry.get_adapter_for_url(url).platform_id, platform)
                    self.assertIsNone(self.adapter.get_rule_for_url(url))
            matcher.assert_not_called()

    def test_upstream_failure_never_invokes_legacy_providers(self):
        extractor, url = self.candidates[0]
        with mock.patch("xhs_download.services.upstream_matching.match_upstream_url",
                        return_value=(self.match(extractor),)), \
                mock.patch("xhs_download.services.platforms.third_party.runtime_bool", return_value=True), \
                mock.patch("xhs_download.services.common.validate_public_download_url"), \
                mock.patch("xhs_download.services.upstream_matching.parse_upstream_url",
                           side_effect=XhsParseError("synthetic failure")) as parse, \
                mock.patch.object(self.adapter, "iter_provider_specs") as legacy:
            with self.assertRaises(XhsParseError):
                self.adapter.parse_url(url)
            parse.assert_called_once()
            legacy.assert_not_called()
