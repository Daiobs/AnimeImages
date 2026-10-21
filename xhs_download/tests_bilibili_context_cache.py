"""Process-local anonymous context reuse through the offline HTTP boundary."""

from collections import Counter
from contextlib import ExitStack
from email.message import Message
import threading
import time
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from django.test import SimpleTestCase

from contracts.network import offline_requests
from .services.common import XhsDownloadError, XhsParseError
from .services.platforms import bilibili
from .tests_bilibili_wbi_context import (
    LEGACY_VIEW, MEDIA, NAV, NOW, PLAY, SOURCE, SPI, WBI_VIEW,
    ProductionShapeTransport, json_response,
)


COLD_PATHS = ['/', SPI, NAV, WBI_VIEW, PLAY]
REBUILD_PATHS = [WBI_VIEW, '/', SPI, NAV, WBI_VIEW, PLAY]


class ContextCacheTransport(ProductionShapeTransport):
    max_view_calls = 2

    def __init__(self):
        super().__init__()
        self.view_outcomes = []
        self.view_calls = 0

    def send(self, request, **kwargs):
        # Bound regressions before an accidental refresh loop can hang the suite.
        if len(self.requests) >= 12:
            raise AssertionError('Anonymous context exceeded the HTTP request budget')
        if urlsplit(request.url).path == WBI_VIEW:
            self.view_calls += 1
            if self.view_calls > self.max_view_calls:
                raise AssertionError('Anonymous context exceeded the view request budget')
            if self.view_outcomes:
                self.wbi_status, self.wbi_code = self.view_outcomes.pop(0)
        return super().send(request, **kwargs)


class InterleavedContextTransport(ContextCacheTransport):
    max_view_calls = 4

    def __init__(self, held_path, outcome=(200, 0), *, update_cookies=False):
        super().__init__()
        self.held_path = held_path
        self.outcome = outcome
        self.update_cookies = update_cookies
        self.entered = threading.Event()
        self.release = threading.Event()
        self.response_lock = threading.Lock()

    def send(self, request, **kwargs):
        path = urlsplit(request.url).path
        held = (threading.current_thread().name == 'held-parse'
                and path == self.held_path and not self.entered.is_set())
        with self.response_lock:
            self.wbi_status, self.wbi_code = self.outcome if held else (200, 0)
            response = super().send(request, **kwargs)
            if self.update_cookies and path == WBI_VIEW:
                response.cookies.set(
                    'buvid3', 'offline-late-buvid3' if held else 'offline-current-buvid3',
                    domain='.bilibili.com', path='/', secure=True,
                )
        # Hold only the response, allowing another parse to finish its own HTTP path.
        if held:
            self.entered.set()
            if not self.release.wait(timeout=3):
                raise AssertionError('Timed out waiting to release the held HTTP response')
        return response


