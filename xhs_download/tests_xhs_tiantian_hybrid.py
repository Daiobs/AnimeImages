from types import SimpleNamespace
import time
from unittest.mock import Mock, patch

from django.test import RequestFactory, SimpleTestCase, override_settings

from .services.assets import MediaAsset, ParseResult
from .services.batch_parse.results import build_batch_result_row
from .services.common import XhsProviderTimeoutError
from .services.download_routing import (
    ROUTE_BLOCKED,
    ROUTE_DIRECT,
    decide_download_route,
    item_can_browser_direct_video,
    single_video_can_server_proxy_download,
)
from .services.downloads import (
    provider_manifest_allows_public_download,
    validate_provider_signed_download_url,
)
from .services.platforms.tiantianjiexi_generic import (
    TianTianJieXiFailureCooldownError,
    TianTianJieXiResultNormalizer,
    source_key_for_request,
)
from .services.platforms.xiaohongshu import (
    XiaohongshuAdapter,
    normalize_video_url,
    xhs_native_video_url_is_browser_streaming_allowed,
)
from .services.platforms.xhs_tiantian_hybrid import (
    XhsUltraAttemptController,
    build_xhs_tiantian_source_identity,
    enhance_xhs_ordinary_video,
    select_xhs_tiantian_video_candidate,
    validate_xhs_tiantian_payload,
    xhs_tiantian_native_result_is_eligible,
    xhs_tiantian_titles_correspond,
)
from .services.platforms.xhs_tiantian_media_policy import (
    XHS_TIANTIAN_PROBE_MAX_BYTES,
    XhsTianTianMediaPolicyError,
    XhsTianTianProbeResult,
    is_iso_bmff_video_prefix,
    probe_xhs_tiantian_video,
    validate_xhs_tiantian_media_url,
    xhs_tiantian_manifest_item_is_allowed,
)
from .services.provider_manifest import build_provider_manifest_token
from .services.provider_access import (
    PROVIDER_DOUYIN_ULTRA,
    provider_access_summary,
)
from .services.provider_recovery import get_tiantianjiexi_manifest_identity
from .services.provider_reliability import ProviderResultInvalidError
from .services.provider_strategy import (
    DOWNLOAD_STRATEGY_BROWSER_STREAMING,
    build_download_strategy,
)
from .views import (
    apply_search_result_to_context,
    build_result_display_context,
    ultra_hd_is_available,
)


NOTE_ID = "67abcdef0123456789abcdef"
OTHER_NOTE_ID = "68abcdef0123456789abcdef"
NATIVE_VIDEO_URL = "https://sns-video-bd.xhscdn.com/native/video.mp4"
TIANTIAN_VIDEO_URL = "https://sns-video-hw.xhscdn.com/media/ultra.mp4?fixture=1"


def native_video_result(**metadata_updates):
    metadata = {
        "title": "芋泥普洱茶",
        "author_name": "作者",
        "note_id": NOTE_ID,
        "page_state": "available",
        "source_identity_verified": True,
        "title_source": "structured",
        "cover_source": "structured",
    }
    metadata.update(metadata_updates)
    return ParseResult(
        platform="xiaohongshu",
        post_type="video",
        base_filename="作者_芋泥普洱茶",
        assets=(
            MediaAsset(
                media_type="video",
                url=NATIVE_VIDEO_URL,
                platform="xiaohongshu",
                cover_url="https://ci.xiaohongshu.com/cover",
                cover_preview_url="https://ci.xiaohongshu.com/cover?preview=1",
                quality_label="普通清晰度",
                size_bytes=2_663_888,
            ),
        ),
        metadata=metadata,
    )


def native_image_result():
    return ParseResult(
        platform="xiaohongshu",
        post_type="image",
        base_filename="gallery",
        assets=(
            MediaAsset(
                media_type="image",
                url="https://ci.xiaohongshu.com/image",
                platform="xiaohongshu",
            ),
        ),
        metadata={
            "note_id": NOTE_ID,
            "page_state": "available",
            "source_identity_verified": True,
        },
    )


def provider_payload(**updates):
    payload = {
        "title": "芋泥普洱茶",
        "description": "",
        "cover": "",
        "videos": (
            {"url": TIANTIAN_VIDEO_URL, "quality": "原画", "size": 29_143_890},
            {
                "url": "https://sns-video-hw.xhscdn.com/media/normal.mp4",
                "quality": "正常",
                "size": 2_663_888,
            },
        ),
        "images": (),
        "audio_url": "",
    }
    payload.update(updates)
    return payload


def probe_result(url=TIANTIAN_VIDEO_URL):
    return XhsTianTianProbeResult(
        final_url=url,
        content_type="video/mp4",
        bytes_read=4096,
    )


class FakeProvider:
    def __init__(self, *, payload=None, error=None, circuit_open=False):
        self.payload = payload if payload is not None else provider_payload()
        self.error = error
        self.circuit_open = circuit_open
        self.calls = []

    def circuit_is_open(self):
        return self.circuit_open

    def parse(self, url, **kwargs):
        self.calls.append((url, kwargs))
        controller = kwargs.get("attempt_controller")
        if controller is not None:
            controller.prepare_fresh_work("tiantianjiexi")
            controller.begin_provider("tiantianjiexi")
        if self.error is not None:
            raise self.error
        validator = kwargs.get("payload_validator")
        if validator is not None:
            validator(self.payload)
        return self.payload


