"""D-06 adapter facts and provenance, without outbound requests or grants."""
from types import SimpleNamespace
from unittest import TestCase

from .services.pipeline import Work, validate_work
from .services.platforms.bilibili import BILIBILI_MEDIA_HOST_SUFFIXES
from .services.platforms.douyin import DOUYIN_MEDIA_HOST_SUFFIXES
from .services.platforms.third_party import THIRD_PARTY_PLATFORM_RULES
from .services.platforms.xiaohongshu import XHS_MEDIA_HOST_SUFFIXES
from .services.provider_adapters.gallerydl import GalleryDlMapper
from .services.provider_adapters.parsehub import ParseHubMapper
from .services.provider_adapters.service_facts import _host_policy
from .services.provider_adapters.videodl import VideodlMapper
from .services.provider_adapters.ytdlp import YtDlpAdapter
from .tests_provider_adapters import deny_map_io


EMPTY_POLICY_SOURCE = "contract:empty_list:any_public_https_host_passing_per_hop_checks"
SOURCE = "https://source.example/work/synthetic"
MEDIA = "https://cdn.example/media.mp4"
KNOWN_POLICIES = (
    (("douyin.com", "iesdouyin.com", "amemv.com"), DOUYIN_MEDIA_HOST_SUFFIXES,
     "existing:DOUYIN_MEDIA_HOST_SUFFIXES"),
    (("bilibili.com", "b23.tv"), BILIBILI_MEDIA_HOST_SUFFIXES,
     "existing:BILIBILI_MEDIA_HOST_SUFFIXES"),
    (("xiaohongshu.com", "xhslink.com"), XHS_MEDIA_HOST_SUFFIXES,
     "existing:XHS_MEDIA_HOST_SUFFIXES"),
    *((rule.host_suffixes, rule.media_host_suffixes,
       "existing:THIRD_PARTY_PLATFORM_RULES." + rule.platform_id)
      for rule in THIRD_PARTY_PLATFORM_RULES if rule.media_host_suffixes),
)


class HostPolicyFactsTests(TestCase):
    def test_no_existing_list_is_empty_independent_of_media_host(self):
        for source in (SOURCE, "https://acfun.cn/v/ac123", "https://evil-douyin.com/work/1"):
            for media in (MEDIA, "https://other.example/file.mp4", "http://legacy.example/file.mp4"):
                with self.subTest(source=source, media=media), deny_map_io():
                    self.assertEqual(_host_policy(source, media), ((), EMPTY_POLICY_SOURCE))

    def test_all_existing_suffix_lists_and_sources_are_preserved(self):
        for hosts, suffixes, origin in KNOWN_POLICIES:
            for host in hosts:
                for prefix in ("", "www."):
                    source = f"https://{prefix}{host}/work/synthetic"
                    with self.subTest(source=source), deny_map_io():
                        self.assertEqual(_host_policy(source, MEDIA), (tuple(suffixes), origin))


class HostPolicyMappingTests(TestCase):
    def assert_policy(self, mapper, raw, source, expected, origin):
        with deny_map_io():
            work = mapper.map(raw, source)
        self.assertIsInstance(work, Work)
        self.assertEqual(validate_work(work), ())
        for item in work.items:
            for variant in item.variants:
                with self.subTest(field="host_policy", variant=variant.variant_id):
                    self.assertEqual(variant.host_policy, expected)
                path = f"items.{item.item_id}.variants.{variant.variant_id}.host_policy"
                with self.subTest(field="field_sources", variant=variant.variant_id):
                    self.assertEqual(work.field_sources[path], origin)

    def test_generic_extractors_emit_empty_policy_and_contract_source(self):
        cases = (
            (YtDlpAdapter(), {"id": "synthetic", "formats": [
                {"format_id": "one", "url": MEDIA, "ext": "mp4"},
                {"format_id": "two", "url": "https://other.example/media.mp4", "ext": "mp4"},
            ]}),
            (GalleryDlMapper(), [[2, {"post_id": "synthetic"}],
                                 [3, MEDIA, {"extension": "mp4"}],
                                 [3, "https://other.example/image.jpg", {"extension": "jpg"}]]),
        )
        for mapper, raw in cases:
            for source in (SOURCE, "https://www.youtube.com/watch?v=synthetic"):
                with self.subTest(mapper=mapper.name, source=source):
                    self.assert_policy(mapper, raw, source, (), EMPTY_POLICY_SOURCE)

    def test_parsehub_and_videodl_reuse_empty_or_existing_policies(self):
        cases = [(SOURCE, (), EMPTY_POLICY_SOURCE)]
        cases.extend((f"https://{hosts[0]}/work/synthetic", tuple(suffixes), origin)
                     for hosts, suffixes, origin in KNOWN_POLICIES)
        for source, policy, origin in cases:
            mappings = (
                (ParseHubMapper(), {"_class": "VideoParseResult", "fields": {
                    "id": "synthetic", "raw_url": source,
                    "media": {"_class": "VideoRef", "fields": {"url": MEDIA, "ext": "mp4"}},
                }}),
                (VideodlMapper(backend_id="SyntheticBackend", backend_category="common"),
                 [SimpleNamespace(source="SyntheticDisplayLabel", identifier="synthetic",
                                  download_url=MEDIA, ext="mp4")]),
            )
            for mapper, raw in mappings:
                with self.subTest(mapper=mapper.name, source=source):
                    self.assert_policy(mapper, raw, source, policy, origin)

    def test_parsehub_embedded_ytdlp_preserves_contract_source(self):
        source = "https://www.youtube.com/watch?v=synthetic"
        raw = {"_class": "YtVideoParseResult", "fields": {
            "raw_url": source,
            "dl": {"_class": "YtVideoInfo", "fields": {"info_json": {
                "id": "synthetic", "url": MEDIA, "ext": "mp4",
            }}},
        }}
        self.assert_policy(ParseHubMapper(), raw, source, (), EMPTY_POLICY_SOURCE)
