"""Offline regressions through the real budget, scheduler, registry and XHS coordinator."""

from contextlib import ExitStack
from copy import deepcopy
from threading import Event, current_thread
import time
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from .models import ProviderAccessMode, ProviderRuntimeSetting
from .services.assets import MediaAsset, ParseResult
from .services.batch_parse.results import build_batch_result_rows
from .services.batch_parse.runtime import calculate_batch_parse_budget
from .services.common import XhsParseError
from .services.platforms import xhs_independent
from .services.platforms.xiaohongshu import XiaohongshuAdapter
from .views import build_result_display_context


LINKS = tuple("https://www.xiaohongshu.com/explore/%024d" % i for i in (1, 2))


def native_image(url):
    note_id = url.rsplit("/", 1)[-1]
    return ParseResult(
        platform="xiaohongshu", post_type="image", base_filename=note_id,
        assets=(MediaAsset(media_type="image", platform="xiaohongshu",
                           url="https://ci.xiaohongshu.com/notes_pre_post/%s.jpg" % note_id,
                           asset_id=note_id, ext="jpg"),),
        metadata={"source_provider": "native", "note_id": note_id},
    )


@override_settings(
    CACHES={"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache",
                        "LOCATION": "xhs-batch-deadline-offline"}},
    THIRD_PARTY_PROVIDER_ENABLED=True,
    THIRD_PARTY_RATE_LIMIT_PER_MINUTE=1000,
    THIRD_PARTY_GLOBAL_REQUEST_LIMIT_PER_MINUTE=1000,
)
class XhsBatchDeadlineTests(SimpleTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        cache.clear()
        self.addCleanup(cache.clear)
        self.network_guards = [self.stack.enter_context(patch(target,
            side_effect=AssertionError("Network forbidden in deadline regression")))
            for target in ("socket.getaddrinfo", "socket.gethostbyname",
                           "socket.create_connection", "socket.socket.connect",
                           "requests.sessions.Session.request")]
        self.addCleanup(self.assert_no_network)
        # Unsaved configuration isolates the DB without replacing authorization.
        self.config = ProviderRuntimeSetting(third_party_mode=ProviderAccessMode.STAFF,
            third_party_blocked_platforms="upstream:tiantianjiexi_generic")
        self.stack.enter_context(patch.object(ProviderRuntimeSetting, "load",
                                             return_value=self.config))
        self.stack.enter_context(patch.object(xhs_independent, "service_configured",
                                             return_value=True))
        self.native = self.stack.enter_context(patch.object(XiaohongshuAdapter, "parse",
            side_effect=lambda url, **kwargs: native_image(url)))
        self.service = self.stack.enter_context(patch.object(xhs_independent, "execute_provider",
            side_effect=XhsParseError("synthetic optional service failure")))
        self.request = RequestFactory().get("/batch/", REMOTE_ADDR="192.0.2.1")
        self.request.user = SimpleNamespace(is_active=True, is_authenticated=True, is_staff=True)

    def assert_no_network(self):
        for guard in self.network_guards:
            guard.assert_not_called()

    def batch(self, links=LINKS, *, workers=1, per_item=1, grace=1, response=1.5,
              display=build_result_display_context):
        budget = calculate_batch_parse_budget(link_count=len(links), worker_count=workers,
            per_link_timeout_seconds=per_item, configured_total_timeout_seconds=60,
            completion_grace_seconds=grace, max_response_timeout_seconds=response)
        start = time.monotonic()
        rows = build_batch_result_rows(self.request, links,
            prefer_ultra_hd=False, prefer_youtube_merge=False,
            deadline=start + budget.scheduling_timeout_seconds,
            response_deadline=start + budget.max_response_timeout_seconds,
            parallel_workers=budget.worker_count,
            per_link_timeout_seconds=budget.per_link_timeout_seconds,
            scheduling_timeout_seconds=budget.scheduling_timeout_seconds,
            max_response_timeout_seconds=budget.max_response_timeout_seconds,
            completion_grace_seconds=budget.completion_grace_seconds,
            row_builder_kwargs={"display_context_builder": display})
        return rows, time.monotonic() - start, budget

    @staticmethod
    def observed(rows):
        return [(row["status"], len(row.get("image_items", [])),
                 row.get("error_message", "")) for row in rows]

    def test_serial_optional_wait_preserves_native_before_hard_response_deadline(self):
        self.config.third_party_mode = ProviderAccessMode.DISABLED
        control, control_elapsed, _ = self.batch()
        self.assertEqual(self.observed(control), [("success", 1, ""), ("success", 1, "")])
        self.service.assert_not_called()
        self.native.reset_mock()
        self.config.third_party_mode = ProviderAccessMode.STAFF
        native_done = {url: Event() for url in LINKS}
        service_done = {url: Event() for url in LINKS}
        supplied_timeouts = []
        service_conditions = []

        def native(url, **kwargs):
            result = native_image(url)
            native_done[url].set()
            return result

        def service(provider, url, *, timeout_seconds, service_authorized):
            try:
                service_conditions.append((service_authorized, native_done[url].wait(.3)))
                supplied_timeouts.append((url, timeout_seconds))
                Event().wait(.6 if url == LINKS[0] else timeout_seconds)
                raise XhsParseError("synthetic optional service failure")
            finally:
                service_done[url].set()

        self.native.side_effect = native
        self.service.side_effect = service
        try:
            rows, elapsed, budget = self.batch()
            evidence = "control=%r (%.3fs); independent=%r (%.3fs); timeouts=%r" % (
                self.observed(control), control_elapsed, self.observed(rows), elapsed,
                supplied_timeouts)
            self.assertEqual(budget.max_response_timeout_seconds, 1.5)
            self.assertEqual(budget.scheduling_timeout_seconds, 1)
            self.assertEqual(self.native.call_count, 2, evidence)
            self.assertEqual(self.service.call_count, 2, evidence)
            self.assertEqual(service_conditions, [(True, True), (True, True)], evidence)
            self.assertTrue(all(event.is_set() for event in native_done.values()), evidence)
            self.assertLess(elapsed, 1.85, evidence)
            self.assertEqual(self.observed(rows), [("success", 1, ""), ("success", 1, "")], evidence)
            self.assertEqual([row["source_text"] for row in rows], list(LINKS))
            for row, url in zip(rows, LINKS):
                self.assertEqual(row["image_items"][0]["url"], native_image(url).assets[0].url)
        finally:
            for event in service_done.values():
                self.assertTrue(event.wait(2), "Synthetic service did not drain")

    def test_multiworker_preserves_order_and_isolates_native_and_service_failure(self):
        links = LINKS + ("https://www.xiaohongshu.com/explore/%024d" % 3,)
        third_started = Event()
        synchronization = []
        completion_order = []

        def native(url, **kwargs):
            if url == links[0]:
                synchronization.append(third_started.wait(.7))
            elif url == links[1]:
                raise XhsParseError("synthetic native failure")
            completion_order.append(url)
            if url == links[2]:
                third_started.set()
            return native_image(url)

        self.native.side_effect = native
        rows, elapsed, _ = self.batch(links, workers=2, response=2.5)
        self.assertEqual(synchronization, [True])
        self.assertEqual(completion_order, [links[2], links[0]])
        self.assertEqual([row["source_text"] for row in rows], list(links))
        self.assertEqual([row["status"] for row in rows], ["success", "failed", "success"])
        self.assertEqual([len(row.get("image_items", [])) for row in rows], [1, 0, 1])
        self.assertEqual(self.native.call_count, 3)
        self.assertEqual(self.service.call_count, 3)
        self.assertLess(elapsed, 1.5)

    def test_completed_display_uses_only_configured_item_grace(self):
        display_done = Event()

        def display(result, **kwargs):
            try:
                Event().wait(.7)
                return build_result_display_context(result, **kwargs)
            finally:
                display_done.set()

        for grace, expected in ((.5, "success"), (0, "timeout")):
            with self.subTest(grace=grace):
                display_done.clear()
                try:
                    rows, elapsed, _ = self.batch(LINKS[:1], per_item=.5,
                        grace=grace, response=2, display=display)
                finally:
                    self.assertTrue(display_done.wait(2), "Synthetic display did not drain")
                self.assertEqual(rows[0]["status"], expected, self.observed(rows))
                self.assertEqual(len(rows[0].get("image_items", [])), int(expected == "success"))
                self.assertLess(elapsed, 1.3)

    def test_hard_response_detaches_worker_and_late_result_cannot_replace_timeout(self):
        self.config.third_party_mode = ProviderAccessMode.DISABLED
        release = Event()
        started = Event()
        worker_threads = []

        def native(url, **kwargs):
            worker_threads.append(current_thread())
            started.set()
            release.wait(3)
            return native_image(url)

        self.native.side_effect = native
        try:
            rows, elapsed, budget = self.batch(response=1)
            self.assertTrue(started.is_set())
            self.assertEqual([row["status"] for row in rows], ["timeout", "skipped"])
            self.assertEqual(len(rows[0].get("image_items", [])), 0)
            self.assertEqual(budget.max_response_timeout_seconds, 1)
            self.assertGreaterEqual(elapsed, .9)
            self.assertLess(elapsed, 1.4)
            snapshot = deepcopy(rows)
        finally:
            release.set()
            for worker in worker_threads:
                worker.join(2)
                self.assertFalse(worker.is_alive(), "Synthetic batch worker did not drain")
        self.assertEqual(rows, snapshot)
        self.native.assert_called_once()
        self.service.assert_not_called()
