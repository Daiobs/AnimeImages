"""Service map regressions for the request-context clarification in #145."""
from copy import deepcopy

from django.test import SimpleTestCase

from .services.pipeline import Fetchable, Work, validate_work
from .services.provider_adapters.kukutool import KukutoolAdapter
from .services.provider_adapters.tiantian import TianTianAdapter
from .tests_provider_adapters import deny_map_io


SOURCE_URL = "https://www.tiktok.com/@demo/video/123"
MEDIA_URL = "https://cdn.example/context.mp4"
ORDINARY_HEADERS = {
    "Referer": "https://source.example/private-referrer",
    "Origin": "https://source.example",
    "User-Agent": "synthetic-agent",
    "Accept": "video/*",
}
ADAPTERS = (
    (TianTianAdapter, "streams"),
    (KukutoolAdapter, "video_fullinfo"),
)


class ServiceAdapterContextTests(SimpleTestCase):
    def map_variant(self, adapter_class, variants_key, facts, scope):
        row = {"url": MEDIA_URL}
        payload = {"id": "123", "title": "Synthetic context", "videos": [{
            "url": MEDIA_URL, variants_key: [row],
        }]}
        target = payload if scope == "payload" else row
        target.update(deepcopy(facts))
        raw = {"code": 200, "data": payload} if adapter_class is TianTianAdapter else payload
        before = deepcopy(raw)
        with deny_map_io():
            work = adapter_class().map(raw, SOURCE_URL)
        self.assertEqual(raw, before)
        self.assertIsInstance(work, Work)
        self.assertEqual(validate_work(work), ())
        self.assertEqual(len(work.items), 1)
        item = work.items[0]
        self.assertEqual(len(item.variants), 1)
        variant = item.variants[0]
        prefix = f"items.{item.item_id}.variants.{variant.variant_id}"
        return work, variant, prefix

    def assert_observed_source(self, work, prefix, field):
        source = work.field_sources[f"{prefix}.{field}"]
        self.assertIsInstance(source, str)
        self.assertNotIn(source, ("", "contract:unknown", "contract:default"))

    def test_explicit_required_referer_and_origin_are_retained(self):
        for adapter_class, variants_key in ADAPTERS:
            for scope in ("payload", "variant"):
                for with_values in (False, True):
                    with self.subTest(adapter=adapter_class.name, scope=scope, with_values=with_values):
                        facts = {"required_headers": ["Referer", "Origin"]}
                        if with_values:
                            facts["headers"] = ORDINARY_HEADERS
                        work, variant, prefix = self.map_variant(adapter_class, variants_key, facts, scope)
                        self.assertEqual(variant.required_headers, ("origin", "referer"))
                        self.assert_observed_source(work, prefix, "required_headers")
                        for value in ORDINARY_HEADERS.values():
                            self.assertNotIn(value, repr(work))

    def test_ordinary_header_values_alone_are_not_requirements(self):
        for adapter_class, variants_key in ADAPTERS:
            for scope in ("payload", "variant"):
                with self.subTest(adapter=adapter_class.name, scope=scope):
                    work, variant, prefix = self.map_variant(
                        adapter_class, variants_key, {"headers": ORDINARY_HEADERS}, scope,
                    )
                    self.assertEqual(variant.required_headers, ())
                    self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
                    self.assertEqual(work.field_sources[f"{prefix}.browser_fetchable"], "contract:unknown")

    def assert_browser_context_unavailable(self, facts):
        for adapter_class, variants_key in ADAPTERS:
            for scope in ("payload", "variant"):
                with self.subTest(adapter=adapter_class.name, scope=scope, facts=facts):
                    work, variant, prefix = self.map_variant(adapter_class, variants_key, facts, scope)
                    self.assertIs(variant.browser_fetchable, Fetchable.FALSE)
                    self.assert_observed_source(work, prefix, "browser_fetchable")

    def test_ip_bound_resources_are_not_browser_fetchable(self):
        self.assert_browser_context_unavailable({"ip_bound": True})

    def test_required_but_missing_context_is_not_browser_fetchable(self):
        self.assert_browser_context_unavailable({"requires_context": True})

    def test_headers_present_without_values_is_not_browser_fetchable(self):
        self.assert_browser_context_unavailable({"headers_present": True})
        for value in (None, ""):
            self.assert_browser_context_unavailable({"headers_present": True, "headers": {"Referer": value}})

    def test_absent_context_facts_remain_unknown(self):
        for adapter_class, variants_key in ADAPTERS:
            with self.subTest(adapter=adapter_class.name):
                work, variant, prefix = self.map_variant(adapter_class, variants_key, {}, "payload")
                self.assertEqual(variant.required_headers, ())
                self.assertIs(variant.browser_fetchable, Fetchable.UNKNOWN)
                self.assertEqual(work.field_sources[f"{prefix}.browser_fetchable"], "contract:unknown")
