"""Anonymous Bilibili WBI regression coverage at the offline HTTP boundary."""

from email.message import Message
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

import requests
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.urls import reverse

from contracts.network import offline_requests
from contracts import test_backend_contracts as fallback_fixture
from .models import ProviderAccessMode, ProviderRuntimeSetting
from .services.common import XhsDownloadError, XhsParseError
from .services.platforms import bilibili


SOURCE = 'https://www.bilibili.com/video/BV1J4JX6PEMV/'
HOME = 'https://www.bilibili.com/'
SPI = '/x/frontend/finger/spi'
NAV = '/x/web-interface/nav'
LEGACY_VIEW = '/x/web-interface/view'
WBI_VIEW = '/x/web-interface/wbi/view'
PLAY = '/x/player/playurl'
MEDIA = 'https://upos-sz-mirrorcos.bilivideo.com/wbi-offline-fixture.mp4'
TITLE = 'Anonymous WBI offline fixture'
IMG_KEY = '7cd084941338484aae1ad9425b84077c'
SUB_KEY = '4932caff0ff746eab6f01bf08b70ac45'
NOW = 1792540800


def json_response(request, payload, status=200):
    response = requests.Response()
    response.request = request
    response.url = request.url
    response.status_code = status
    response.headers['Content-Type'] = 'application/json'
    response._content = json.dumps(payload).encode()
    response._content_consumed = True
    return response


class ProductionShapeTransport:
    """The completed diagnostic, with no provider or media network access."""

    def __init__(self):
        self.requests = []
        self.spi_payload = {'code': 0, 'data': {'b_4': 'offline-spi-buvid4'}}
        self.nav_payload = {'code': -101, 'data': {
            'isLogin': False, 'wbi_img': {
                'img_url': f'https://i0.hdslb.com/bfs/wbi/{IMG_KEY}.png',
                'sub_url': f'https://i0.hdslb.com/bfs/wbi/{SUB_KEY}.png',
            },
        }}
        self.legacy_status = 412
        self.wbi_status = 200
        self.wbi_code = 0
        self.revoke_on_nav = False

    def send(self, request, **kwargs):
        self.requests.append(request)
        parsed = urlsplit(request.url)
        if request.url == HOME:
            response = json_response(request, {}, status=302)
            response.headers['Location'] = '/zh-Hans'
            headers = Message()
            for name, value in (('buvid3', 'offline-home-buvid3'), ('b_nut', '1792540800')):
                headers.add_header('Set-Cookie',
                                   f'{name}={value}; Domain=.bilibili.com; Path=/; Secure')
                response.cookies.set(name, value, domain='.bilibili.com', path='/', secure=True)
            response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
            return response
        if parsed.hostname != 'api.bilibili.com':
            raise AssertionError(f'Unexpected HTTP request: {request.method} {request.url}')
        if parsed.path == SPI:
            return json_response(request, self.spi_payload)
        if parsed.path == NAV:
            response = json_response(request, self.nav_payload)
            if self.revoke_on_nav:
                headers = Message()
                for name in ('buvid3', 'b_nut', 'buvid4'):
                    headers.add_header('Set-Cookie',
                                       f'{name}=; Domain=.bilibili.com; Path=/; Max-Age=0')
                response.raw = SimpleNamespace(_original_response=SimpleNamespace(msg=headers))
            return response
        if parsed.path in (LEGACY_VIEW, WBI_VIEW):
            status = self.legacy_status if parsed.path == LEGACY_VIEW else self.wbi_status
            code = -status if status != 200 else self.wbi_code
            if status != 200 or code != 0:
                return json_response(request, {'code': code}, status=status)
            return json_response(request, {'code': 0, 'data': {
                'bvid': 'BV1J4JX6PEMV', 'aid': 123, 'cid': 456,
                'title': TITLE, 'pic': 'https://i0.hdslb.com/wbi-offline-cover.jpg',
                'pages': [{'cid': 456, 'page': 1}],
            }})
        if parsed.path == PLAY:
            return json_response(request, {'code': 0, 'data': {
                'quality': 64, 'accept_quality': [64], 'accept_description': ['720P'],
                'durl': [{'url': MEDIA, 'size': 12345}],
            }})
        raise AssertionError(f'Unexpected API endpoint: {request.url}')