class FakeResponse:
    def __init__(self, *, status=206, content_type="video/mp4", body=None, url=TIANTIAN_VIDEO_URL, location=""):
        self.status_code = status
        self.headers = {"Content-Type": content_type}
        if location:
            self.headers["Location"] = location
        self.url = url
        self.body = body if body is not None else b"\x00\x00\x00\x18ftypisom" + b"x" * 32
        self.closed = False

    def iter_content(self, chunk_size=1024):
        del chunk_size
        yield self.body

    def close(self):
        self.closed = True


def make_manifest_item(url=TIANTIAN_VIDEO_URL):
    source_key = source_key_for_request(
        "https://irrelevant.example/source",
        build_xhs_tiantian_source_identity(NOTE_ID),
    )
    asset_id = f"xhs:{source_key}:video:tiantianjiexi"
    cache_key = f"third_party:tiantianjiexi_generic:{source_key}"
    item = {
        "type": "video",
        "url": url,
        "asset_id": asset_id,
        "provider": "tiantianjiexi_generic",
        "provider_resource_id": source_key,
        "provider_cache_key": cache_key,
        "size_bytes": 29_143_890,
    }
    item["provider_manifest_token"] = build_provider_manifest_token(
        asset_id=asset_id,
        provider=item["provider"],
        provider_resource_id=source_key,
        provider_cache_key=cache_key,
        url=url,
        media_type="video",
    )
    return item


class XhsTianTianAdapterContractTests(SimpleTestCase):
    def test_capability_declares_ultra_hd(self):
        self.assertTrue(XiaohongshuAdapter.capabilities.ultra_hd)

    def test_parse_option_accepts_prefer_ultra_hd(self):
        self.assertIn("prefer_ultra_hd", XiaohongshuAdapter.parse_option_names)

    def test_attempt_controller_accepts_generic_provider_cooldown_callback(self):
        controller = XhsUltraAttemptController(
            client_identity="client",
            limit=10,
        )

        controller.record_cooldown("tiantianjiexi", 7)

        self.assertEqual(controller.cooldown_retry_after, 7)

    @patch(
        "xhs_download.services.platforms.xiaohongshu.enhance_xhs_ordinary_video"
    )
    @patch("xhs_download.services.platforms.xiaohongshu.request_get_with_retry")
    def test_ultra_off_native_result_does_not_call_enhancer(
        self,
        request_get,
        enhance,
    ):
        response = SimpleNamespace(
            text="",
            url=f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
            close=Mock(),
        )
        request_get.return_value = response
        adapter = XiaohongshuAdapter()
        with patch.object(
            adapter,
            "parse_html",
            return_value=native_video_result(),
        ):
            result = adapter.parse_url(
                f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
                prefer_ultra_hd=False,
            )

        self.assertEqual(result.video_assets[0].url, NATIVE_VIDEO_URL)
        enhance.assert_not_called()
        response.close.assert_called_once_with()

    def test_image_result_is_not_eligible(self):
        self.assertFalse(xhs_tiantian_native_result_is_eligible(native_image_result()))

    def test_image_result_returns_without_provider_call(self):
        provider = FakeProvider()
        result = enhance_xhs_ordinary_video(native_image_result(), provider=provider)
        self.assertEqual(result.post_type, "image")
        self.assertEqual(provider.calls, [])

    def test_livephoto_result_is_not_eligible(self):
        result = SimpleNamespace(
            platform="xiaohongshu",
            post_type="video",
            rich_media=object(),
            video_assets=(SimpleNamespace(url=NATIVE_VIDEO_URL),),
            metadata={
                "note_id": NOTE_ID,
                "page_state": "available",
                "source_identity_verified": True,
            },
        )
        self.assertFalse(xhs_tiantian_native_result_is_eligible(result))

    def test_livephoto_result_returns_without_provider_call(self):
        provider = FakeProvider()
        result = SimpleNamespace(
            platform="xiaohongshu",
            post_type="video",
            rich_media=object(),
            video_assets=(SimpleNamespace(url=NATIVE_VIDEO_URL),),
            metadata={
                "note_id": NOTE_ID,
                "page_state": "available",
                "source_identity_verified": True,
            },
        )
        self.assertIs(
            enhance_xhs_ordinary_video(result, provider=provider),
            result,
        )
        self.assertEqual(provider.calls, [])

    def test_invalid_note_identity_is_not_eligible(self):
        self.assertFalse(
            xhs_tiantian_native_result_is_eligible(
                native_video_result(note_id="invalid")
            )
        )

    def test_invalid_note_returns_without_provider_call(self):
        provider = FakeProvider()
        result = native_video_result(note_id="invalid")
        self.assertIs(
            enhance_xhs_ordinary_video(result, provider=provider),
            result,
        )
        self.assertEqual(provider.calls, [])

    def test_terminal_page_is_not_eligible(self):
        for page_state in ("deleted", "private", "unavailable", "login_required"):
            with self.subTest(page_state=page_state):
                self.assertFalse(
                    xhs_tiantian_native_result_is_eligible(
                        native_video_result(page_state=page_state)
                    )
                )

    def test_terminal_page_returns_without_provider_call(self):
        provider = FakeProvider()
        result = native_video_result(page_state="deleted")
        self.assertIs(
            enhance_xhs_ordinary_video(result, provider=provider),
            result,
        )
        self.assertEqual(provider.calls, [])

    def test_native_exact_identity_is_required(self):
        self.assertFalse(
            xhs_tiantian_native_result_is_eligible(
                native_video_result(source_identity_verified=False)
            )
        )

    def test_logical_identity_is_stable_across_url_representations(self):
        identity = build_xhs_tiantian_source_identity(NOTE_ID)
        keys = {
            source_key_for_request(url, identity)
            for url in (
                "https://xhslink.cn/o/transient",
                f"https://www.xiaohongshu.com/explore/{NOTE_ID}?tracking=redacted",
                f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
            )
        }
        self.assertEqual(len(keys), 1)

    def test_logical_identity_does_not_expose_note_id_in_source_key(self):
        source_key = source_key_for_request(
            "https://unused.example",
            build_xhs_tiantian_source_identity(NOTE_ID),
        )
        self.assertNotIn(NOTE_ID, source_key)
        self.assertRegex(source_key, r"^[0-9a-f]{16}$")

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=False,
    )
    @patch(
        "xhs_download.views.xhs_tiantian_provider_is_available",
        return_value=True,
    )
    @patch(
        "xhs_download.views.provider_access_summary",
        return_value={"request_allowed": True},
    )
    @patch(
        "xhs_download.views.provider_is_allowed_for_request",
        return_value=False,
    )
    def test_xhs_ultra_availability_does_not_require_kukutool(
        self,
        _douyin_access,
        _xhs_access,
        _xhs_provider,
    ):
        self.assertTrue(
            ultra_hd_is_available(
                RequestFactory().get("/"),
                "xiaohongshu",
            )
        )

    @override_settings(
        ULTRA_HD_PROVIDER_ENABLED=True,
        KUKUTOOL_PROVIDER_ENABLED=False,
    )
    def test_access_mode_can_be_reused_with_xhs_capability_gate(self):
        runtime_setting = SimpleNamespace(
            douyin_ultra_mode="public",
            douyin_ultra_rollout_percent=100,
            douyin_ultra_disabled_reason="",
        )
        self.assertFalse(
            provider_access_summary(
                PROVIDER_DOUYIN_ULTRA,
                runtime_setting=runtime_setting,
            )["request_allowed"]
        )
        self.assertTrue(
            provider_access_summary(
                PROVIDER_DOUYIN_ULTRA,
                runtime_setting=runtime_setting,
                hard_enabled_override=True,
            )["request_allowed"]
        )