class BilibiliAnonymousContextCacheTests(SimpleTestCase):
    def setUp(self):
        super().setUp()
        bilibili.clear_anonymous_context_cache()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.transport = ContextCacheTransport()
        self.stack.enter_context(offline_requests(self.transport.send))
        self.clock = self.stack.enter_context(patch.object(
            bilibili, '_context_clock', return_value=1000.0,
        ))
        # Replace only this module's time binding; HTTP deadlines keep real time.
        self.module_time = self.stack.enter_context(patch.object(
            bilibili, 'time', wraps=time,
        ))
        self.module_time.time.return_value = NOW
        self.http_get = self.stack.enter_context(patch.object(
            bilibili, 'request_get_with_retry', wraps=bilibili.request_get_with_retry,
        ))
        self.deadline = time.monotonic() + 10

    def tearDown(self):
        try:
            bilibili.clear_anonymous_context_cache()
        finally:
            super().tearDown()

    def parse(self):
        result = bilibili.BilibiliAdapter().parse_url(
            SOURCE, deadline_monotonic=self.deadline,
        )
        self.assertEqual(result.video_assets[0].url, MEDIA)
        for asset in result.video_assets:
            self.assertNotIn('cookie', {name.lower() for name in asset.headers})
        self.assertNotIn('cookie', {name.lower() for name in bilibili.BILIBILI_HEADERS})
        self.assertNotIn('offline-home-buvid3', str(result))
        self.assertNotIn('offline-spi-buvid4', str(result))
        return result

    def paths(self):
        return [urlsplit(request.url).path for request in self.transport.requests]

    def reset_observations(self):
        self.transport.requests.clear()
        self.transport.view_calls = 0
        self.http_get.reset_mock()

    def warm_context(self):
        self.parse()
        self.assertEqual(self.paths(), COLD_PATHS)
        self.reset_observations()
        self.clock.return_value = 1001.0

    def assert_original_deadline(self):
        self.assertTrue(self.http_get.call_args_list)
        for call in self.http_get.call_args_list:
            self.assertEqual(call.kwargs['deadline_monotonic'], self.deadline)
            self.assertGreater(call.kwargs['timeout'], 0)
            self.assertLessEqual(call.kwargs['timeout'], 10)

    def start_parse(self, name):
        results, errors = [], []

        def run():
            try:
                results.append(self.parse())
            except Exception as error:
                errors.append(error)

        worker = threading.Thread(target=run, name=name, daemon=True)
        worker.start()
        return worker, results, errors

    def assert_workers_succeeded(self, workers):
        for worker, results, errors in workers:
            self.assertFalse(worker.is_alive(), 'Parse worker did not exit within its deadline')
            if errors:
                raise errors[0]
            self.assertEqual(len(results), 1)

    def test_second_parse_reuses_cookie_and_wbi_without_media_cookies(self):
        self.warm_context()
        self.parse()
        self.assertEqual(self.paths(), [WBI_VIEW, PLAY])
        for request in self.transport.requests:
            cookie = request.headers.get('Cookie', '')
            self.assertIn('buvid3=offline-home-buvid3', cookie)
            self.assertIn('b_nut=', cookie)
            self.assertIn('buvid4=offline-spi-buvid4', cookie)
        self.assert_original_deadline()

    def test_cookie_expires_at_600_seconds_while_wbi_remains_cached(self):
        self.warm_context()
        self.clock.return_value = 1600.0
        self.parse()
        self.assertEqual(self.paths(), ['/', SPI, WBI_VIEW, PLAY])
        self.assert_original_deadline()

    def test_wbi_expires_at_3600_seconds_without_discarding_fresh_cookies(self):
        self.warm_context()
        self.clock.return_value = 4599.0
        self.parse()
        self.assertEqual(self.paths(), ['/', SPI, WBI_VIEW, PLAY])
        self.reset_observations()
        self.clock.return_value = 4600.0
        self.parse()
        self.assertEqual(self.paths(), [NAV, WBI_VIEW, PLAY])
        self.assert_original_deadline()

    def test_cached_wbi_keys_recalculate_timestamp_and_signature_for_each_parse(self):
        self.parse()
        first = next(request for request in self.transport.requests
                     if urlsplit(request.url).path == WBI_VIEW)
        first_query = parse_qs(urlsplit(first.url).query)
        self.reset_observations()
        self.clock.return_value = 1001.0
        self.module_time.time.return_value = NOW + 1
        self.parse()
        self.assertEqual(self.paths(), [WBI_VIEW, PLAY])
        second_query = parse_qs(urlsplit(self.transport.requests[0].url).query)
        self.assertEqual(first_query, {
            'bvid': ['BV1J4JX6PEMV'], 'wts': [str(NOW)],
            'w_rid': ['e45c9b1c9d50f5a6966a0ba3787743a4'],
        })
        self.assertEqual(second_query, {
            'bvid': ['BV1J4JX6PEMV'], 'wts': [str(NOW + 1)],
            'w_rid': ['a341502d171a5343700e736abbcaa817'],
        })

    def assert_cached_failure_recovers(self, outcome):
        self.warm_context()
        self.transport.view_outcomes = [outcome, (200, 0)]
        self.parse()
        self.assertEqual(self.paths(), REBUILD_PATHS)
        self.assertEqual(self.transport.view_calls, 2)
        self.assertEqual(self.transport.view_outcomes, [])
        self.assert_original_deadline()

    def test_cached_http_412_rebuilds_once_with_original_deadline(self):
        self.assert_cached_failure_recovers((412, -412))

    def test_cached_signature_minus_403_rebuilds_once_with_original_deadline(self):
        self.assert_cached_failure_recovers((200, -403))

    def assert_failed_rebuild_preserves_fallback_error(self, first, last):
        self.warm_context()
        self.transport.view_outcomes = [first, last]
        errors = []
        original_api_get_json = bilibili.api_get_json

        def observe_api_error(url, *args, **kwargs):
            try:
                return original_api_get_json(url, *args, **kwargs)
            except (XhsDownloadError, XhsParseError) as error:
                if urlsplit(url).path == WBI_VIEW:
                    errors.append(error)
                raise

        with patch.object(bilibili, 'api_get_json', side_effect=observe_api_error):
            with self.assertRaises(XhsDownloadError) as raised:
                self.parse()
        self.assertEqual(self.paths(), REBUILD_PATHS[:-1])
        self.assertEqual(self.transport.view_calls, 2)
        self.assertEqual(len(errors), 2)
        if first[0] == 412:
            self.assertIs(raised.exception, errors[0])
            self.assertIs(raised.exception.__cause__, errors[-1])
            self.assertIsInstance(raised.exception.__cause__, XhsParseError)
        else:
            self.assertIs(raised.exception, errors[-1])
            self.assertIsNot(raised.exception, errors[0])
        self.assertEqual(self.transport.view_outcomes, [])
        self.assertEqual(raised.exception.http_status, 412)
        self.assert_original_deadline()

    def test_rebuilt_http_412_preserves_last_error_without_another_rebuild(self):
        self.assert_failed_rebuild_preserves_fallback_error(
            (200, -403), (412, -412),
        )

    def test_rebuilt_signature_failure_preserves_first_412_with_last_error_as_cause(self):
        self.assert_failed_rebuild_preserves_fallback_error(
            (412, -412), (200, -403),
        )

    def test_late_same_generation_cookie_revocation_is_not_lost_after_publish(self):
        self.warm_context()
        first = bilibili.anonymous_cookie_context(deadline_monotonic=self.deadline)
        late = bilibili.anonymous_cookie_context(deadline_monotonic=self.deadline)
        generation = first._bilibili_generation
        self.assertIs(late._bilibili_generation, generation)
        self.assertIs(first._bilibili_snapshot, late._bilibili_snapshot)
        params = {'bvid': 'BV1J4JX6PEMV'}
        bilibili.api_get_json(
            'https://api.bilibili.com' + WBI_VIEW, params,
            cookies=first, deadline_monotonic=self.deadline,
        )
        self.assertIsNot(first._bilibili_snapshot, late._bilibili_snapshot)
        self.assertEqual(first.get_dict(), late.get_dict())

        def revoke_cookie(request, **kwargs):
            response = self.transport.send(request, **kwargs)
            if urlsplit(request.url).path == WBI_VIEW:
                headers = Message()
                headers.add_header(
                    'Set-Cookie', 'buvid3=; Domain=.bilibili.com; Path=/; Max-Age=0',
                )
                response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
            return response

        with offline_requests(revoke_cookie):
            bilibili.api_get_json(
                'https://api.bilibili.com' + WBI_VIEW, params,
                cookies=late, deadline_monotonic=self.deadline,
            )
        self.assertNotIn('buvid3', late.get_dict())
        self.assertTrue(generation.is_set(), 'Same-value Cookie revocation was lost')
        with self.assertRaises(XhsParseError):
            bilibili.api_get_json(
                'https://api.bilibili.com' + PLAY, {'cid': 456},
                cookies=first, deadline_monotonic=self.deadline,
            )
        self.assertEqual(self.paths(), [WBI_VIEW, WBI_VIEW])
        self.reset_observations()
        self.parse()
        self.assertEqual(self.paths(), COLD_PATHS)
        self.assert_original_deadline()

    def test_revoked_generation_blocks_its_first_creator_before_any_more_http(self):
        creator = bilibili.anonymous_cookie_context(deadline_monotonic=self.deadline)
        params = bilibili.signed_view_params(
            {'bvid': 'BV1J4JX6PEMV'}, creator, deadline_monotonic=self.deadline,
        )
        self.assertFalse(creator._bilibili_reused)
        peer = bilibili.anonymous_cookie_context(deadline_monotonic=self.deadline)
        self.assertIs(creator._bilibili_generation, peer._bilibili_generation)
        self.transport.view_outcomes = [(412, -412)]
        with self.assertRaises(XhsDownloadError):
            bilibili.api_get_json(
                'https://api.bilibili.com' + WBI_VIEW, params,
                cookies=peer, deadline_monotonic=self.deadline,
            )
        self.assertTrue(creator._bilibili_generation.is_set())
        self.assertFalse(creator._bilibili_reused)
        with self.assertRaises(XhsParseError):
            bilibili.api_get_json(
                'https://api.bilibili.com' + WBI_VIEW, params,
                cookies=creator, deadline_monotonic=self.deadline,
            )
        with self.assertRaises(XhsParseError):
            bilibili.signed_view_params(
                {'bvid': 'BV1J4JX6PEMV'}, creator, deadline_monotonic=self.deadline,
            )
        self.assertEqual(self.paths(), ['/', SPI, NAV, WBI_VIEW])
        self.assertEqual(self.transport.view_calls, 1)
        self.assert_original_deadline()

    def test_fresh_optional_failures_preserve_compatibility_without_seeding_cache(self):
        for endpoint in (SPI, NAV):
            for status in (200, 412):
                with self.subTest(endpoint=endpoint, status=status):
                    bilibili.clear_anonymous_context_cache()
                    self.transport = ContextCacheTransport()
                    self.transport.legacy_status = 200

                    def optional_failure(request, **kwargs):
                        response = self.transport.send(request, **kwargs)
                        if urlsplit(request.url).path == endpoint:
                            response.close()
                            return json_response(request, {'code': -412}, status=status)
                        return response

                    view = LEGACY_VIEW if endpoint == NAV else WBI_VIEW
                    with offline_requests(optional_failure):
                        self.parse()
                        self.assertEqual(self.paths(), ['/', SPI, NAV, view, PLAY])
                        self.reset_observations()
                        self.clock.return_value = 1001.0
                        self.parse()
                    self.assertEqual(self.paths(), ['/', SPI, NAV, view, PLAY])
                    self.assert_original_deadline()

    def test_parallel_cold_parses_initialize_home_spi_and_nav_only_once(self):
        self.transport = InterleavedContextTransport('/')
        second_entered = threading.Event()
        original_context = bilibili.anonymous_cookie_context

        def observe_context(**kwargs):
            if threading.current_thread().name == 'parallel-parse':
                second_entered.set()
            return original_context(**kwargs)

        workers = []
        with offline_requests(self.transport.send), patch.object(
            bilibili, 'anonymous_cookie_context', side_effect=observe_context,
        ):
            try:
                workers.append(self.start_parse('held-parse'))
                self.assertTrue(self.transport.entered.wait(timeout=3),
                                'First parse did not reach the homepage')
                workers.append(self.start_parse('parallel-parse'))
                self.assertTrue(second_entered.wait(timeout=3),
                                'Second parse did not enter context initialization')
            finally:
                self.transport.release.set()
                for worker, _, _ in workers:
                    worker.join(timeout=10)
            self.assert_workers_succeeded(workers)
        self.assertEqual(Counter(self.paths()), Counter({
            '/': 1, SPI: 1, NAV: 1, WBI_VIEW: 2, PLAY: 2,
        }))
        self.assert_original_deadline()

    def test_late_cookie_responses_cannot_overwrite_or_revoke_current_context(self):
        cases = (
            ('new_generation_update', 'clear', (200, 0)),
            ('new_generation_http_412', 'clear', (412, -412)),
            ('new_generation_signature_failure', 'clear', (200, -403)),
            ('same_generation_new_snapshot', 'none', (200, 0)),
            ('ttl_generation_late_http_412', 'ttl', (412, -412)),
        )
        for name, refresh, outcome in cases:
            with self.subTest(case=name):
                bilibili.clear_anonymous_context_cache()
                self.clock.return_value = 1000.0
                self.deadline = time.monotonic() + 10
                self.transport = InterleavedContextTransport(
                    WBI_VIEW, outcome, update_cookies=True,
                )
                workers = []
                with offline_requests(self.transport.send):
                    self.warm_context()
                    old_generation = bilibili._context_generation
                    try:
                        workers.append(self.start_parse('held-parse'))
                        self.assertTrue(self.transport.entered.wait(timeout=3),
                                        'Old parse did not reach the view response')
                        if refresh == 'clear':
                            bilibili.clear_anonymous_context_cache()
                        elif refresh == 'ttl':
                            self.clock.return_value = 1600.0
                        self.parse()
                        new_generation = bilibili._context_generation
                        if refresh != 'none':
                            self.assertIsNot(old_generation, new_generation)
                        self.assertFalse(new_generation.is_set())
                        refresh_paths = {
                            'clear': COLD_PATHS,
                            'ttl': ['/', SPI, WBI_VIEW, PLAY],
                            'none': [WBI_VIEW, PLAY],
                        }
                        ready_paths = [WBI_VIEW] + refresh_paths[refresh]
                        self.assertEqual(self.paths(), ready_paths)
                        ready_count = len(self.transport.requests)
                    finally:
                        self.transport.release.set()
                        for worker, _, _ in workers:
                            worker.join(timeout=10)
                    self.assert_workers_succeeded(workers)
                    self.assertIs(bilibili._context_generation, new_generation)
                    self.assertFalse(new_generation.is_set())
                    if refresh == 'ttl':
                        self.assertTrue(old_generation.is_set())
                    self.parse()
                late_paths = ([WBI_VIEW, PLAY] if refresh != 'none' else [PLAY])
                self.assertEqual(self.paths(), ready_paths + late_paths + [WBI_VIEW, PLAY])
                for request in self.transport.requests[ready_count:]:
                    if urlsplit(request.url).path == WBI_VIEW:
                        self.assertIn('buvid3=offline-current-buvid3',
                                      request.headers.get('Cookie', ''))
                self.assert_original_deadline()
