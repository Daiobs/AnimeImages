"""Controlled raw boundaries only: no upstream calls, database, or new captures."""
from concurrent.futures import Future, ThreadPoolExecutor
from copy import deepcopy
from dataclasses import replace
from datetime import date
import json
from pathlib import Path
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, override_settings

from .services.assets import MediaAsset, ParseResult
from .services.candidates import MediaCandidate
from .services.douyin_continuation import DouyinContinuationCoordinator
from .services.pipeline import Failure, FailureKind, Work
from .services.pipeline_shadow_capture import bind_capture, collect_raw
from .services.platforms import bilibili, douyin, douyin_kukutool, douyin_providers
from .services.platforms import tiantianjiexi_generic as tiantian
from .services.platforms import xhs_independent, xhs_tiantian_hybrid, xiaohongshu
from .services.provider_adapters.bilibili import BilibiliAdapter, BilibiliMapper
from .services.provider_adapters.douyin import DouyinAdapter, DouyinMapper
from .services.provider_adapters.kukutool import KukutoolAdapter, KukutoolSidecarAdapter
from .services.provider_adapters.xiaohongshu import XiaohongshuAdapter


ROOT = Path(__file__).parent / "fixtures/provider_raw"
AWEME_ID = "7000000000000000001"
DOUYIN = f"https://www.douyin.com/video/{AWEME_ID}"
MEDIA = "https://v26-default.365yg.com/controlled.mp4"


def fixture(provider, name):
    document = json.loads((ROOT / provider / (name + ".json")).read_text())
    assert document["provenance"]["kind"] == "synthetic"
    return document


def response(*, raw=None, text="", url=""):
    return SimpleNamespace(json=Mock(return_value=raw), text=text, url=url,
                           close=Mock(), raise_for_status=Mock(), ok=True,
                           content=b"controlled", status_code=200, headers={})


def aweme():
    return {"aweme_id": AWEME_ID, "desc": "Controlled native title",
            "video": {"play_addr": {"url_list": [MEDIA]}, "duration": 12000},
            "upstream_only": {"untouched": [1, 2]}}