class XhsTianTianPayloadTests(SimpleTestCase):
    def test_payload_with_https_video_is_accepted(self):
        self.assertTrue(
            validate_xhs_tiantian_payload(
                provider_payload(), note_id=NOTE_ID, native_title="芋泥普洱茶"
            )
        )

    def test_image_only_payload_is_rejected(self):
        with self.assertRaises(ProviderResultInvalidError):
            validate_xhs_tiantian_payload(
                provider_payload(videos=(), images=("https://ci.xiaohongshu.com/a",)),
                note_id=NOTE_ID,
                native_title="芋泥普洱茶",
            )

    def test_explicit_note_identity_match_is_accepted(self):
        payload = provider_payload(_source_identity_signals=(NOTE_ID,))
        self.assertTrue(
            validate_xhs_tiantian_payload(
                payload, note_id=NOTE_ID, native_title="芋泥普洱茶"
            )
        )

    def test_explicit_note_identity_mismatch_is_rejected(self):
        with self.assertRaises(ProviderResultInvalidError):
            validate_xhs_tiantian_payload(
                provider_payload(_source_identity_signals=(OTHER_NOTE_ID,)),
                note_id=NOTE_ID,
                native_title="芋泥普洱茶",
            )

    def test_explicit_title_mismatch_is_rejected(self):
        with self.assertRaises(ProviderResultInvalidError):
            validate_xhs_tiantian_payload(
                provider_payload(title="完全不同的内容"),
                note_id=NOTE_ID,
                native_title="芋泥普洱茶",
            )

    def test_missing_provider_title_uses_request_bound_identity(self):
        self.assertTrue(
            validate_xhs_tiantian_payload(
                provider_payload(title=""),
                note_id=NOTE_ID,
                native_title="芋泥普洱茶",
            )
        )

    def test_author_prefixed_title_corresponds(self):
        self.assertTrue(xhs_tiantian_titles_correspond("标题", "作者_标题"))

    def test_too_many_candidates_are_rejected(self):
        candidates = tuple(
            {"url": TIANTIAN_VIDEO_URL, "quality": "原画", "size": index}
            for index in range(17)
        )
        with self.assertRaises(ProviderResultInvalidError):
            validate_xhs_tiantian_payload(
                provider_payload(videos=candidates),
                note_id=NOTE_ID,
                native_title="芋泥普洱茶",
            )

    def test_highest_quality_candidate_is_selected(self):
        selected = select_xhs_tiantian_video_candidate(provider_payload())
        self.assertEqual(selected["quality"], "原画")

    def test_declared_size_breaks_equal_quality_tie(self):
        payload = provider_payload(
            videos=(
                {"url": TIANTIAN_VIDEO_URL, "quality": "原画", "size": 10},
                {
                    "url": "https://sns-video-hw.xhscdn.com/media/larger.mp4",
                    "quality": "原画",
                    "size": 20,
                },
            )
        )
        self.assertEqual(select_xhs_tiantian_video_candidate(payload)["size"], 20)

    def test_stable_original_order_breaks_equal_score_tie(self):
        first = {"url": TIANTIAN_VIDEO_URL, "quality": "原画", "size": 20}
        second = {
            "url": "https://sns-video-hw.xhscdn.com/media/second.mp4",
            "quality": "原画",
            "size": 20,
        }
        selected = select_xhs_tiantian_video_candidate(
            provider_payload(videos=(first, second))
        )
        self.assertEqual(selected["url"], first["url"])

    def test_normalizer_extracts_only_bounded_note_identity(self):
        normalized = TianTianJieXiResultNormalizer.normalize_payload(
            {
                "noteId": NOTE_ID,
                "source_url": f"https://www.xiaohongshu.com/explore/{NOTE_ID}?secret=redacted",
                "video": TIANTIAN_VIDEO_URL,
            }
        )
        self.assertEqual(normalized["_source_identity_signals"], (NOTE_ID,))
        self.assertNotIn("source_url", normalized)


