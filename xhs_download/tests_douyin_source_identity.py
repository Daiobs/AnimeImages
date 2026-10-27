import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.assets import MediaAsset, ParseResult
from .services.batch_parse.results import build_batch_result_row
from .services.common import extract_url_from_text
from .services.platforms.douyin import DouyinAdapter
from .services.platforms.douyin_providers import (
    DouyinParseContext,
    DouyinTianTianJieXiProvider,
    build_tiantian_douyin_request_url,
    context_log_fields,
    select_tiantian_request_url,
)
from .services.platforms.kukutool_generic import source_key_for_url
from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiFailureCooldownError,
    TianTianJieXiGenericProvider,
    build_single_flight_key,
    source_key_for_identity,
)
from .services.registry import PlatformRegistry


AWEME_ID = "7669858827638845160"
CANONICAL_URL = f"https://www.douyin.com/video/{AWEME_ID}"
SOURCE_IDENTITY = f"douyin:{AWEME_ID}"
MEDIA_URL = "https://v26-default.365yg.com/controlled-video.mp4"
TIANTIAN_REQUEST_URL = CANONICAL_URL


class FakeMonotonicClock:
    def __init__(self, value=100.0):
        self.value = float(value)

    def __call__(self):
        return self.value

    def advance(self, seconds):
        self.value += float(seconds)


def provider_result():
    return ParseResult(
        platform="douyin",
        post_type="video",
        base_filename="controlled",
        assets=(
            MediaAsset(
                media_type="video",
                url=MEDIA_URL,
                platform="douyin",
                provider="tiantianjiexi",
                size_bytes=100 * 1024 * 1024,
            ),
        ),
        metadata={"selected_provider": "tiantianjiexi"},
    )


