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

    def __init__(self, held_path, outcome=(200, 0), *, update_cookies=False,
                 distinct_contexts=False):
        super().__init__()
        self.held_path = held_path
        self.outcome = outcome
        self.update_cookies = update_cookies
        self.distinct_contexts = distinct_contexts
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
            if self.distinct_contexts:
                late = threading.current_thread().name == 'held-parse'
                if path == '/':
                    response.cookies.set(
                        'buvid3', 'offline-late-context' if late else 'offline-winning-context',
                        domain='.bilibili.com', path='/', secure=True,
                    )
                elif path == NAV:
                    response.close()
                    image, sub = ('a', 'b') if late else ('c', 'd')
                    response = json_response(request, {'code': -101, 'data': {'wbi_img': {
                        'img_url': f'https://i0.hdslb.com/bfs/wbi/{image * 32}.png',
                        'sub_url': f'https://i0.hdslb.com/bfs/wbi/{sub * 32}.png',
                    }}})
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
        return self.start_operation(name, self.parse)

    def start_operation(self, name, operation):
        results, errors = [], []

        def run():
            try:
                results.append(operation())
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

    def test_parallel_parse_finishes_before_blocked_home_spi_or_nav_response(self):
        for held_path in ('/', SPI, NAV):
            with self.subTest(held_path=held_path):
                bilibili.clear_anonymous_context_cache()
                self.deadline = time.monotonic() + 10
                self.transport = InterleavedContextTransport(held_path)
                self.reset_observations()
                workers = []
                with offline_requests(self.transport.send):
                    try:
                        workers.append(self.start_parse('held-parse'))
                        self.assertTrue(self.transport.entered.wait(timeout=3),
                                        'First parse did not reach the held HTTP response')
                        second = self.start_parse('parallel-parse')
                        workers.append(second)
                        second[0].join(timeout=1)
                        self.assertFalse(
                            second[0].is_alive(),
                            'Second parse waited behind a blocked initialization response',
                        )
                        self.assertFalse(self.transport.release.is_set())
                        self.assert_workers_succeeded([second])
                    finally:
                        self.transport.release.set()
                        for worker, _, _ in workers:
                            worker.join(timeout=10)
                    self.assert_workers_succeeded(workers)
                self.assertEqual(Counter(self.paths())[WBI_VIEW], 2)
                self.assertEqual(Counter(self.paths())[PLAY], 2)
                self.assert_original_deadline()

    def test_first_completed_cold_context_and_wbi_survive_late_initialization(self):
        for held_path in ('/', SPI, NAV):
            with self.subTest(held_path=held_path):
                bilibili.clear_anonymous_context_cache()
                self.deadline = time.monotonic() + 10
                self.transport = InterleavedContextTransport(
                    held_path, distinct_contexts=True,
                )
                workers = []
                with offline_requests(self.transport.send):
                    try:
                        workers.append(self.start_parse('held-parse'))
                        self.assertTrue(self.transport.entered.wait(timeout=3))
                        second = self.start_parse('parallel-parse')
                        workers.append(second)
                        second[0].join(timeout=1)
                        self.assert_workers_succeeded([second])
                        self.assertFalse(self.transport.release.is_set())
                        winner_generation = bilibili._context_generation
                        winner_cookies = bilibili._cookie_cache.get_dict()
                        winner_mixin = bilibili._wbi_cache
                        winner_expiry = (bilibili._cookie_expires, bilibili._wbi_expires)
                        winner_query = parse_qs(urlsplit(next(
                            request.url for request in self.transport.requests
                            if urlsplit(request.url).path == WBI_VIEW
                        )).query)
                        if held_path != NAV:
                            self.assertEqual(winner_cookies['buvid3'], 'offline-winning-context')
                    finally:
                        self.transport.release.set()
                        for worker, _, _ in workers:
                            worker.join(timeout=10)
                    self.assert_workers_succeeded(workers)
                    self.assertIs(bilibili._context_generation, winner_generation)
                    self.assertFalse(winner_generation.is_set())
                    self.assertEqual(bilibili._cookie_cache.get_dict(), winner_cookies)
                    self.assertEqual(bilibili._wbi_cache, winner_mixin)
                    self.assertEqual((bilibili._cookie_expires, bilibili._wbi_expires), winner_expiry)
                    self.reset_observations()
                    self.parse()
                    self.assertEqual(self.paths(), [WBI_VIEW, PLAY])
                    self.assertEqual(parse_qs(urlsplit(self.transport.requests[0].url).query),
                                     winner_query)
                    self.assertIn(f"buvid3={winner_cookies['buvid3']}",
                                  self.transport.requests[0].headers['Cookie'])

    def test_busy_cache_lock_uses_private_context_without_waiting_or_publishing(self):
        for warm in (False, True):
            with self.subTest(warm=warm):
                bilibili.clear_anonymous_context_cache()
                self.deadline = time.monotonic() + 10
                self.transport = ContextCacheTransport()
                with offline_requests(self.transport.send):
                    if warm:
                        self.warm_context()
                original_generation = bilibili._context_generation
                original_cookies = bilibili._cookie_cache
                original_mixin = bilibili._wbi_cache
                self.transport = InterleavedContextTransport(None, distinct_contexts=True)
                worker = None
                with offline_requests(self.transport.send):
                    try:
                        with bilibili._context_lock:
                            worker = self.start_parse('private-parse')
                            worker[0].join(timeout=1)
                            self.assert_workers_succeeded([worker])
                            self.assertEqual(self.paths(), COLD_PATHS)
                            self.assertIn('buvid3=offline-winning-context',
                                          self.transport.requests[-2].headers['Cookie'])
                            self.assertIs(bilibili._context_generation, original_generation)
                            self.assertIs(bilibili._cookie_cache, original_cookies)
                            self.assertEqual(bilibili._wbi_cache, original_mixin)
                    finally:
                        if worker:
                            worker[0].join(timeout=10)
                self.assert_workers_succeeded([worker])
                self.assert_original_deadline()

    def test_api_cookie_publication_does_not_wait_for_busy_cache_lock(self):
        self.warm_context()
        cookies = bilibili.anonymous_cookie_context(deadline_monotonic=self.deadline)
        snapshot = bilibili._cookie_cache
        self.transport = InterleavedContextTransport(None, update_cookies=True)
        worker = None
        with offline_requests(self.transport.send):
            try:
                with bilibili._context_lock:
                    worker = self.start_operation('api-parse', lambda: bilibili.api_get_json(
                        'https://api.bilibili.com' + WBI_VIEW,
                        {'bvid': 'BV1J4JX6PEMV'}, cookies=cookies,
                        deadline_monotonic=self.deadline,
                    ))
                    worker[0].join(timeout=1)
                    self.assert_workers_succeeded([worker])
                    self.assertEqual(worker[1][0]['cid'], 456)
                    self.assertEqual(cookies.get('buvid3'), 'offline-current-buvid3')
                    self.assertIs(bilibili._cookie_cache, snapshot)
                    self.assertEqual(self.paths(), [WBI_VIEW])
            finally:
                if worker:
                    worker[0].join(timeout=10)
        self.assert_workers_succeeded([worker])

    def test_signing_uses_local_nav_when_cache_lock_is_busy(self):
        self.warm_context()
        cookies = bilibili.anonymous_cookie_context(deadline_monotonic=self.deadline)
        original_mixin = bilibili._wbi_cache
        original_expiry = bilibili._wbi_expires
        self.transport = InterleavedContextTransport(None, distinct_contexts=True)
        worker = None
        with offline_requests(self.transport.send):
            try:
                with bilibili._context_lock:
                    worker = self.start_operation('signed-parse', lambda: bilibili.signed_view_params(
                        {'bvid': 'BV1J4JX6PEMV'}, cookies,
                        deadline_monotonic=self.deadline,
                    ))
                    worker[0].join(timeout=1)
                    self.assert_workers_succeeded([worker])
                    self.assertIn('w_rid', worker[1][0])
                    self.assertNotEqual(worker[1][0]['w_rid'], 'e45c9b1c9d50f5a6966a0ba3787743a4')
                    self.assertEqual(self.paths(), [NAV])
                    self.assertEqual(bilibili._wbi_cache, original_mixin)
                    self.assertEqual(bilibili._wbi_expires, original_expiry)
            finally:
                if worker:
                    worker[0].join(timeout=10)
        self.assert_workers_succeeded([worker])

    def test_private_and_old_generation_do_not_borrow_or_invalidate_shared_wbi(self):
        for context_kind in ('private', 'old_generation'):
            with self.subTest(context_kind=context_kind):
                bilibili.clear_anonymous_context_cache()
                self.deadline = time.monotonic() + 10
                self.clock.return_value = 1000.0
                self.transport = ContextCacheTransport()
                with offline_requests(self.transport.send):
                    self.warm_context()
                    if context_kind == 'private':
                        worker = None
                        try:
                            with bilibili._context_lock:
                                worker = self.start_operation('private-context', lambda:
                                    bilibili.anonymous_cookie_context(deadline_monotonic=self.deadline))
                                worker[0].join(timeout=1)
                                self.assert_workers_succeeded([worker])
                                cookies = worker[1][0]
                        finally:
                            if worker:
                                worker[0].join(timeout=10)
                    else:
                        cookies = bilibili.anonymous_cookie_context(deadline_monotonic=self.deadline)
                        self.clock.return_value = 1600.0
                        bilibili.anonymous_cookie_context(deadline_monotonic=self.deadline)
                shared_generation = bilibili._context_generation
                shared_cookies = bilibili._cookie_cache
                shared_mixin = bilibili._wbi_cache
                self.assertIsNot(cookies._bilibili_generation, shared_generation)
                self.transport = InterleavedContextTransport(None, distinct_contexts=True)
                with offline_requests(self.transport.send):
                    signed = bilibili.signed_view_params(
                        {'bvid': 'BV1J4JX6PEMV'}, cookies, deadline_monotonic=self.deadline,
                    )
                    self.assertEqual(self.paths(), [NAV])
                    self.assertIn('w_rid', signed)
                    self.assertNotEqual(signed['w_rid'], 'e45c9b1c9d50f5a6966a0ba3787743a4')
                    self.assertEqual(bilibili._wbi_cache, shared_mixin)
                    self.assertIs(bilibili._cookie_cache, shared_cookies)
                    self.transport.view_outcomes = [(412, -412)]
                    with self.assertRaises(XhsDownloadError):
                        bilibili.api_get_json(
                            'https://api.bilibili.com' + WBI_VIEW, signed,
                            cookies=cookies, deadline_monotonic=self.deadline,
                        )
                self.assertTrue(cookies._bilibili_generation.is_set())
                self.assertIs(bilibili._context_generation, shared_generation)
                self.assertFalse(shared_generation.is_set())
                self.assertEqual(bilibili._wbi_cache, shared_mixin)
                self.assertIs(bilibili._cookie_cache, shared_cookies)

    def test_busy_lock_cookie_deletion_stays_local_without_waiting_or_shared_publication(self):
        self.warm_context()
        cookies = bilibili.anonymous_cookie_context(deadline_monotonic=self.deadline)
        generation = cookies._bilibili_generation
        shared_snapshot = bilibili._cookie_cache
        shared_values = shared_snapshot.get_dict()

        def revoke_cookie(request, **kwargs):
            response = self.transport.send(request, **kwargs)
            if urlsplit(request.url).path == WBI_VIEW:
                headers = Message()
                headers.add_header('Set-Cookie',
                                   'buvid3=; Domain=.bilibili.com; Path=/; Max-Age=0')
                response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
            return response

        worker = None
        with offline_requests(revoke_cookie):
            try:
                with bilibili._context_lock:
                    worker = self.start_operation('revoked-api', lambda: bilibili.api_get_json(
                        'https://api.bilibili.com' + WBI_VIEW, {'bvid': 'BV1J4JX6PEMV'},
                        cookies=cookies, deadline_monotonic=self.deadline,
                    ))
                    worker[0].join(timeout=1)
                    self.assert_workers_succeeded([worker])
                    self.assertNotIn('buvid3', cookies.get_dict())
                    self.assertFalse(generation.is_set())
                    self.assertIs(bilibili._context_generation, generation)
                    self.assertIs(bilibili._cookie_cache, shared_snapshot)
                    self.assertEqual(bilibili._cookie_cache.get_dict(), shared_values)
            finally:
                if worker:
                    worker[0].join(timeout=10)
        self.assert_workers_succeeded([worker])
        self.assertEqual(self.paths(), [WBI_VIEW])

    def test_busy_lock_old_cookie_deletion_keeps_newer_same_generation_value_live(self):
        self.warm_context()
        old = bilibili.anonymous_cookie_context(deadline_monotonic=self.deadline)
        current = bilibili.anonymous_cookie_context(deadline_monotonic=self.deadline)
        generation = current._bilibili_generation
        self.assertIs(old._bilibili_generation, generation)
        self.assertIs(old._bilibili_snapshot, current._bilibili_snapshot)

        def update_cookie(request, **kwargs):
            response = self.transport.send(request, **kwargs)
            if urlsplit(request.url).path == WBI_VIEW:
                response.cookies.set('buvid3', 'offline-newer-buvid3',
                                     domain='.bilibili.com', path='/', secure=True)
            return response

        params = {'bvid': 'BV1J4JX6PEMV'}
        with offline_requests(update_cookie):
            bilibili.api_get_json(
                'https://api.bilibili.com' + WBI_VIEW, params, cookies=current,
                deadline_monotonic=self.deadline,
            )
        self.assertEqual(bilibili._cookie_cache.get('buvid3'), 'offline-newer-buvid3')
        self.assertEqual(old.get('buvid3'), 'offline-home-buvid3')
        self.assertIsNot(old._bilibili_snapshot, current._bilibili_snapshot)
        self.assertFalse(generation.is_set())

        def delete_old_cookie(request, **kwargs):
            response = self.transport.send(request, **kwargs)
            if urlsplit(request.url).path == WBI_VIEW:
                headers = Message()
                headers.add_header('Set-Cookie',
                                   'buvid3=; Domain=.bilibili.com; Path=/; Max-Age=0')
                response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
            return response

        workers = []
        with offline_requests(delete_old_cookie):
            try:
                # The main thread owns the lock while both API workers run.
                with bilibili._context_lock:
                    deletion = self.start_operation('late-delete-api', lambda: bilibili.api_get_json(
                        'https://api.bilibili.com' + WBI_VIEW, params, cookies=old,
                        deadline_monotonic=self.deadline,
                    ))
                    workers.append(deletion)
                    deletion[0].join(timeout=1)
                    self.assert_workers_succeeded([deletion])
                    self.assertNotIn('buvid3', old.get_dict())
                    play = self.start_operation('current-play-api', lambda: bilibili.api_get_json(
                        'https://api.bilibili.com' + PLAY, {'cid': 456}, cookies=current,
                        deadline_monotonic=self.deadline,
                    ))
                    workers.append(play)
                    play[0].join(timeout=1)
                    self.assertFalse(play[0].is_alive(), 'Current playurl waited for the cache lock')
                    self.assertEqual(bilibili._cookie_cache.get('buvid3'), 'offline-newer-buvid3')
                    self.assertFalse(
                        generation.is_set(),
                        'Old Cookie deletion revoked the newer shared value; '
                        f'playurl errors={[type(error).__name__ for error in play[2]]}',
                    )
                    self.assert_workers_succeeded([play])
                    self.assertEqual(play[1][0]['durl'][0]['url'], MEDIA)
            finally:
                for worker, _, _ in workers:
                    worker.join(timeout=10)
        self.assert_workers_succeeded(workers)
        self.assertIs(bilibili._context_generation, generation)
        self.assertEqual(self.paths(), [WBI_VIEW, WBI_VIEW, PLAY])
        self.assertIn('buvid3=offline-newer-buvid3', self.transport.requests[-1].headers['Cookie'])
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