class XhsTianTianMediaPolicyTests(SimpleTestCase):
    def test_reviewed_exact_host_is_accepted(self):
        self.assertEqual(validate_xhs_tiantian_media_url(TIANTIAN_VIDEO_URL), TIANTIAN_VIDEO_URL)

    def test_lookalike_host_is_rejected(self):
        for url in (
            "https://evil.sns-video-hw.xhscdn.com/media/video.mp4",
            "https://sns-video-hw.xhscdn.com.evil.example/media/video.mp4",
            "https://other.xhscdn.com/media/video.mp4",
        ):
            with self.subTest(url=url), self.assertRaises(XhsTianTianMediaPolicyError):
                validate_xhs_tiantian_media_url(url)

    def test_private_ip_and_localhost_are_rejected(self):
        for url in (
            "https://127.0.0.1/video.mp4",
            "https://localhost/video.mp4",
            "https://[::1]/video.mp4",
        ):
            with self.subTest(url=url), self.assertRaises(XhsTianTianMediaPolicyError):
                validate_xhs_tiantian_media_url(url)

    def test_userinfo_port_trailing_dot_and_empty_path_are_rejected(self):
        for url in (
            "https://user@sns-video-hw.xhscdn.com/media/video.mp4",
            "https://sns-video-hw.xhscdn.com:444/media/video.mp4",
            "https://sns-video-hw.xhscdn.com./media/video.mp4",
            "https://sns-video-hw.xhscdn.com/",
        ):
            with self.subTest(url=url), self.assertRaises(XhsTianTianMediaPolicyError):
                validate_xhs_tiantian_media_url(url)

    def test_mp4_prefix_is_accepted(self):
        self.assertTrue(is_iso_bmff_video_prefix(b"\x00\x00\x00\x18ftypisom"))

    def test_html_json_and_xml_prefixes_are_rejected(self):
        for content in (b"<html>bad", b'{"error":true}', b"<?xml version='1.0'?>"):
            with self.subTest(content=content):
                self.assertFalse(is_iso_bmff_video_prefix(content))

    def test_bounded_probe_accepts_video_and_closes_response(self):
        response = FakeResponse(body=b"\x00\x00\x00\x18ftypisom" + b"x" * 5000)
        result = probe_xhs_tiantian_video(
            TIANTIAN_VIDEO_URL,
            requester=Mock(return_value=response),
        )
        self.assertEqual(result.bytes_read, XHS_TIANTIAN_PROBE_MAX_BYTES)
        self.assertTrue(response.closed)

    def test_bounded_probe_accepts_octet_stream_only_with_mp4_prefix(self):
        response = FakeResponse(
            content_type="application/octet-stream",
            body=b"\x00\x00\x00\x14ftypqt  " + b"x" * 32,
        )
        result = probe_xhs_tiantian_video(
            TIANTIAN_VIDEO_URL,
            requester=Mock(return_value=response),
        )
        self.assertEqual(result.content_type, "application/octet-stream")
        self.assertTrue(response.closed)

        invalid_response = FakeResponse(
            content_type="application/octet-stream",
            body=b"unknown binary payload",
        )
        with self.assertRaises(XhsTianTianMediaPolicyError):
            probe_xhs_tiantian_video(
                TIANTIAN_VIDEO_URL,
                requester=Mock(return_value=invalid_response),
            )
        self.assertTrue(invalid_response.closed)

    def test_bounded_probe_rejects_html_with_video_mime(self):
        response = FakeResponse(body=b"<html>not media</html>")
        with self.assertRaises(XhsTianTianMediaPolicyError):
            probe_xhs_tiantian_video(
                TIANTIAN_VIDEO_URL,
                requester=Mock(return_value=response),
            )
        self.assertTrue(response.closed)

    def test_bounded_probe_rejects_json_mime(self):
        response = FakeResponse(content_type="application/json", body=b'{"ok":false}')
        with self.assertRaises(XhsTianTianMediaPolicyError):
            probe_xhs_tiantian_video(
                TIANTIAN_VIDEO_URL,
                requester=Mock(return_value=response),
            )

    def test_redirect_to_unknown_host_is_rejected(self):
        response = FakeResponse(
            status=302,
            location="https://attacker.example/video.mp4",
        )
        requester = Mock(return_value=response)
        with self.assertRaises(XhsTianTianMediaPolicyError):
            probe_xhs_tiantian_video(TIANTIAN_VIDEO_URL, requester=requester)
        requester.assert_called_once()