class DouyinSourceIdentityResolutionTests(SimpleTestCase):
    def test_direct_id_shapes_converge_without_source_network_calls(self):
        urls_and_kinds = (
            (CANONICAL_URL, "video_url"),
            (f"https://www.douyin.com/note/{AWEME_ID}", "note_url"),
            (
                f"https://www.iesdouyin.com/share/video/{AWEME_ID}/",
                "share_url",
            ),
            (
                "https://www.douyin.com/user/self"
                f"?from_tab_name=main&modal_id={AWEME_ID}",
                "modal_url",
            ),
            (
                f"https://www.douyin.com/?item_id={AWEME_ID}&utm_source=safe",
                "query_id",
            ),
        )

        adapter = DouyinAdapter()
        with (
            patch("xhs_download.services.platforms.douyin.requests.get") as short_get,
            patch(
                "xhs_download.services.platforms.douyin.request_get_with_retry"
            ) as page_get,
        ):
            contexts = [
                adapter.build_parse_context(url, fetch_direct_page=True)
                for url, _kind in urls_and_kinds
            ]

        short_get.assert_not_called()
        page_get.assert_not_called()
        for context, (_url, expected_kind) in zip(contexts, urls_and_kinds):
            with self.subTest(expected_kind=expected_kind):
                self.assertEqual(context.aweme_id, AWEME_ID)
                self.assertEqual(context.source_identity, SOURCE_IDENTITY)
                self.assertEqual(context.canonical_url, CANONICAL_URL)
                self.assertEqual(context.input_kind, expected_kind)
                self.assertTrue(context.source_identity_resolved)

    def test_share_text_and_short_link_resolve_to_same_identity(self):
        short_url = "https://v.douyin.com/SAFE_TEST/"
        share_text = f"复制此链接打开抖音 {short_url} 查看作品"
        response = Mock(
            status_code=302,
            url=short_url,
            headers={"Location": f"{CANONICAL_URL}?region=CN"},
        )
        response.close = Mock()

        with patch(
            "xhs_download.services.platforms.douyin.requests.get",
            return_value=response,
        ) as short_get:
            context = DouyinAdapter().build_parse_context(
                extract_url_from_text(share_text),
                fetch_direct_page=False,
                fast_short_link=True,
            )

        self.assertEqual(short_get.call_count, 1)
        self.assertEqual(context.aweme_id, AWEME_ID)
        self.assertEqual(context.source_identity, SOURCE_IDENTITY)
        self.assertEqual(context.canonical_url, CANONICAL_URL)
        self.assertEqual(context.input_kind, "short_link")
        self.assertEqual(context.resolved_url, f"{CANONICAL_URL}?region=CN")

    def test_short_link_stops_on_second_redirect_when_identity_appears(self):
        short_url = "https://v.douyin.com/SAFE_TEST/"
        intermediate_url = "https://www.douyin.com/discover/safe"
        responses = (
            Mock(
                status_code=302,
                url=short_url,
                headers={"Location": intermediate_url},
            ),
            Mock(
                status_code=302,
                url=intermediate_url,
                headers={"Location": f"{CANONICAL_URL}?region=CN"},
            ),
        )
        for response in responses:
            response.close = Mock()

        with patch(
            "xhs_download.services.platforms.douyin.requests.get",
            side_effect=responses,
        ) as short_get:
            resolved_url = DouyinAdapter().resolve_short_url(short_url)

        self.assertEqual(resolved_url, f"{CANONICAL_URL}?region=CN")
        self.assertEqual(short_get.call_count, 2)
        for response in responses:
            response.close.assert_called_once()

    def test_short_link_timeout_is_bounded_before_network_call(self):
        adapter = DouyinAdapter()

        with patch(
            "xhs_download.services.platforms.douyin.requests.get"
        ) as short_get:
            with self.assertRaises(requests.Timeout):
                adapter.resolve_short_url(
                    "https://v.douyin.com/SAFE_TEST/",
                    request_deadline=time.monotonic() - 1,
                )

        short_get.assert_not_called()

    @override_settings(DOUYIN_SHORT_LINK_MAX_REDIRECTS=1)
    def test_short_link_redirect_limit_remains_bounded(self):
        short_url = "https://v.douyin.com/SAFE_TEST/"
        first_url = "https://www.douyin.com/discover/first"
        second_url = "https://www.douyin.com/discover/second"
        responses = (
            Mock(status_code=302, url=short_url, headers={"Location": first_url}),
            Mock(status_code=302, url=first_url, headers={"Location": second_url}),
        )
        for response in responses:
            response.close = Mock()

        with patch(
            "xhs_download.services.platforms.douyin.requests.get",
            side_effect=responses,
        ) as short_get:
            with self.assertRaises(requests.TooManyRedirects):
                DouyinAdapter().resolve_short_url(short_url)

        self.assertEqual(short_get.call_count, 2)
        for response in responses:
            response.close.assert_called_once()

    def test_short_link_without_redirect_identity_keeps_bounded_page_fallback(self):
        short_url = "https://v.douyin.com/SAFE_TEST/"
        resolved_url = "https://www.douyin.com/discover/controlled"
        redirect_response = Mock(
            status_code=200,
            url=resolved_url,
            headers={},
        )
        redirect_response.close = Mock()
        page_response = Mock(
            url=resolved_url,
            text=(
                '<script type="application/json">'
                f'{{"aweme_id":"{AWEME_ID}"}}'
                "</script>"
            ),
        )
        page_response.close = Mock()

        with (
            patch(
                "xhs_download.services.platforms.douyin.requests.get",
                return_value=redirect_response,
            ),
            patch(
                "xhs_download.services.platforms.douyin.request_get_with_retry",
                return_value=page_response,
            ) as page_get,
        ):
            context = DouyinAdapter().build_parse_context(
                short_url,
                fast_short_link=True,
                request_deadline=time.monotonic() + 2,
            )

        self.assertEqual(context.aweme_id, AWEME_ID)
        self.assertEqual(context.source_identity, SOURCE_IDENTITY)
        self.assertEqual(context.canonical_url, CANONICAL_URL)
        page_get.assert_called_once()
        self.assertEqual(page_get.call_args.kwargs["retries"], 1)

    def test_source_keys_are_stable_for_same_aweme_and_distinct_for_other_aweme(self):
        alternate_aweme_id = "7669858827638845161"

        self.assertEqual(
            source_key_for_identity(SOURCE_IDENTITY),
            source_key_for_url(CANONICAL_URL),
        )
        self.assertNotEqual(
            source_key_for_identity(SOURCE_IDENTITY),
            source_key_for_identity(f"douyin:{alternate_aweme_id}"),
        )

    def test_context_logs_use_fixed_diagnostics_without_raw_query(self):
        context = DouyinParseContext(
            url=(
                "https://www.douyin.com/user/self"
                f"?modal_id={AWEME_ID}&token=never-log-this"
            ),
            aweme_id=AWEME_ID,
            input_kind="modal_url",
            source_resolution_duration_ms=1234,
            ultra_window_ms=6000,
            provider_window_started_after_resolution=True,
            provider_budget_ms=6000,
        )

        fields = context_log_fields(context)

        self.assertEqual(fields["input_kind"], "modal_url")
        self.assertEqual(fields["source_resolution_ms"], 1234)
        self.assertTrue(fields["aweme_identity_resolved"])
        self.assertEqual(fields["ultra_window_ms"], 6000)
        self.assertTrue(fields["provider_window_started_after_resolution"])
        self.assertNotIn("token", str(fields))
        self.assertNotIn("never-log-this", str(fields))


