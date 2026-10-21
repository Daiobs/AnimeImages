"""Bilibili anonymous context through real HTTP consumers, offline transport only."""
from contextlib import ExitStack
from email.message import Message
import json
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import urlsplit

import requests
from django.core.cache import cache
from django.test import SimpleTestCase, TestCase, override_settings
from django.urls import reverse

from contracts.network import offline_requests
from .services.common import XhsDownloadError, XhsSecurityError
from .services.platforms import bilibili


SOURCE = 'https://www.bilibili.com/video/BV1J4JX6PEMV/'
HOME = 'https://www.bilibili.com/'
MEDIA = 'https://upos-sz-mirrorcos.bilivideo.com/session-fixture.mp4'


class AnonymousTransport:
    def __init__(self):
        self.requests = []
        self.bootstraps = 0
        self.home_status = 200
        self.home_location = None
        self.api_status = 200
        self.require_cookies = True
        self.home_cookie_domain = '.bilibili.com'
        self.revoke_cookie_on_view = False

    def send(self, request, **kwargs):
        self.requests.append(request)
        response = requests.Response()
        response.request = request
        response.url = request.url
        response.status_code = 200
        response.headers['Content-Type'] = 'application/json'
        payload = {'code': 0, 'data': {}}
        path = urlsplit(request.url).path
        cookie = request.headers.get('Cookie', '')
        if request.url == HOME:
            self.bootstraps += 1
            response.status_code = self.home_status
            if self.home_location:
                response.headers['Location'] = self.home_location
            for name, value in (
                ('buvid3', f'anonymous-{self.bootstraps}'), ('b_nut', '1700000000'),
                ('SESSDATA', 'must-not-leave-bootstrap'),
            ):
                response.cookies.set(name, value, domain=self.home_cookie_domain,
                                     path='/', secure=True)
        elif urlsplit(request.url).hostname == 'api.bilibili.com':
            response.status_code = self.api_status
            if self.require_cookies and f'buvid3=anonymous-{self.bootstraps}' not in cookie:
                response.status_code = 412
            if path == '/x/web-interface/view':
                payload['data'] = {
                    'bvid': 'BV1J4JX6PEMV', 'aid': 123, 'cid': 456,
                    'title': 'Anonymous session fixture',
                    'pic': 'https://i0.hdslb.com/session-cover.jpg',
                    'pages': [{'cid': 456, 'page': 1}],
                }
                response.cookies.set('buvid4', 'from-view', domain='.bilibili.com',
                                     path='/', secure=True)
                if self.revoke_cookie_on_view:
                    headers = Message()
                    headers.add_header('Set-Cookie',
                                       'buvid3=; Domain=.bilibili.com; Path=/; Max-Age=0')
                    response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
            elif path == '/x/player/playurl':
                payload['data'] = {
                    'quality': 64, 'accept_quality': [64],
                    'accept_description': ['720P'],
                    'durl': [{'url': MEDIA, 'size': 12345}],
                }
            else:
                raise AssertionError(f'Unexpected API path: {path}')
        else:
            raise AssertionError('Unexpected outbound host')
        response._content = json.dumps(payload).encode()
        response._content_consumed = True
        return response


@override_settings(CACHES={'default': {
    'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
}})
class BilibiliAnonymousPublicEntryTests(TestCase):
    def test_public_native_bootstrap_and_cookie_continuity(self):
        cache.clear()
        self.addCleanup(cache.clear)
        transport = AnonymousTransport()
        with offline_requests(transport.send):
            response = self.client.get(reverse('bilibili_results'), {'q': SOURCE})
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, 'Anonymous session fixture')
        self.assertEqual(response.context['video_items'][0]['url'], MEDIA)
        self.assertEqual(transport.bootstraps, 1)
        self.assertEqual(len(transport.requests), 3)
        self.assertNotContains(response, 'anonymous-1')
        self.assertNotContains(response, 'must-not-leave-bootstrap')