class XhsTianTianHybridResultTests(SimpleTestCase):
    def enhance(self, provider=None, controller=None, payload=None):
        provider = provider or FakeProvider(payload=payload)
        controller = controller or XhsUltraAttemptController(
            client_identity="client",
            limit=10,
            consume=lambda *args: SimpleNamespace(allowed=True, retry_after=0),
        )
        with patch(
            "xhs_download.services.platforms.xhs_tiantian_hybrid.provider_configured",
            return_value=True,
        ):
            result = enhance_xhs_ordinary_video(
                native_video_result(),
                provider=provider,
                attempt_controller=controller,
                probe_func=lambda *args, **kwargs: probe_result(),
            )
        return result, provider, controller

    def test_success_preserves_native_title_author_cover_and_filename(self):
        result, _provider, _controller = self.enhance()
        self.assertEqual(result.base_filename, "作者_芋泥普洱茶")
        self.assertEqual(result.metadata["title"], "芋泥普洱茶")
        self.assertEqual(result.metadata["author_name"], "作者")
        self.assertEqual(result.video_assets[0].cover_url, "https://ci.xiaohongshu.com/cover")

    def test_success_uses_high_quality_url_size_and_label(self):
        result, _provider, _controller = self.enhance()
        asset = result.video_assets[0]
        self.assertEqual(asset.url, TIANTIAN_VIDEO_URL)
        self.assertEqual(asset.quality_label, "原画")
        self.assertEqual(asset.size_bytes, 29_143_890)

    def test_success_uses_opaque_asset_identity(self):
        result, _provider, _controller = self.enhance()
        asset = result.video_assets[0]
        self.assertRegex(asset.asset_id, r"^xhs:[0-9a-f]{16}:video:tiantianjiexi$")
        self.assertNotIn(NOTE_ID, asset.asset_id)

    def test_missing_title_records_request_bound_identity(self):
        result, _provider, _controller = self.enhance(payload=provider_payload(title=""))
        self.assertEqual(
            result.metadata["xhs_tiantian_provider_identity_signal"],
            "REQUEST_BOUND_ONLY",
        )

    def test_provider_timeout_returns_native(self):
        result, _provider, _controller = self.enhance(
            provider=FakeProvider(error=XhsProviderTimeoutError("timeout"))
        )
        self.assertEqual(result.video_assets[0].url, NATIVE_VIDEO_URL)
        self.assertEqual(result.metadata["xhs_tiantian_fallback_reason"], "timeout")

    def test_failure_cooldown_returns_native(self):
        class CooldownProvider(FakeProvider):
            def parse(self, url, **kwargs):
                self.calls.append((url, kwargs))
                kwargs["attempt_controller"].skip_fresh_work(
                    "tiantianjiexi",
                    reason="failure_cooldown",
                    retry_after=5,
                )
                raise TianTianJieXiFailureCooldownError(5)

        provider = CooldownProvider()
        controller = XhsUltraAttemptController(
            client_identity="client",
            limit=10,
            consume=lambda *args: SimpleNamespace(allowed=True, retry_after=0),
        )
        result, _provider, _controller = self.enhance(provider=provider, controller=controller)
        self.assertEqual(result.video_assets[0].url, NATIVE_VIDEO_URL)
        self.assertEqual(result.metadata["xhs_tiantian_fallback_reason"], "failure_cooldown")

    def test_open_circuit_returns_native_without_provider_parse(self):
        provider = FakeProvider(circuit_open=True)
        result, _provider, _controller = self.enhance(provider=provider)
        self.assertEqual(result.video_assets[0].url, NATIVE_VIDEO_URL)
        self.assertEqual(provider.calls, [])

    def test_per_client_rate_limit_returns_native(self):
        controller = XhsUltraAttemptController(
            client_identity="client",
            limit=1,
            consume=lambda *args: SimpleNamespace(allowed=False, retry_after=12),
        )
        result, _provider, _controller = self.enhance(controller=controller)
        self.assertEqual(result.video_assets[0].url, NATIVE_VIDEO_URL)
        self.assertEqual(result.metadata["xhs_tiantian_fallback_reason"], "rate_limit")
        self.assertEqual(result.metadata["xhs_tiantian_retry_after_seconds"], 12)

    def test_explicit_identity_mismatch_returns_native(self):
        result, _provider, _controller = self.enhance(
            payload=provider_payload(_source_identity_signals=(OTHER_NOTE_ID,))
        )
        self.assertEqual(result.video_assets[0].url, NATIVE_VIDEO_URL)
        self.assertEqual(result.metadata["xhs_tiantian_fallback_reason"], "identity_mismatch")

    def test_unknown_host_returns_native(self):
        payload = provider_payload(
            videos=({"url": "https://other.xhscdn.com/video.mp4", "quality": "原画", "size": 10},)
        )
        result, _provider, _controller = self.enhance(payload=payload)
        self.assertEqual(result.video_assets[0].url, NATIVE_VIDEO_URL)
        self.assertEqual(result.metadata["xhs_tiantian_fallback_reason"], "host_reject")

    def test_success_records_bounded_probe_and_zero_full_media_bytes(self):
        result, _provider, _controller = self.enhance()
        self.assertEqual(result.metadata["xhs_tiantian_bounded_probe_bytes"], 4096)
        self.assertEqual(result.metadata["xhs_tiantian_full_server_media_bytes"], 0)

    def test_provider_window_is_capped_at_eight_seconds(self):
        provider = FakeProvider()
        controller = XhsUltraAttemptController(
            client_identity="client",
            limit=10,
            consume=lambda *args: SimpleNamespace(allowed=True, retry_after=0),
        )
        started_at = time.monotonic()
        with (
            patch(
                "xhs_download.services.platforms.xhs_tiantian_hybrid.provider_configured",
                return_value=True,
            ),
            patch(
                "xhs_download.services.platforms.xhs_tiantian_hybrid.runtime_default_float",
                return_value=99.0,
            ),
        ):
            result = enhance_xhs_ordinary_video(
                native_video_result(),
                provider=provider,
                attempt_controller=controller,
                probe_func=lambda *args, **kwargs: probe_result(),
            )

        provider_options = provider.calls[0][1]
        self.assertEqual(provider_options["timeout_seconds"], 8.0)
        self.assertLessEqual(
            provider_options["deadline_monotonic"],
            started_at + 8.1,
        )
        self.assertEqual(result.metadata["provider_budget_ms"], 8000)

    def test_payload_cache_reuse_does_not_consume_client_quota(self):
        consume = Mock(return_value=SimpleNamespace(allowed=True, retry_after=0))
        controller = XhsUltraAttemptController(
            client_identity="client",
            limit=1,
            consume=consume,
        )

        class CachedProvider(FakeProvider):
            def parse(self, url, **kwargs):
                self.calls.append((url, kwargs))
                kwargs["attempt_controller"].reuse_cached_result(
                    "tiantianjiexi",
                    reason="payload_cache",
                )
                kwargs["payload_validator"](self.payload)
                return self.payload

        result, provider, _controller = self.enhance(
            provider=CachedProvider(),
            controller=controller,
        )
        self.assertEqual(len(provider.calls), 1)
        consume.assert_not_called()
        self.assertTrue(result.metadata["xhs_tiantian_cache_reused"])
        self.assertFalse(result.metadata["xhs_tiantian_external_call"])

    def test_probe_format_failure_returns_native(self):
        provider = FakeProvider()
        controller = XhsUltraAttemptController(
            client_identity="client",
            limit=10,
            consume=lambda *args: SimpleNamespace(allowed=True, retry_after=0),
        )
        with (
            patch(
                "xhs_download.services.platforms.xhs_tiantian_hybrid.provider_configured",
                return_value=True,
            ),
            self.assertLogs(
                "xhs_download.services.platforms.xhs_tiantian_hybrid",
                level="INFO",
            ) as captured,
        ):
            result = enhance_xhs_ordinary_video(
                native_video_result(),
                provider=provider,
                attempt_controller=controller,
                probe_func=Mock(
                    side_effect=XhsTianTianMediaPolicyError(
                        "XHS_TIANTIAN_FORMAT_REJECTED"
                    )
                ),
            )

        self.assertEqual(result.video_assets[0].url, NATIVE_VIDEO_URL)
        self.assertEqual(
            result.metadata["xhs_tiantian_fallback_reason"],
            "format_reject",
        )
        log_output = "\n".join(captured.output)
        self.assertNotIn(TIANTIAN_VIDEO_URL, log_output)
        self.assertNotIn(NOTE_ID, log_output)
        self.assertNotIn("fixture=1", log_output)