@override_settings(CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache"}})
class NativeRawHookTests(SimpleTestCase):
    def setUp(self):
        for target in ("requests.sessions.Session.request", "socket.getaddrinfo", "socket.socket.connect"):
            self.enterContext(patch(target, side_effect=AssertionError("unexpected network")))

    def bili_boundaries(self, document):
        raw = document["raw"]
        self.enterContext(patch.object(bilibili, "anonymous_cookie_context", return_value=None))
        self.enterContext(patch.object(bilibili, "signed_view_params", return_value=None))
        view = response(raw=raw["view"])
        play = response(raw=raw["playurl"])
        request = self.enterContext(patch.object(bilibili, "request_get_with_retry", side_effect=[view, play]))
        return request, view, play

    def test_bilibili_keeps_complete_envelopes_and_executed_selection_without_extra_calls(self):
        document = fixture("bilibili_native", "synthetic_complete_page2")
        request, view, play = self.bili_boundaries(document)
        baseline = bilibili.BilibiliAdapter().parse_url(document["source_url"])
        request.side_effect = [view, play]
        with collect_raw() as session:
            actual = bilibili.BilibiliAdapter().parse_url(document["source_url"])
        self.assertEqual(actual, baseline)
        self.assertEqual(request.call_count, 4)
        self.assertEqual(view.json.call_count, 2)
        self.assertEqual(play.json.call_count, 2)
        self.assertEqual(len(session.records), 1)
        captured = session.records[0]
        self.assertEqual(captured.raw["view"], document["raw"]["view"])
        self.assertEqual(captured.raw["playurl"], document["raw"]["playurl"])
        self.assertEqual(captured.raw["selection"]["page"], 2)
        self.assertEqual(captured.raw["selection"]["playurl_params"]["cid"],
                         document["raw"]["selection"]["playurl_params"]["cid"])
        self.assertIsInstance(BilibiliMapper().map(captured.raw, captured.source_url), Work)
        self.assertNotIn("Cookie", repr(captured.metadata))

    def test_bilibili_fetch_maps_segmented_raw_even_when_legacy_raises(self):
        document = fixture("bilibili_native", "synthetic_multi_durl")
        request, _, _ = self.bili_boundaries(document)
        adapter = BilibiliAdapter()
        raw = adapter.fetch(document["source_url"], 2)
        self.assertEqual(raw["playurl"], document["raw"]["playurl"])
        outcome = adapter.map(raw, document["source_url"])
        self.assertEqual(outcome, Failure(FailureKind.UNSUPPORTED, "BILIBILI_SEGMENTED_DURL"))
        self.assertEqual(request.call_count, 2)

    def test_native_fetch_no_raw_never_reissues_a_request(self):
        for adapter, legacy, source, code in (
            (BilibiliAdapter(), bilibili.BilibiliAdapter, "https://www.bilibili.com/video/BV1J4JX6PEMV", "BILIBILI_NO_RAW"),
            (DouyinAdapter(), douyin.DouyinAdapter, DOUYIN, "DOUYIN_NO_RAW"),
            (XiaohongshuAdapter(), xiaohongshu.XiaohongshuAdapter, "https://www.xiaohongshu.com/explore/64a1b2c3d4e5f6789012abcd", "XHS_NO_RAW"),
        ):
            with self.subTest(provider=adapter.name), patch.object(legacy, "parse_url", return_value=object()) as parse:
                self.assertEqual(adapter.fetch(source, 2), Failure(FailureKind.INVALID_RESULT, code))
                parse.assert_called_once()
                self.assertEqual(parse.call_args.kwargs["timeout_seconds"], 2)
                for budget in (0, -1, float("nan"), float("inf")):
                    self.assertIs(adapter.fetch(source, budget).kind, FailureKind.TIMEOUT)
                parse.assert_called_once()

    def test_douyin_feed_keeps_raw_before_projection_and_result_is_unchanged(self):
        raw = {"status_code": 0, "aweme_list": [aweme()], "upstream_envelope": ["kept"]}
        reply = response(raw=raw)
        with patch.object(douyin, "request_get_with_retry", return_value=reply) as get:
            baseline = douyin.DouyinAdapter().fetch_mobile_feed_result(AWEME_ID)
            with collect_raw() as session:
                actual = douyin.DouyinAdapter().fetch_mobile_feed_result(AWEME_ID)
        self.assertEqual(actual, baseline)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(len(session.records), 1)
        self.assertEqual(session.records[0].raw, raw)
        self.assertIsInstance(DouyinMapper().map(session.records[0].raw, DOUYIN), Work)
        raw["upstream_envelope"].append("later mutation")
        self.assertEqual(session.records[0].raw["upstream_envelope"], ["kept"])

    def test_douyin_short_fetch_preserves_resolved_identity_and_rejects_conflict(self):
        reply = response(raw={"status_code": 0, "aweme_list": [aweme()]})
        def legacy_parse(adapter, source, **options):
            return adapter.fetch_mobile_feed_result(AWEME_ID, capture_source_url=DOUYIN)
        with patch.object(douyin, "request_get_with_retry", return_value=reply) as get, patch.object(
            douyin.DouyinAdapter, "parse_url", legacy_parse
        ):
            adapter = DouyinAdapter()
            raw = adapter.fetch("https://v.douyin.com/controlled/", 2)
        self.assertEqual(raw["page_url"], DOUYIN)
        self.assertIsInstance(adapter.map(raw, "https://v.douyin.com/controlled/"), Work)
        conflict = adapter.map(raw, "https://www.douyin.com/video/7000000000000000002")
        self.assertIs(conflict.kind, FailureKind.INVALID_RESULT)
        get.assert_called_once()

    def test_xhs_fetch_preserves_html_and_actual_redirect_url(self):
        document = fixture("xhs_native", "synthetic_video")
        raw = document["raw"]
        html = raw.get("html") or "<script>window.__INITIAL_STATE__=" + json.dumps(raw.get("page_state", raw)) + "</script>"
        reply = response(text=html, url=document["source_url"])
        with patch.object(xiaohongshu, "request_get_with_retry", return_value=reply) as get:
            fetched = XiaohongshuAdapter().fetch("https://xhslink.com/controlled", 2)
        self.assertEqual(fetched, {"html": html, "page_url": document["source_url"]})
        self.assertIsInstance(XiaohongshuAdapter().map(fetched, "https://xhslink.com/controlled"), Work)
        get.assert_called_once()
        reply.close.assert_called_once()

    def test_tiantian_captures_before_existing_observer_without_interfering(self):
        raw = {"code": 200, "data": {"title": "Controlled", "url": MEDIA}, "unknown": [1]}
        reply = response(raw=raw)
        observer = Mock(side_effect=lambda value: value["unknown"].append(2))
        client = tiantian.TianTianJieXiApiClient(session=SimpleNamespace(post=Mock(return_value=reply)), observe=observer)
        with patch.object(tiantian, "api_configured", return_value=True), collect_raw() as session:
            result = client.parse_url(DOUYIN, timeout_seconds=1)
        observer.assert_called_once_with(raw)
        self.assertEqual(session.records[0].raw["unknown"], [1])
        self.assertNotEqual(result, session.records[0].raw)
        client.session.post.assert_called_once()

    def test_sidecar_capture_is_before_conversion_and_fetch_uses_that_raw(self):
        document = fixture("kukutool_generic", "synthetic_sidecar_best")
        raw = document["raw"]
        reply = response(raw=raw)
        with patch.object(douyin_kukutool.requests, "post", return_value=reply) as post:
            with collect_raw() as session:
                converted = douyin_kukutool.run_kukutool_sidecar(AWEME_ID, base_url="http://127.0.0.1:9999")
            self.assertEqual(session.records[0].raw, raw)
            self.assertIn("_sidecar", converted)
            with patch.object(douyin_kukutool.subprocess, "run", side_effect=AssertionError("sidecar must not launch generic helper")):
                fetched = KukutoolSidecarAdapter(base_url="http://127.0.0.1:9999").fetch(DOUYIN, 1)
        self.assertEqual(fetched, raw)
        self.assertEqual(post.call_count, 2)
        self.assertEqual(post.call_args.args, ("http://127.0.0.1:9999/v1/ultra/resolve",))
        self.assertEqual(post.call_args.kwargs["json"], {"aweme_id": AWEME_ID})
        self.assertEqual(post.call_args.kwargs["timeout"], 1)
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertIsInstance(KukutoolAdapter().map(fetched, DOUYIN), Work)

    def test_sidecar_fetch_is_explicit_bounded_and_does_not_fallback(self):
        import requests

        adapter = KukutoolSidecarAdapter(base_url="http://127.0.0.1:9999")
        with patch.object(douyin_kukutool.requests, "post", side_effect=requests.Timeout("controlled timeout")) as post:
            self.assertEqual(adapter.fetch(DOUYIN, 0.5), Failure(FailureKind.TIMEOUT, "KUKUTOOL_TIMEOUT"))
            post.assert_called_once()
            for budget in (0, 0.01, -1, float("nan"), float("inf")):
                self.assertIs(adapter.fetch(DOUYIN, budget).kind, FailureKind.TIMEOUT)
            self.assertIs(adapter.fetch("https://v.douyin.com/controlled", 1).kind, FailureKind.UNSUPPORTED)
            self.assertIs(adapter.fetch("https://example.test/video/" + AWEME_ID, 1).kind, FailureKind.UNSUPPORTED)
            post.assert_called_once()

    def test_sidecar_fetch_retains_business_error_raw_and_observed_http_failure(self):
        raw = {"contract_version": "douyin-source-resolver.v1", "status": "error", "reason": "controlled"}
        reply = response(raw=raw)
        adapter = KukutoolSidecarAdapter(base_url="http://127.0.0.1:9999")
        with patch.object(douyin_kukutool.requests, "post", return_value=reply) as post:
            self.assertEqual(adapter.fetch(DOUYIN, 1), raw)
            reply.ok = False
            reply.status_code = 404
            self.assertIs(adapter.fetch(DOUYIN, 1).kind, FailureKind.NOT_FOUND)
        self.assertEqual(post.call_count, 2)

    def test_copy_failure_is_fail_open_and_does_not_repeat_tiantian(self):
        class Uncopyable:
            def __deepcopy__(self, memo):
                raise ValueError("controlled copy failure")
        raw = {"code": 200, "data": {"url": MEDIA}, "unknown": Uncopyable()}
        client = tiantian.TianTianJieXiApiClient(session=SimpleNamespace(post=Mock(return_value=response(raw=raw))))
        with patch.object(tiantian, "api_configured", return_value=True), collect_raw() as session:
            self.assertTrue(client.parse_url(DOUYIN))
        self.assertEqual(session.records, ())
        self.assertEqual(session.errors, {"tiantianjiexi_generic"})
        client.session.post.assert_called_once()

    def test_real_provider_executor_preserves_native_capture_after_scope_exit(self):
        entered, release = Event(), Event()
        reply = response(raw={"status_code": 0, "aweme_list": [aweme()]})
        def native_call():
            entered.set()
            if not release.wait(2):
                raise AssertionError("worker gate timed out")
            return douyin.DouyinAdapter().fetch_mobile_feed_result(AWEME_ID)
        with ThreadPoolExecutor(max_workers=1) as executor, patch.object(
            douyin_kukutool, "get_provider_executor", return_value=executor
        ), patch.object(douyin_kukutool, "PROVIDER_CAPACITY", {}), patch.object(
            douyin, "request_get_with_retry", return_value=reply
        ):
            with collect_raw() as first:
                future = douyin_kukutool.submit_provider_task("controlled-native", 1, 1, native_call)
                self.assertTrue(entered.wait(2))
            with collect_raw() as unrelated:
                release.set()
                self.assertTrue(future.result(timeout=2).assets)
        self.assertEqual(len(first.records), 1)
        self.assertEqual(first.records[0].provider, "douyin_native")
        self.assertEqual(unrelated.records, ())

    def test_xhs_independent_native_worker_inherits_capture(self):
        document = fixture("xhs_native", "synthetic_video")
        raw = document["raw"]
        html = raw.get("html") or "<script>window.__INITIAL_STATE__=" + json.dumps(raw.get("page_state", raw)) + "</script>"
        with patch.object(xiaohongshu, "request_get_with_retry", return_value=response(text=html, url=document["source_url"])), patch.object(
            xhs_independent, "service_configured", return_value=True
        ), patch.object(xhs_independent, "execute_provider", return_value=None), collect_raw() as session:
            result = xhs_independent.parse_independent(
                document["source_url"], provider_ids=("tiantianjiexi_generic",), timeout_seconds=2,
                native_parse=lambda deadline: xiaohongshu.XiaohongshuAdapter().parse_url(document["source_url"], deadline_monotonic=deadline),
            )
        self.assertTrue(result.assets)
        self.assertEqual([record.provider for record in session.records], ["xhs_native"])

    def test_late_bound_continuation_captures_only_the_adopted_prepared_result(self):
        native = douyin_providers.build_external_parse_result(
            douyin.DouyinAdapter(), douyin_providers.DouyinParseContext(url=DOUYIN, aweme_id=AWEME_ID),
            MediaCandidate(url=MEDIA, quality_label="1080P", size_bytes=4096),
            provider_name="tiantianjiexi", base_filename="Controlled service title",
        )
        prepared = replace(native, metadata={**native.metadata, "prepared": True})
        coordinator = DouyinContinuationCoordinator(2, first_upgrade_wins=True)
        future = Future()
        with collect_raw() as session:
            future.add_done_callback(bind_capture(lambda done: coordinator.capture_future(
                done, provider_name="native", score_result=lambda result: (1, 1),
                is_strong_result=lambda result: True, prepare_result=lambda result: prepared,
            )))
        with collect_raw() as unrelated:
            future.set_result(native)
            discarded = Future()
            discarded.set_result(replace(native, base_filename="not adopted"))
            coordinator.capture_future(discarded, provider_name="late", score_result=lambda result: (9, 9),
                                       is_strong_result=lambda result: True, prepare_result=lambda result: result)
        self.assertIs(session.result, prepared)
        self.assertIs(coordinator.get_best_result(), prepared)
        self.assertIsNone(unrelated.result)

    def test_generic_helper_captures_decrypted_raw_without_profile_or_normalization(self):
        raw = {"title": "Controlled generic", "url": MEDIA, "unknown": {"all": [1, 2]}}
        process = SimpleNamespace(returncode=0, stdout=json.dumps({"result": raw, "profile": {}}), stderr="")
        with patch.object(douyin_kukutool, "get_kukutool_runtime_status", return_value={
            "checked": True, "available": True, "node_binary": "/controlled/node"
        }), patch.object(douyin_kukutool.subprocess, "run", return_value=process) as run, collect_raw() as session:
            actual = douyin_kukutool.run_kukutool_url_helper(DOUYIN, timeout_seconds=1)
        self.assertEqual(actual, raw)
        self.assertEqual(session.records[0].raw, raw)
        self.assertEqual(session.records[0].source_url, DOUYIN)
        self.assertEqual(session.records[0].metadata["transport"], "url_helper")
        run.assert_called_once()

    def test_xhs_native_metadata_and_service_media_have_explicit_legacy_sources(self):
        from .tests_xhs_tiantian_hybrid import FakeProvider, native_video_result, probe_result

        native = native_video_result()
        before = deepcopy(native)
        with patch.object(xhs_tiantian_hybrid, "xhs_tiantian_provider_is_available", return_value=True), collect_raw() as session:
            result = xhs_tiantian_hybrid.enhance_xhs_ordinary_video(
                native, provider=FakeProvider(), probe_func=lambda *args, **kwargs: probe_result(),
            )
        self.assertEqual(native, before)
        self.assertEqual(result.metadata["xhs_tiantian_hybrid_status"], "success")
        self.assertEqual(session.legacy_sources[id(result)], {
            "title": "xhs_native", "cover": "xhs_native", "items": "tiantianjiexi_generic",
            "quality": "tiantianjiexi_generic", "size": "tiantianjiexi_generic",
        })
        self.assertIsNone(session.result)

    def test_douyin_merge_sources_survive_existing_metadata_replacements(self):
        native = douyin.DouyinAdapter().parse_aweme(aweme())
        native = replace(native, assets=(replace(native.assets[0], cover_url="https://p3.douyinpic.com/controlled.jpg"),))
        context = douyin_providers.DouyinParseContext(url=DOUYIN, aweme_id=AWEME_ID)
        candidate = MediaCandidate(url=MEDIA, quality_label="1080P", size_bytes=4096)
        before = deepcopy(native)
        with collect_raw() as session:
            result = douyin_providers.build_external_parse_result(
                douyin.DouyinAdapter(), context, candidate, native,
                provider_name="tiantianjiexi", base_filename="Service title",
            )
            result = douyin_providers.with_parse_metadata(result, selected_provider="tiantianjiexi")
        self.assertEqual(native, before)
        self.assertEqual(session.legacy_sources[id(result)], {
            "title": "douyin_native", "cover": "douyin_native", "items": "tiantianjiexi_generic",
            "quality": "tiantianjiexi_generic", "size": "tiantianjiexi_generic",
        })
        self.assertNotIn("legacy_sources", result.metadata)
        self.assertIsNone(session.result)

    def test_public_comparison_waits_for_continuation_adoption_not_only_future_done(self):
        from .services import pipeline_shadow

        selected = douyin_providers.build_external_parse_result(
            douyin.DouyinAdapter(), douyin_providers.DouyinParseContext(url=DOUYIN, aweme_id=AWEME_ID),
            MediaCandidate(url=MEDIA, quality_label="1080P", size_bytes=4096),
            provider_name="tiantianjiexi", base_filename="Controlled late result",
        )
        coordinator = DouyinContinuationCoordinator(1, first_upgrade_wins=True)
        worker_release, preparing, adopt, persisted = Event(), Event(), Event(), Event()
        saved = []

        def provider_call():
            if not worker_release.wait(2):
                raise AssertionError("provider gate timed out")
            return selected

        def prepare(result):
            preparing.set()
            if not adopt.wait(2):
                raise AssertionError("adoption gate timed out")
            return result

        def persist(session, day):
            saved.append(session.result)
            persisted.set()

        with ThreadPoolExecutor(max_workers=1) as executor, patch.object(
            douyin_kukutool, "get_provider_executor", return_value=executor
        ), patch.object(douyin_kukutool, "PROVIDER_CAPACITY", {}), patch.object(
            pipeline_shadow, "persist_public", side_effect=persist
        ):
            with collect_raw() as session:
                session.started = True
                future = douyin_kukutool.submit_provider_task("controlled-late", 1, 1, provider_call)
                future.add_done_callback(bind_capture(lambda done: coordinator.capture_future(
                    done, provider_name="tiantianjiexi", score_result=lambda result: (1, 1),
                    is_strong_result=lambda result: True, prepare_result=prepare,
                )))
            try:
                worker_release.set()
                self.assertTrue(preparing.wait(2))
                self.assertTrue(future.done())
                pipeline_shadow.finish_public(session, date(2026, 10, 26))
                self.assertFalse(persisted.is_set())
            finally:
                adopt.set()
            self.assertTrue(persisted.wait(2))
            self.assertFalse(pipeline_shadow.wait_for_shadow(timeout=2))
        self.assertEqual(saved, [selected])

    def test_xhs_independent_records_actual_candidate_provider_without_mutating_inputs(self):
        native = ParseResult(platform="xiaohongshu", post_type="video", base_filename="Controlled native",
                             assets=(MediaAsset(media_type="video", url="https://sns-video-bd.xhscdn.com/native.mp4",
                                                platform="xiaohongshu", cover_url="https://sns-webpic-qc.xhscdn.com/cover.jpg"),),
                             metadata={"title": "Controlled native"})
        service = ParseResult(platform="xiaohongshu", post_type="unknown", base_filename="Controlled candidates",
                              assets=(), metadata={"upstream_candidates": [{"media_type": "video", "controlled": True}]})
        before = deepcopy((native, service))
        with patch.object(xhs_independent, "service_configured", return_value=True), patch.object(
            xhs_independent, "execute_provider", return_value=service
        ), collect_raw() as session:
            result = xhs_independent.parse_independent(
                "https://www.xiaohongshu.com/explore/64a1b2c3d4e5f6789012abcd",
                native_parse=lambda deadline: native, provider_ids=("kukutool_generic",), timeout_seconds=2,
            )
        self.assertEqual((native, service), before)
        self.assertEqual(session.legacy_sources[id(result)], {
            "title": "xhs_native", "cover": "xhs_native", "items": "xhs_native", "candidates": "kukutool_generic",
        })

    def test_hybrid_source_hook_failure_keeps_success_instead_of_native_fallback(self):
        from .tests_xhs_tiantian_hybrid import FakeProvider, native_video_result, probe_result

        native = native_video_result()
        provider = FakeProvider()
        with patch.object(xhs_tiantian_hybrid, "xhs_tiantian_provider_is_available", return_value=True), patch.object(
            xhs_tiantian_hybrid, "capture_legacy_sources", side_effect=RuntimeError("controlled hook failure")
        ), collect_raw() as session:
            result = xhs_tiantian_hybrid.enhance_xhs_ordinary_video(
                native, provider=provider, probe_func=lambda *args, **kwargs: probe_result(),
            )
        self.assertEqual(result.metadata["xhs_tiantian_hybrid_status"], "success")
        self.assertNotEqual(result.assets[0].url, native.assets[0].url)
        self.assertEqual(len(provider.calls), 1)
        self.assertEqual(session.errors, {"tiantianjiexi_generic"})

    def test_independent_source_field_failure_keeps_selected_native_result(self):
        class BrokenCover:
            media_type = "video"
            url = "https://sns-video-bd.xhscdn.com/controlled.mp4"

            @property
            def cover_url(self):
                raise ValueError("controlled source-field failure")

        native = ParseResult(platform="xiaohongshu", post_type="video", base_filename="Controlled",
                             assets=(BrokenCover(),), metadata={"title": "Controlled"})
        service = ParseResult(platform="xiaohongshu", post_type="unknown", base_filename="Controlled",
                              assets=(), metadata={"upstream_candidates": [{"media_type": "video"}]})
        with patch.object(xhs_independent, "service_configured", return_value=True), patch.object(
            xhs_independent, "execute_provider", return_value=service
        ), collect_raw() as session:
            result = xhs_independent.parse_independent(
                "https://www.xiaohongshu.com/explore/64a1b2c3d4e5f6789012abcd", native_parse=lambda deadline: native,
                provider_ids=("kukutool_generic",), timeout_seconds=2,
            )
        self.assertEqual(result.assets, native.assets)
        self.assertEqual(result.metadata["xhs_independent_source"], "native")
        self.assertEqual(session.errors, {"xhs_native"})