class DouyinUltraBudgetLifecycleTests(SimpleTestCase):
    def test_short_link_resolution_does_not_consume_ultra_window(self):
        clock = FakeMonotonicClock()
        captured = {}

        class Provider:
            name = "tiantianjiexi"

            def parse(self, _adapter, context):
                captured["deadline"] = context.deadline_monotonic
                captured["continuation_deadline"] = (
                    context.continuation_deadline_monotonic
                )
                return provider_result()

        def build_context(url, **_kwargs):
            captured["resolution_deadline"] = _kwargs["request_deadline"]
            clock.advance(1.2)
            context = DouyinParseContext(
                url=url,
                final_url=CANONICAL_URL,
                aweme_id=AWEME_ID,
                prefer_ultra_hd=True,
            )
            context.resolved_url = CANONICAL_URL
            context.canonical_url = CANONICAL_URL
            context.source_identity = SOURCE_IDENTITY
            context.input_kind = "short_link"
            context.source_identity_resolved = True
            context.source_resolution_duration_ms = 1200
            context.source_resolution_completed_monotonic = clock()
            return context

        adapter = DouyinAdapter()
        adapter.build_parse_context = Mock(side_effect=build_context)
        adapter.get_result_providers = Mock(return_value=(Provider(),))

        with (
            patch("xhs_download.services.platforms.douyin.monotonic", clock),
            patch(
                "xhs_download.services.platforms.douyin.get_platform_provider_settings",
                return_value=SimpleNamespace(ultra_total_timeout_seconds=6.0),
            ),
            patch(
                "xhs_download.services.platforms.douyin.find_active_douyin_continuation",
                return_value="",
            ),
        ):
            result = adapter.parse_url(
                "https://v.douyin.com/SAFE_TEST/",
                prefer_ultra_hd=True,
                allow_continuation=True,
                continuation_identity="controlled-client",
            )

        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertAlmostEqual(captured["resolution_deadline"], 102.0, places=6)
        self.assertAlmostEqual(captured["deadline"], 107.2, places=6)
        self.assertAlmostEqual(captured["continuation_deadline"], 116.2, places=6)

    def test_explicit_batch_deadline_remains_a_hard_outer_cap(self):
        clock = FakeMonotonicClock()
        captured = {}

        class Provider:
            name = "tiantianjiexi"

            def parse(self, _adapter, context):
                captured["deadline"] = context.deadline_monotonic
                return provider_result()

        def build_context(url, **_kwargs):
            captured["resolution_deadline"] = _kwargs["request_deadline"]
            clock.advance(1.2)
            context = DouyinParseContext(url=url, aweme_id=AWEME_ID)
            context.source_resolution_completed_monotonic = clock()
            return context

        adapter = DouyinAdapter()
        adapter.build_parse_context = Mock(side_effect=build_context)
        adapter.get_result_providers = Mock(return_value=(Provider(),))

        with (
            patch("xhs_download.services.platforms.douyin.monotonic", clock),
            patch(
                "xhs_download.services.platforms.douyin.get_platform_provider_settings",
                return_value=SimpleNamespace(ultra_total_timeout_seconds=6.0),
            ),
        ):
            adapter.parse_url(
                "https://v.douyin.com/SAFE_TEST/",
                prefer_ultra_hd=True,
                deadline_monotonic=105.0,
            )

        self.assertAlmostEqual(captured["resolution_deadline"], 102.0, places=6)
        self.assertEqual(captured["deadline"], 105.0)

    def test_provider_result_exposes_safe_lifecycle_diagnostics(self):
        clock = FakeMonotonicClock()

        class Provider:
            name = "tiantianjiexi"

            def parse(self, _adapter, _context):
                clock.advance(3.0)
                return provider_result()

        def build_context(url, **_kwargs):
            clock.advance(1.5)
            context = DouyinParseContext(
                url=url,
                final_url=CANONICAL_URL,
                aweme_id=AWEME_ID,
                input_kind="short_link",
                source_resolution_duration_ms=1500,
                source_resolution_completed_monotonic=clock(),
            )
            return context

        adapter = DouyinAdapter()
        adapter.build_parse_context = Mock(side_effect=build_context)
        adapter.get_result_providers = Mock(return_value=(Provider(),))

        with (
            patch("xhs_download.services.platforms.douyin.monotonic", clock),
            patch(
                "xhs_download.services.platforms.douyin.get_platform_provider_settings",
                return_value=SimpleNamespace(ultra_total_timeout_seconds=6.0),
            ),
        ):
            result = adapter.parse_url(
                "https://v.douyin.com/SAFE_TEST/",
                prefer_ultra_hd=True,
            )

        self.assertEqual(result.metadata["input_kind"], "short_link")
        self.assertEqual(result.metadata["source_resolution_ms"], 1500)
        self.assertTrue(result.metadata["aweme_identity_resolved"])
        self.assertEqual(result.metadata["ultra_window_ms"], 6000)
        self.assertTrue(
            result.metadata["provider_window_started_after_resolution"]
        )
        self.assertEqual(result.metadata["provider_budget_ms"], 6000)
        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")

    def test_empty_result_preserves_safe_lifecycle_diagnostics(self):
        clock = FakeMonotonicClock()

        class EmptyProvider:
            name = "tiantianjiexi"

            def parse(self, _adapter, _context):
                return None

        adapter = DouyinAdapter()
        adapter.get_result_providers = Mock(return_value=(EmptyProvider(),))

        with (
            patch("xhs_download.services.platforms.douyin.monotonic", clock),
            patch(
                "xhs_download.services.platforms.douyin.get_platform_provider_settings",
                return_value=SimpleNamespace(ultra_total_timeout_seconds=6.0),
            ),
        ):
            result = adapter.parse_url(CANONICAL_URL, prefer_ultra_hd=True)

        self.assertFalse(result.assets)
        self.assertEqual(result.metadata["input_kind"], "video_url")
        self.assertTrue(result.metadata["aweme_identity_resolved"])
        self.assertEqual(result.metadata["ultra_window_ms"], 6000)
        self.assertTrue(
            result.metadata["provider_window_started_after_resolution"]
        )

    def test_provider_failure_log_redacts_urls_and_sensitive_query_values(self):
        class FailingProvider:
            name = "tiantianjiexi"

            def parse(self, _adapter, _context):
                raise ValueError(
                    "failed https://provider.invalid/media?token=never-log-this"
                )

        adapter = DouyinAdapter()
        adapter.get_result_providers = Mock(return_value=(FailingProvider(),))

        with (
            patch(
                "xhs_download.services.platforms.douyin.get_platform_provider_settings",
                return_value=SimpleNamespace(ultra_total_timeout_seconds=6.0),
            ),
            patch(
                "xhs_download.services.platforms.douyin.logger.info"
            ) as log_info,
        ):
            result = adapter.parse_url(CANONICAL_URL, prefer_ultra_hd=True)

        self.assertFalse(result.assets)
        failure_call = next(
            call
            for call in log_info.call_args_list
            if call.args == ("douyin_provider_failed",)
        )
        fields = failure_call.kwargs["extra"]
        self.assertEqual(fields["input_kind"], "video_url")
        self.assertTrue(fields["aweme_identity_resolved"])
        self.assertNotIn("provider.invalid", fields["error"])
        self.assertNotIn("never-log-this", fields["error"])

    def test_single_and_batch_share_canonical_identity_semantics(self):
        contexts = []

        class Provider:
            name = "tiantianjiexi"

            def parse(self, _adapter, context):
                contexts.append(context)
                return provider_result()

        adapter = DouyinAdapter()
        adapter.resolve_short_url = Mock(return_value=CANONICAL_URL)
        adapter.get_result_providers = Mock(return_value=(Provider(),))
        registry = PlatformRegistry(adapters=(adapter,))
        provider_settings = SimpleNamespace(ultra_total_timeout_seconds=6.0)

        with patch(
            "xhs_download.services.platforms.douyin.get_platform_provider_settings",
            return_value=provider_settings,
        ):
            registry.parse_url(
                "https://v.douyin.com/SAFE_TEST/",
                prefer_ultra_hd=True,
            )
            batch_row = build_batch_result_row(
                SimpleNamespace(),
                "https://v.douyin.com/SAFE_TEST/",
                prefer_ultra_hd=True,
                prefer_youtube_merge=False,
                per_link_timeout_seconds=12.0,
                deadline=time.monotonic() + 12.0,
                parser=registry.parse_url,
                display_context_builder=lambda _result, **_kwargs: {
                    "video_items": ({"url": MEDIA_URL},),
                    "image_items": (),
                },
                request_country_code_builder=lambda _request: "",
                query_uses_third_party_provider_func=lambda _query: False,
                third_party_provider_is_available_func=lambda _request: True,
            )

        self.assertEqual(batch_row["status"], "success")
        self.assertEqual(len(contexts), 2)
        self.assertEqual(
            {context.source_identity for context in contexts},
            {SOURCE_IDENTITY},
        )
        self.assertEqual(
            {context.canonical_url for context in contexts},
            {CANONICAL_URL},
        )
        self.assertTrue(
            all(context.ultra_window_ms == 6000 for context in contexts)
        )