class XhsTianTianManifestAndRoutingTests(SimpleTestCase):
    def test_native_http_payload_url_is_normalized_to_https(self):
        self.assertEqual(
            normalize_video_url(
                "http://sns-video-v28.xhscdn.com/native/video.mp4?fixture=1"
            ),
            "https://sns-video-v28.xhscdn.com/native/video.mp4?fixture=1",
        )
        self.assertIsNone(
            normalize_video_url(
                "http://user@sns-video-v28.xhscdn.com/native/video.mp4"
            )
        )
        self.assertIsNone(
            normalize_video_url(
                "http://sns-video-v28.xhscdn.com:444/native/video.mp4"
            )
        )

    def test_native_browser_video_host_is_exact_and_https_only(self):
        self.assertTrue(
            xhs_native_video_url_is_browser_streaming_allowed(NATIVE_VIDEO_URL)
        )
        self.assertTrue(
            xhs_native_video_url_is_browser_streaming_allowed(
                "https://sns-video-v28.xhscdn.com/native/video.mp4"
            )
        )
        for rejected in (
            "https://other.xhscdn.com/native/video.mp4",
            "https://evil.sns-video-bd.xhscdn.com/native/video.mp4",
            "https://evil-sns-video-v28.xhscdn.com/native/video.mp4",
            "https://sns-video-vx.xhscdn.com/native/video.mp4",
            "https://sns-video-bd.xhscdn.com.evil.example/native/video.mp4",
            "http://sns-video-bd.xhscdn.com/native/video.mp4",
            "https://user@sns-video-bd.xhscdn.com/native/video.mp4",
            "https://sns-video-bd.xhscdn.com:444/native/video.mp4",
            "https://sns-video-bd.xhscdn.com/",
            "https://sns-video-bd.xhscdn.com/live/index.m3u8",
        ):
            self.assertFalse(
                xhs_native_video_url_is_browser_streaming_allowed(rejected),
                rejected,
            )

    def test_valid_manifest_is_accepted(self):
        item = make_manifest_item()
        self.assertTrue(xhs_tiantian_manifest_item_is_allowed(item))
        self.assertTrue(provider_manifest_allows_public_download(item))

    def test_manifest_url_tampering_is_rejected(self):
        item = make_manifest_item()
        item["url"] = "https://sns-video-hw.xhscdn.com/media/tampered.mp4"
        self.assertFalse(xhs_tiantian_manifest_item_is_allowed(item))

    def test_wrong_asset_id_is_rejected(self):
        item = make_manifest_item()
        item["asset_id"] = "xhs:wrong:video:tiantianjiexi"
        self.assertFalse(xhs_tiantian_manifest_item_is_allowed(item))

    def test_wrong_cache_key_is_rejected(self):
        item = make_manifest_item()
        item["provider_cache_key"] = "third_party:tiantianjiexi_generic:wrong"
        self.assertFalse(xhs_tiantian_manifest_item_is_allowed(item))

    def test_signed_url_validator_uses_xhs_policy(self):
        item = make_manifest_item()
        self.assertEqual(
            validate_provider_signed_download_url(item, item["url"], "blocked"),
            item["url"],
        )

    def test_douyin_recovery_identity_does_not_accept_xhs_item(self):
        self.assertIsNone(get_tiantianjiexi_manifest_identity(make_manifest_item()))

    def test_valid_item_is_browser_streaming_and_not_server_proxy(self):
        item = make_manifest_item()
        self.assertTrue(item_can_browser_direct_video(item))
        self.assertFalse(single_video_can_server_proxy_download(item))

    def test_provider_strategy_is_zero_relay_browser_streaming(self):
        item = make_manifest_item()
        asset = MediaAsset(
            media_type="video",
            url=item["url"],
            platform="xiaohongshu",
            provider=item["provider"],
        )
        strategy = build_download_strategy(
            asset,
            result_platform="xiaohongshu",
            result_metadata={
                "selected_provider": "tiantianjiexi_generic",
                "xhs_tiantian_hybrid_status": "success",
            },
        )
        self.assertEqual(strategy["download_strategy"], DOWNLOAD_STRATEGY_BROWSER_STREAMING)
        self.assertTrue(strategy["browser_direct_fetch_safe"])

    def test_native_provider_strategy_is_zero_relay_browser_streaming(self):
        strategy = build_download_strategy(
            native_video_result().video_assets[0],
            result_platform="xiaohongshu",
            result_metadata=native_video_result().metadata,
        )
        self.assertEqual(
            strategy["download_strategy"], DOWNLOAD_STRATEGY_BROWSER_STREAMING
        )
        self.assertEqual(strategy["fallback_strategy"], "browser_blob")
        self.assertTrue(strategy["browser_direct_fetch_safe"])

    def test_native_fallback_marker_keeps_zero_relay_browser_streaming(self):
        fallback = native_video_result(
            selected_provider="native",
            xhs_tiantian_hybrid_status="fallback",
            xhs_tiantian_fallback_reason="timeout",
        )
        strategy = build_download_strategy(
            fallback.video_assets[0],
            result_platform="xiaohongshu",
            result_metadata=fallback.metadata,
        )
        self.assertEqual(
            strategy["download_strategy"], DOWNLOAD_STRATEGY_BROWSER_STREAMING
        )
        self.assertEqual(strategy["fallback_strategy"], "browser_blob")
        self.assertTrue(strategy["browser_direct_fetch_safe"])

        display_item = build_result_display_context(fallback)["video_items"][0]
        self.assertTrue(display_item["browser_direct_fetch_safe"])

    def test_native_display_item_carries_server_generated_platform_identity(self):
        item = build_result_display_context(native_video_result())["video_items"][0]
        self.assertEqual(item["source_platform"], "xiaohongshu")
        self.assertTrue(item["browser_direct_fetch_safe"])

    @patch("xhs_download.services.download_routing.DownloadRuntimeSetting.load")
    def test_native_xhs_auto_route_has_zero_server_egress(self, load_setting):
        load_setting.return_value = SimpleNamespace(
            visitor_video_download_enabled=True,
            staff_large_download_exempt=False,
            single_video_bytes_limit=0,
            single_archive_bytes_limit=0,
        )
        item = build_result_display_context(native_video_result())["video_items"][0]
        item["url"] = item.pop("video_url")
        route = decide_download_route(
            RequestFactory().post("/media/download-route/"),
            [item],
            requested_mode="auto",
        )
        self.assertEqual(route["route_type"], ROUTE_DIRECT)
        self.assertEqual(route["server_egress_estimated_bytes"], 0)
        self.assertEqual(
            route["items"][0]["source_platform"], "xiaohongshu"
        )

    @patch("xhs_download.services.download_routing.DownloadRuntimeSetting.load")
    def test_native_xhs_server_proxy_request_is_blocked(self, load_setting):
        load_setting.return_value = SimpleNamespace(
            visitor_video_download_enabled=True,
            staff_large_download_exempt=False,
            single_video_bytes_limit=0,
            single_archive_bytes_limit=0,
        )
        route = decide_download_route(
            RequestFactory().post("/media/download-route/"),
            [{
                "type": "video",
                "url": NATIVE_VIDEO_URL,
                "source_platform": "xiaohongshu",
            }],
            requested_mode="server_single",
        )
        self.assertEqual(route["route_type"], ROUTE_BLOCKED)
        self.assertEqual(route["error_code"], "ORDINARY_BROWSER_DOWNLOAD_REQUIRED")

    @patch("xhs_download.services.download_routing.DownloadRuntimeSetting.load")
    def test_providerless_non_xhs_video_keeps_existing_server_route(self, load_setting):
        load_setting.return_value = SimpleNamespace(
            visitor_video_download_enabled=True,
            staff_large_download_exempt=False,
            single_video_bytes_limit=0,
            single_archive_bytes_limit=0,
        )
        route = decide_download_route(
            RequestFactory().post("/media/download-route/"),
            [{
                "type": "video",
                "url": "https://media.example/video.mp4",
                "source_platform": "other",
            }],
            requested_mode="auto",
        )
        self.assertEqual(route["route_type"], "proxy")

    @patch("xhs_download.services.download_routing.DownloadRuntimeSetting.load")
    def test_auto_route_has_zero_server_egress(self, load_setting):
        load_setting.return_value = SimpleNamespace(
            visitor_video_download_enabled=True,
            staff_large_download_exempt=False,
            single_video_bytes_limit=0,
            single_archive_bytes_limit=0,
        )
        route = decide_download_route(
            RequestFactory().post("/media/download-route/"),
            [make_manifest_item()],
            requested_mode="auto",
        )
        self.assertEqual(route["route_type"], ROUTE_DIRECT)
        self.assertEqual(route["server_egress_estimated_bytes"], 0)

    @patch("xhs_download.services.download_routing.DownloadRuntimeSetting.load")
    def test_server_proxy_request_is_blocked_for_ordinary_video(self, load_setting):
        load_setting.return_value = SimpleNamespace(
            visitor_video_download_enabled=True,
            staff_large_download_exempt=False,
            single_video_bytes_limit=0,
            single_archive_bytes_limit=0,
        )
        route = decide_download_route(
            RequestFactory().post("/media/download-route/"),
            [make_manifest_item()],
            requested_mode="proxy",
        )
        self.assertEqual(route["route_type"], ROUTE_BLOCKED)
        self.assertEqual(route["error_code"], "ORDINARY_BROWSER_DOWNLOAD_REQUIRED")