@override_settings(CACHES={'default': {
    'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    'LOCATION': 'bilibili-wbi-offline',
}})
class BilibiliWbiPublicEntryTests(TestCase):
    def setUp(self):
        bilibili.clear_anonymous_context_cache()
        self.addCleanup(bilibili.clear_anonymous_context_cache)
        cache.clear()
        self.addCleanup(cache.clear)

    def test_production_302_spi_anonymous_nav_requires_wbi_view(self):
        transport = ProductionShapeTransport()
        with offline_requests(transport.send), patch('time.time', return_value=NOW):
            response = self.client.get(reverse('bilibili_results'), {'q': SOURCE})
        paths = [urlsplit(request.url).path for request in transport.requests]
        self.assertEqual(response.status_code, 200)
        self.assertTrue(TITLE.encode() in response.content,
                        f'Expected usable native WBI result; outbound paths={paths!r}')
        self.assertEqual(response.context['video_items'][0]['url'], MEDIA)
        self.assertEqual(paths, ['/', SPI, NAV, WBI_VIEW, PLAY])
        self.assertNotIn('/zh-Hans', paths)
        self.assertNotIn(LEGACY_VIEW, paths)
        wbi = transport.requests[3]
        self.assertEqual(parse_qs(urlsplit(wbi.url).query), {
            'bvid': ['BV1J4JX6PEMV'], 'wts': [str(NOW)],
            'w_rid': ['e45c9b1c9d50f5a6966a0ba3787743a4'],
        })
        for name, value in (('buvid3', 'offline-home-buvid3'), ('b_nut', str(NOW)),
                            ('buvid4', 'offline-spi-buvid4')):
            self.assertIn(f'{name}={value}', wbi.headers['Cookie'])
            self.assertNotContains(response, value)

    def test_missing_or_invalid_nav_keys_preserve_legacy_success(self):
        for payload in (
            {'code': -101, 'data': {}},
            {'code': 0, 'data': {'wbi_img': {'img_url': 'invalid', 'sub_url': 42}}},
            {'code': -412},
            [],
        ):
            with self.subTest(payload=payload):
                bilibili.clear_anonymous_context_cache()
                transport = ProductionShapeTransport()
                transport.nav_payload = payload
                transport.legacy_status = 200
                with offline_requests(transport.send):
                    result = bilibili.BilibiliAdapter().parse_url(SOURCE)
                self.assertEqual(result.video_assets[0].url, MEDIA)
                self.assertEqual([urlsplit(r.url).path for r in transport.requests],
                                 ['/', SPI, NAV, LEGACY_VIEW, PLAY])

    def test_optional_spi_failure_keeps_home_context_and_does_not_forge_buvid4(self):
        transport = ProductionShapeTransport()
        transport.spi_payload = {'code': -412}
        with offline_requests(transport.send):
            result = bilibili.BilibiliAdapter().parse_url(SOURCE)
        self.assertEqual(result.video_assets[0].url, MEDIA)
        wbi = transport.requests[3]
        self.assertEqual(urlsplit(wbi.url).path, WBI_VIEW)
        self.assertIn('buvid3=offline-home-buvid3', wbi.headers['Cookie'])
        self.assertNotIn('buvid4=', wbi.headers['Cookie'])

    def test_nav_cookie_revocation_does_not_select_signed_view(self):
        transport = ProductionShapeTransport()
        transport.revoke_on_nav = True
        transport.legacy_status = 200
        with offline_requests(transport.send):
            result = bilibili.BilibiliAdapter().parse_url(SOURCE)
        self.assertEqual(result.video_assets[0].url, MEDIA)
        self.assertEqual([urlsplit(r.url).path for r in transport.requests],
                         ['/', SPI, NAV, LEGACY_VIEW, PLAY])
        for request in transport.requests[-2:]:
            self.assertNotIn('Cookie', request.headers)

    def test_expired_context_does_not_request_nav_or_sign(self):
        cookies = requests.cookies.RequestsCookieJar()
        cookies.set('buvid3', 'expired', domain='.bilibili.com', path='/', expires=1)
        transport = ProductionShapeTransport()
        with offline_requests(transport.send):
            self.assertIsNone(bilibili.signed_view_params({'bvid': 'BV1J4JX6PEMV'}, cookies))
        self.assertEqual(transport.requests, [])

    def test_wbi_failure_never_retries_legacy_or_accepts_nav_only_code(self):
        for status, code, error in ((412, -412, XhsDownloadError), (200, -101, XhsParseError)):
            with self.subTest(status=status, code=code):
                bilibili.clear_anonymous_context_cache()
                transport = ProductionShapeTransport()
                transport.wbi_status, transport.wbi_code = status, code
                with offline_requests(transport.send), self.assertRaises(error):
                    bilibili.BilibiliAdapter().parse_url(SOURCE)
                self.assertEqual([urlsplit(r.url).path for r in transport.requests],
                                 ['/', SPI, NAV, WBI_VIEW])

    @override_settings(
        THIRD_PARTY_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PROVIDER_ENABLED=True,
        TIANTIANJIEXI_PHONE='wbi-offline-fixture',
        TIANTIANJIEXI_PASSWORD='wbi-offline-fixture',
        TIANTIANJIEXI_API_BASE_URL=fallback_fixture.SERVICE,
        TIANTIANJIEXI_RETRIES=1,
    )
    def test_public_wbi_412_uses_only_admitted_tiantian_fallback(self):
        ProviderRuntimeSetting.objects.create(
            pk=1, third_party_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms=fallback_fixture.ADMISSION,
            third_party_blocked_platforms='',
        )
        transport = ProductionShapeTransport()
        transport.wbi_status = 412
        service = fallback_fixture.OfflineTransport()
        paths = []

        def send(request, **kwargs):
            paths.append(urlsplit(request.url).path)
            target = service if request.url == fallback_fixture.SERVICE else transport
            return target.send(request, **kwargs)

        with TemporaryDirectory() as directory, patch(
            'xhs_download.services.platforms.tiantian_serial.lock_root', return_value=Path(directory),
        ), offline_requests(send):
            response = self.client.get(reverse('bilibili_results'), {'q': SOURCE})
        self.assertContains(response, fallback_fixture.SERVICE_TITLE)
        self.assertEqual(paths, ['/', SPI, NAV, WBI_VIEW, '/parse'])
        self.assertEqual(service.events, ['tiantian'])