@override_settings(
    THIRD_PARTY_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PHONE="controlled-phone",
    TIANTIANJIEXI_PASSWORD="controlled-password",
    TIANTIANJIEXI_RETRIES=1,
    TIANTIANJIEXI_FAILURE_CACHE_TTL_SECONDS=8,
)
class TianTianDouyinIdentityTests(SimpleTestCase):
    def setUp(self):
        cache.clear()

    def tearDown(self):
        cache.clear()

    def test_douyin_wrapper_separates_provider_url_from_logical_identity(self):
        calls = []

        class Provider:
            def parse(
                self,
                url,
                timeout_seconds=None,
                source_identity=None,
                **_kwargs,
            ):
                calls.append((url, source_identity, timeout_seconds))
                return {
                    "title": "controlled title",
                    "cover": "https://p3.douyinpic.com/cover.jpeg",
                    "videos": [
                        {
                            "url": MEDIA_URL,
                            "quality": "4K",
                            "size": 100 * 1024 * 1024,
                        }
                    ],
                }

        context = DouyinParseContext(
            url="https://v.douyin.com/SAFE_TEST/",
            final_url=f"{CANONICAL_URL}?region=CN",
            aweme_id=AWEME_ID,
            prefer_ultra_hd=True,
            deadline_monotonic=time.monotonic() + 6,
        )
        context.canonical_url = CANONICAL_URL
        context.source_identity = SOURCE_IDENTITY
        provider_settings = SimpleNamespace(
            provider_is_enabled=lambda _name: True,
            ultra_total_timeout_seconds=6.0,
            enabled_provider_order=("tiantianjiexi", "native"),
            ultra_kukutool_grace_seconds=1.0,
            kukutool_sidecar_timeout_seconds=1.0,
        )

        result = DouyinTianTianJieXiProvider(
            provider_settings=provider_settings,
            provider=Provider(),
            reserve_fallback_grace=False,
        ).parse(DouyinAdapter(), context)

        self.assertEqual(result.metadata["selected_provider"], "tiantianjiexi")
        self.assertEqual(calls[0][0], TIANTIAN_REQUEST_URL)
        self.assertEqual(calls[0][1], SOURCE_IDENTITY)

    def test_tiantian_request_url_is_derived_from_validated_aweme_identity(self):
        contexts = (
            DouyinParseContext(
                url="https://v.douyin.com/SAFE_TEST/",
                final_url=CANONICAL_URL,
                aweme_id=AWEME_ID,
            ),
            DouyinParseContext(
                url=CANONICAL_URL,
                final_url=CANONICAL_URL,
                aweme_id=AWEME_ID,
            ),
            DouyinParseContext(
                url=(
                    "https://www.douyin.com/user/self"
                    f"?modal_id={AWEME_ID}"
                ),
                aweme_id=AWEME_ID,
            ),
            DouyinParseContext(
                url=f"https://www.iesdouyin.com/share/video/{AWEME_ID}/",
                aweme_id=AWEME_ID,
            ),
        )

        self.assertEqual(
            {select_tiantian_request_url(context) for context in contexts},
            {TIANTIAN_REQUEST_URL},
        )
        self.assertEqual(
            {context.source_identity for context in contexts},
            {SOURCE_IDENTITY},
        )

    def test_tiantian_request_url_rejects_invalid_aweme_identity(self):
        self.assertEqual(build_tiantian_douyin_request_url("not-an-id"), "")

    def test_fresh_wrapper_attempt_calls_tiantian_once_with_video_representation(self):
        payload = {
            "title": "controlled title",
            "cover": "https://p3.douyinpic.com/cover.jpeg",
            "videos": [
                {
                    "url": MEDIA_URL,
                    "quality": "原画",
                    "size": 100 * 1024 * 1024,
                }
            ],
        }
        client = Mock()
        client.parse_url.return_value = payload
        context = DouyinParseContext(
            url=CANONICAL_URL,
            final_url=CANONICAL_URL,
            aweme_id=AWEME_ID,
            prefer_ultra_hd=True,
            deadline_monotonic=time.monotonic() + 6,
        )

        result = DouyinTianTianJieXiProvider(
            provider_settings=SimpleNamespace(
                provider_is_enabled=lambda _name: True,
                ultra_total_timeout_seconds=6.0,
                enabled_provider_order=("tiantianjiexi", "native"),
                ultra_kukutool_grace_seconds=1.0,
                kukutool_sidecar_timeout_seconds=1.0,
            ),
            provider=TianTianJieXiGenericProvider(client=client),
            reserve_fallback_grace=False,
        ).parse(DouyinAdapter(), context)

        self.assertEqual(result.video_assets[0].size_bytes, 100 * 1024 * 1024)
        client.parse_url.assert_called_once()
        self.assertEqual(client.parse_url.call_args.args[0], TIANTIAN_REQUEST_URL)

    def test_same_identity_reuses_payload_cache_across_url_shapes(self):
        payload = {
            "videos": [
                {"url": MEDIA_URL, "quality": "4K", "size": 100 * 1024 * 1024}
            ]
        }
        client = Mock()
        client.parse_url.return_value = payload
        provider = TianTianJieXiGenericProvider(client=client)
        validator = lambda value: bool(value.get("videos"))

        first = provider.parse(
            select_tiantian_request_url(DouyinParseContext(
                url="https://v.douyin.com/SAFE_TEST/", resolved_url=CANONICAL_URL,
                aweme_id=AWEME_ID,
            )),
            timeout_seconds=0.5,
            source_identity=SOURCE_IDENTITY,
            payload_validator=validator,
        )
        second = provider.parse(
            f"{CANONICAL_URL}?utm_source=controlled",
            timeout_seconds=0.5,
            source_identity=SOURCE_IDENTITY,
            payload_validator=validator,
        )

        self.assertEqual(first, payload)
        self.assertEqual(second, payload)
        client.parse_url.assert_called_once()

    def test_same_identity_observes_single_flight_across_url_shapes(self):
        source_key = source_key_for_url(CANONICAL_URL)
        cache.set(build_single_flight_key(source_key), "controlled-owner", 30)
        payload = {"videos": [{"url": MEDIA_URL, "quality": "4K"}]}
        provider = TianTianJieXiGenericProvider(client=Mock())

        with patch.object(
            provider,
            "wait_for_cached_payload",
            return_value=payload,
        ) as wait_for_payload:
            result = provider.parse(
                "https://v.douyin.com/SAFE_TEST/",
                timeout_seconds=0.5,
                source_identity=SOURCE_IDENTITY,
                payload_validator=lambda value: bool(value.get("videos")),
            )

        self.assertEqual(result, payload)
        wait_for_payload.assert_called_once()
        provider.client.parse_url.assert_not_called()

    def test_same_identity_failure_cooldown_cannot_be_bypassed_by_url_shape(self):
        first_client = Mock()
        first_client.parse_url.side_effect = requests.Timeout("controlled timeout")
        validator = lambda value: bool(value.get("videos"))

        with self.assertRaises(requests.Timeout):
            TianTianJieXiGenericProvider(client=first_client).parse(
                "https://v.douyin.com/SAFE_TEST/",
                timeout_seconds=0.5,
                source_identity=SOURCE_IDENTITY,
                payload_validator=validator,
            )

        second_client = Mock()
        with self.assertRaises(TianTianJieXiFailureCooldownError):
            TianTianJieXiGenericProvider(client=second_client).parse(
                CANONICAL_URL,
                timeout_seconds=0.5,
                source_identity=SOURCE_IDENTITY,
                payload_validator=validator,
            )

        second_client.parse_url.assert_not_called()

    def test_generic_tiantian_without_explicit_identity_remains_url_keyed(self):
        payload = {
            "videos": [
                {"url": MEDIA_URL, "quality": "4K", "size": 100 * 1024 * 1024}
            ]
        }
        client = Mock()
        client.parse_url.return_value = payload
        provider = TianTianJieXiGenericProvider(client=client)
        validator = lambda value: bool(value.get("videos"))

        provider.parse(
            "https://www.tiktok.com/@controlled/video/111111111111111111",
            timeout_seconds=0.5,
            payload_validator=validator,
        )
        provider.parse(
            "https://www.tiktok.com/@controlled/video/222222222222222222",
            timeout_seconds=0.5,
            payload_validator=validator,
        )

        self.assertEqual(client.parse_url.call_count, 2)