class XhsTianTianPresentationAndBatchTests(SimpleTestCase):
    @staticmethod
    def context(*, staff=False):
        return {
            "show_internal_diagnostics": staff,
            "page_platform_id": "xiaohongshu",
            "image_items": [],
            "video_items": [],
        }

    @staticmethod
    def display_context():
        return {
            "result_theme": "xhs",
            "platform_name": "小红书",
            "post_type": "video",
            "base_filename": "video",
            "display_title": "title",
            "display_author": "author",
            "note_id": NOTE_ID,
            "metadata_source": "structured",
            "image_items": [],
            "video_items": [{"video_url": TIANTIAN_VIDEO_URL}],
        }

    @patch("xhs_download.views.build_result_display_context")
    def test_public_success_notice_does_not_name_provider(self, display):
        display.return_value = self.display_context()
        result = SimpleNamespace(
            platform="xiaohongshu",
            metadata={"xhs_tiantian_hybrid_status": "success"},
            rich_media=None,
        )
        context = self.context(staff=False)
        apply_search_result_to_context(
            context, result, platform_key="xhs", prefer_ultra_hd=True
        )
        self.assertEqual(context["provider_notice"], "已获取小红书原画 / 超高清资源。")
        self.assertNotIn("天天", context["provider_notice"])

    @patch("xhs_download.views.build_result_display_context")
    def test_public_fallback_notice_is_sanitized(self, display):
        display.return_value = self.display_context()
        result = SimpleNamespace(
            platform="xiaohongshu",
            metadata={
                "xhs_tiantian_hybrid_status": "fallback",
                "xhs_tiantian_fallback_reason": "host_reject",
            },
            rich_media=None,
        )
        context = self.context(staff=False)
        apply_search_result_to_context(
            context, result, platform_key="xhs", prefer_ultra_hd=True
        )
        self.assertEqual(
            context["provider_notice"],
            "超高清解析暂不可用，已返回普通清晰度。",
        )
        self.assertNotIn("host", context["provider_notice"])

    @patch("xhs_download.views.build_result_display_context")
    def test_staff_fallback_notice_keeps_fixed_stage_only(self, display):
        display.return_value = self.display_context()
        result = SimpleNamespace(
            platform="xiaohongshu",
            metadata={
                "xhs_tiantian_hybrid_status": "fallback",
                "xhs_tiantian_fallback_reason": "format_reject",
            },
            rich_media=None,
        )
        context = self.context(staff=True)
        apply_search_result_to_context(
            context, result, platform_key="xhs", prefer_ultra_hd=True
        )
        self.assertIn("format_reject", context["provider_notice"])
        self.assertNotIn("http", context["provider_notice"])

    def test_batch_ultra_passes_xhs_attempt_controller(self):
        captured = {}

        def parser(source, **kwargs):
            captured.update(kwargs)
            return native_video_result(
                selected_provider="native",
                xhs_tiantian_hybrid_status="fallback",
            )

        row = build_batch_result_row(
            RequestFactory().post("/batch/", REMOTE_ADDR="127.0.0.2"),
            f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
            prefer_ultra_hd=True,
            prefer_youtube_merge=False,
            per_link_timeout_seconds=12,
            parser=parser,
            display_context_builder=lambda result, platform_key: {
                "platform": result.platform,
                "platform_name": "小红书",
                "post_type": result.post_type,
                "base_filename": result.base_filename,
                "display_title": "title",
                "display_author": "author",
                "image_items": [],
                "video_items": [{"url": NATIVE_VIDEO_URL}],
            },
        )
        self.assertEqual(row["status"], "success")
        self.assertIsInstance(captured["ultra_attempt_controller"], XhsUltraAttemptController)
        self.assertEqual(row["provider_notice"], "超高清解析暂不可用，已返回普通清晰度。")

    def test_batch_image_result_keeps_no_provider_notice(self):
        row = build_batch_result_row(
            RequestFactory().post("/batch/", REMOTE_ADDR="127.0.0.3"),
            f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
            prefer_ultra_hd=True,
            prefer_youtube_merge=False,
            per_link_timeout_seconds=12,
            parser=lambda *args, **kwargs: native_image_result(),
            display_context_builder=lambda result, platform_key: {
                "platform": result.platform,
                "platform_name": "小红书",
                "post_type": result.post_type,
                "base_filename": result.base_filename,
                "display_title": "gallery",
                "display_author": "",
                "image_items": [{"url": "https://ci.xiaohongshu.com/image"}],
                "video_items": [],
            },
        )
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["provider_notice"], "")