class BilibiliAnonymousContextTests(SimpleTestCase):
    def setUp(self):
        self.transport = AnonymousTransport()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(offline_requests(self.transport.send))

    def test_cookie_scope_is_one_parse_and_never_media_headers(self):
        for number in (1, 2):
            result = bilibili.BilibiliAdapter().parse_url(SOURCE)
            self.assertEqual(result.video_assets[0].quality_label, '720P')
            home, view, play = self.transport.requests[-3:]
            self.assertEqual(home.url, HOME)
            self.assertNotIn('Cookie', home.headers)
            self.assertIn(f'buvid3=anonymous-{number}', view.headers['Cookie'])
            self.assertIn('b_nut=1700000000', view.headers['Cookie'])
            self.assertIn('buvid4=from-view', play.headers['Cookie'])
            for request in (view, play):
                self.assertNotIn('SESSDATA', request.headers['Cookie'])
            self.assertNotIn('Cookie', result.video_assets[0].headers)
            self.assertNotIn('anonymous-', str(result))
        self.assertEqual(self.transport.bootstraps, 2)

    def test_optional_bootstrap_failure_preserves_existing_api_success(self):
        self.transport.home_status = 503
        self.transport.require_cookies = False
        result = bilibili.BilibiliAdapter().parse_url(SOURCE)
        self.assertEqual(result.video_assets[0].url, MEDIA)
        self.assertEqual(self.transport.bootstraps, 1)
        self.assertNotIn('Cookie', self.transport.requests[1].headers)

    def test_cookie_for_another_domain_is_not_sent_to_api(self):
        self.transport.home_cookie_domain = 'other.example'
        with self.assertRaises(XhsDownloadError) as raised:
            bilibili.BilibiliAdapter().parse_url(SOURCE)
        self.assertEqual(raised.exception.http_status, 412)
        self.assertNotIn('Cookie', self.transport.requests[-1].headers)
        self.assertEqual(len(self.transport.requests), 2)

    def test_bootstrap_redirect_is_not_followed_and_api_can_still_succeed(self):
        self.transport.home_status = 302
        self.transport.home_location = 'https://other.example/challenge'
        self.transport.require_cookies = False
        result = bilibili.BilibiliAdapter().parse_url(SOURCE)
        self.assertEqual(result.video_assets[0].url, MEDIA)
        self.assertEqual(len(self.transport.requests), 3)
        self.assertNotIn('Cookie', self.transport.requests[1].headers)

    def test_explicit_412_is_not_repeated_after_initialization(self):
        self.transport.api_status = 412
        with self.assertRaises(XhsDownloadError) as raised:
            bilibili.BilibiliAdapter().parse_url(SOURCE)
        self.assertEqual(raised.exception.http_status, 412)
        self.assertEqual(len(self.transport.requests), 2)

    def test_expired_and_wrong_path_cookies_are_not_sent(self):
        cookies = requests.cookies.RequestsCookieJar()
        cookies.set('buvid3', 'expired', domain='.bilibili.com', path='/', expires=1)
        cookies.set('b_nut', 'wrong-path', domain='.bilibili.com', path='/private/')
        self.transport.require_cookies = False
        bilibili.api_get_json(
            'https://api.bilibili.com/x/web-interface/view', {}, cookies=cookies,
        )
        self.assertNotIn('Cookie', self.transport.requests[0].headers)

    def test_cookie_bearing_api_does_not_follow_same_or_cross_origin_redirect(self):
        for target in ('https://api.bilibili.com/outside-scope',
                       'https://www.bilibili.com/api-result'):
            seen = []

            def redirect_boundary(request, **kwargs):
                seen.append(request)
                response = requests.Response()
                response.request = request
                response.url = request.url
                response._content = b''
                response._content_consumed = True
                response.status_code = 302
                response.headers['Location'] = target
                return response

            cookies = requests.cookies.RequestsCookieJar()
            cookies.set('buvid3', 'anonymous', domain='.bilibili.com', path='/x/web-interface/')
            with self.subTest(target=target), offline_requests(redirect_boundary):
                with self.assertRaises(requests.TooManyRedirects):
                    bilibili.api_get_json(
                        'https://api.bilibili.com/x/web-interface/view', {}, cookies=cookies,
                    )
            self.assertEqual(len(seen), 1)
            self.assertIn('buvid3=anonymous', seen[0].headers['Cookie'])

    def test_cookie_revoked_by_view_is_not_sent_to_playurl(self):
        self.transport.revoke_cookie_on_view = True
        self.transport.require_cookies = False
        bilibili.BilibiliAdapter().parse_url(SOURCE)
        _, view, play = self.transport.requests
        self.assertIn('buvid3=anonymous-1', view.headers['Cookie'])
        self.assertNotIn('buvid3=', play.headers['Cookie'])
        self.assertIn('b_nut=1700000000', play.headers['Cookie'])

    def test_bootstrap_security_and_tls_failure_are_not_ignored(self):
        for error in (XhsSecurityError('blocked address'),
                      requests.exceptions.SSLError('invalid certificate')):
            with self.subTest(error=type(error).__name__), patch.object(
                bilibili, 'request_get_with_retry', side_effect=error,
            ) as get:
                with self.assertRaises(type(error)):
                    bilibili.BilibiliAdapter().parse_url(SOURCE)
                get.assert_called_once()

    def test_existing_deadline_covers_bootstrap_and_both_api_calls(self):
        original = bilibili.request_get_with_retry
        with patch.object(bilibili, 'request_get_with_retry', wraps=original) as get:
            deadline = bilibili.time.monotonic() + 5
            bilibili.BilibiliAdapter().parse_url(SOURCE, deadline_monotonic=deadline)
        self.assertEqual(len(get.call_args_list), 3)
        for call in get.call_args_list:
            self.assertEqual(call.kwargs['deadline_monotonic'], deadline)
            self.assertLessEqual(call.kwargs['timeout'], 5)
        bootstrap = get.call_args_list[0].kwargs
        self.assertEqual(bootstrap['retries'], 1)
        self.assertEqual(bootstrap['max_redirects'], 0)
        self.assertTrue(bootstrap['stream'])
