"""Navigation authorization through real normalization, issuance and endpoints."""
import socket
import time
from types import SimpleNamespace
from unittest.mock import patch

from bs4 import BeautifulSoup
from django.core import signing
from django.core.cache import cache
from django.template.loader import render_to_string
from django.test import Client, RequestFactory, SimpleTestCase, override_settings
from django.urls import resolve, reverse

from .models import ProviderAccessMode, ProviderRuntimeSetting
from .services import service_format_selection, ytdlp_selection
from .services.candidates import MediaCandidate
from .services.platforms.douyin import DouyinAdapter
from .services.platforms.douyin_providers import (
    DouyinParseContext, attach_douyin_service_picker, build_external_parse_result,
)
from .services.platforms.kukutool_generic import KukutoolResultNormalizer
from .services.platforms.tiantianjiexi_generic import TianTianJieXiResultNormalizer
from .services.upstream_candidates import SALT, TTL, resolve_candidate_record
from .services.upstream_delivery import normalize_upstream_result


PROVIDERS = ('yt_dlp_generic', 'kukutool_generic', 'tiantianjiexi_generic')
DOUYIN_PROVIDERS = ('tiantianjiexi', 'kukutool')
SOURCE = 'https://source.example/work/one'


class OpenFixtures:
    """Composed fixtures, not inherited test suites or mocked authorization decisions."""

    def __init__(self, case):
        self.case = case
        self.matches = tuple({'provider_id': provider,
                              'extractor_id': 'Fixture' if provider == 'yt_dlp_generic' else provider,
                              'version': 'fixture', 'label': 'Fixture'} for provider in PROVIDERS)
        self.rule = SimpleNamespace(platform_id='upstream', risk_level='low', staff_default=True,
                                    provider_ids=PROVIDERS, upstream_matches=self.matches,
                                    media_host_suffixes=('media.example',),
                                    image_media_host_suffixes=(), video_media_host_suffixes=())
        self.runtime = ProviderRuntimeSetting(
            third_party_mode=ProviderAccessMode.PUBLIC, douyin_ultra_mode=ProviderAccessMode.PUBLIC,
            third_party_allowed_public_platforms='\n'.join('upstream:' + p for p in PROVIDERS))
        case.enterContext(patch('xhs_download.services.provider_access.load_runtime_setting', return_value=self.runtime))
        case.enterContext(patch('xhs_download.services.platforms.third_party.GenericThirdPartyAdapter.get_rule_for_url',
                               return_value=self.rule))
        self.enabled = case.enterContext(patch('xhs_download.services.platforms.third_party.provider_is_enabled',
                                              return_value=True))
        self.douyin_settings = SimpleNamespace(provider_is_enabled=lambda provider: provider in DOUYIN_PROVIDERS)
        case.enterContext(patch('xhs_download.services.provider_settings.get_platform_provider_settings',
                               return_value=self.douyin_settings))

    def candidates(self, provider, *changes):
        rows = [{'url': f'https://media.example/video-{index}.mp4', 'format_id': f'format-{index}',
                 'type': f'{1080 - index * 360}P', 'qualityLabel': f'{1080 - index * 360}P',
                 'ext': 'mp4', 'protocol': 'https', **change} for index, change in enumerate(changes or ({}, {}))]
        if provider == 'yt_dlp_generic':
            result = normalize_upstream_result({'id': 'one', 'title': 'Fixture work', 'formats': rows},
                                              match=self.matches[0], source_url=SOURCE)
        else:
            normalizer = (KukutoolResultNormalizer() if provider == 'kukutool_generic'
                          else TianTianJieXiResultNormalizer())
            key = 'video_fullinfo' if provider == 'kukutool_generic' else 'streams'
            result = normalizer.normalize(rule=self.rule, platform_name='Fixture', source_url=SOURCE,
                                          payload={'title': 'Fixture work', 'videos': [{key: rows}]})
        return result.metadata['upstream_candidates']

    def douyin_candidates(self, provider, urls=None):
        context = DouyinParseContext(url='https://www.douyin.com/video/1234567890123456789',
                                    aweme_id='1234567890123456789')
        adapter = DouyinAdapter()
        urls = urls or tuple(f'https://v3-dy-o.zjcdn.com/fixture/{quality}.mp4' for quality in ('1080P', '720P'))
        candidates = tuple(MediaCandidate(url=url, quality_label=str(index)) for index, url in enumerate(urls))
        result = build_external_parse_result(adapter, context, candidates[0], provider_name=provider)
        result = attach_douyin_service_picker(result, adapter=adapter, context=context, candidates=candidates)
        return result.metadata['upstream_candidates']

    @staticmethod
    def selection(provider):
        return ytdlp_selection if provider == 'yt_dlp_generic' else service_format_selection

    @staticmethod
    def endpoint(provider):
        return reverse('ytdlp_selected_format' if provider == 'yt_dlp_generic' else 'service_selected_format')

    def post(self, provider, candidate, action='open', **extra):
        return self.case.client.post(self.endpoint(provider),
                                    {'candidate_id': candidate['candidate_id'],
                                     'resource_token': candidate['resource_token'], 'action': action, **extra},
                                    content_type='application/json')

    def staff_open(self, provider, candidate):
        request = RequestFactory().post(self.endpoint(provider),
                                       {'candidate_id': candidate['candidate_id'],
                                        'resource_token': candidate['resource_token'], 'action': 'open'},
                                       content_type='application/json')
        request.user = SimpleNamespace(is_authenticated=True, is_active=True, is_staff=True)
        request._dont_enforce_csrf_checks = True
        match = resolve(self.endpoint(provider))
        return match.func(request, *match.args, **match.kwargs)


@override_settings(
    CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                        'LOCATION': 'selected-video-open-tests'}},
    THIRD_PARTY_PROVIDER_ENABLED=True, ULTRA_HD_PROVIDER_ENABLED=True, KUKUTOOL_PROVIDER_ENABLED=True,
)
class SelectedVideoOpenTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.addCleanup(cache.clear)
        self.enterContext(patch('socket.getaddrinfo', return_value=[
            (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('93.184.216.34', 443)),
        ]))
        self.forbidden = {}
        for target in (
            'requests.sessions.Session.request', 'urllib.request.OpenerDirector.open', 'socket.socket.connect',
            'subprocess.Popen',
            'xhs_download.services.platforms.ytdlp_generic.YtDlpGenericProvider.parse',
            'xhs_download.services.platforms.kukutool_generic.KukutoolGenericProvider.parse',
            'xhs_download.services.platforms.tiantianjiexi_generic.TianTianJieXiGenericProvider.parse',
            'xhs_download.services.platforms.douyin_providers.DouyinTianTianJieXiProvider.parse',
            'xhs_download.services.platforms.douyin_kukutool.DouyinKukuToolProvider.parse',
            'xhs_download.services.ytdlp_format_check.check_selected_record',
            'xhs_download.services.ytdlp_selection.selected_descriptor',
            'xhs_download.services.service_format_selection.selected_descriptor',
            'xhs_download.services.service_format_selection.selected_legacy_descriptor',
            'xhs_download.views.check_download_cost_preflight',
            'xhs_download.views.check_download_request_limits',
        ):
            self.forbidden[target] = self.enterContext(patch(target, side_effect=AssertionError(target)))
        self.fixture = OpenFixtures(self)

    def tearDown(self):
        for target, guard in self.forbidden.items():
            with self.subTest(forbidden=target):
                guard.assert_not_called()

    def assert_open(self, provider, candidate):
        choice = self.fixture.selection(provider).choice(candidate)
        self.assertEqual(choice['audio_tracks'], 'unknown')
        self.assertFalse(choice['can_preview'])
        self.assertTrue(choice['can_open'])
        record = resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])
        response = self.fixture.post(provider, candidate)
        self.assertEqual(response.status_code, 200, response.content)
        self.assertTrue(response.wsgi_request.user.is_anonymous)
        self.assertTrue({'private', 'no-store'} <= set(response['Cache-Control'].split(', ')))
        payload = response.json()
        self.assertFalse(payload['server_media_relay'])
        self.assertEqual(payload['item'], self.fixture.selection(provider).selected_link_descriptor(record))
        self.assertEqual(payload['item']['candidate_id'], candidate['candidate_id'])
        self.assertEqual(payload['item']['url'], record['url'])
        self.assertEqual(set(payload['item']), {'selection_schema', 'candidate_id', 'selected_format_id',
                                              'url', 'type', 'provider', 'filename'})

    def test_unknown_audio_without_preview_opens_exact_selected_video_without_cors_or_save_grant(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                rows = self.fixture.candidates(provider)
                for candidate in reversed(rows):
                    self.assert_open(provider, candidate)

    def test_dedicated_douyin_nondefault_video_uses_its_own_candidate_and_current_admission(self):
        for provider in DOUYIN_PROVIDERS:
            with self.subTest(provider=provider):
                rows = self.fixture.douyin_candidates(provider)
                self.assertEqual(len(rows), 2)
                self.assert_open(provider, rows[-1])
                self.fixture.runtime.douyin_ultra_mode = ProviderAccessMode.STAFF
                self.assertEqual(self.fixture.staff_open(provider, rows[-1]).status_code, 200)
                self.assertEqual(self.fixture.post(provider, rows[-1]).status_code, 403)
                self.fixture.runtime.douyin_ultra_mode = ProviderAccessMode.PUBLIC
                self.fixture.douyin_settings.provider_is_enabled = lambda name: False
                self.assertEqual(self.fixture.post(provider, rows[-1]).status_code, 403)
                self.fixture.douyin_settings.provider_is_enabled = lambda name: name in DOUYIN_PROVIDERS

    def test_rendered_projection_exposes_native_open_button_without_replacing_embedded_previews(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                rows = self.fixture.candidates(provider)
                picker = (ytdlp_selection.build_picker(rows) if provider == 'yt_dlp_generic'
                          else service_format_selection.build_service_picker(rows))
                soup = BeautifulSoup(render_to_string('partials/ytdlp_result_picker.html',
                                                       {'ytdlp_picker': picker}), 'html.parser')
                self.assertFalse(soup.select('[data-ytdlp-action="preview"]'))
                self.assertEqual(len(soup.select('[data-picker-radio][data-can-open="true"]')), 2)
                button = soup.select_one('button[data-ytdlp-action="open"]')
                self.assertEqual(button['type'], 'button')
                self.assertEqual(button['aria-label'], '在新标签页播放所选视频')
        rows = self.fixture.candidates('yt_dlp_generic', {'vcodec': 'h264', 'acodec': 'aac'})
        soup = BeautifulSoup(render_to_string('partials/ytdlp_result_picker.html',
            {'ytdlp_picker': ytdlp_selection.build_picker(rows)}), 'html.parser')
        self.assertIsNotNone(soup.select_one('button[data-ytdlp-action="preview"]'))
        self.assertFalse(soup.select('[data-ytdlp-action="open"]'))

    def assert_restricted(self, changes):
        for provider in PROVIDERS:
            with self.subTest(provider=provider, changes=changes):
                candidate = self.fixture.candidates(provider, changes)[0]
                self.assertFalse(self.fixture.selection(provider).choice(candidate)['can_open'])
                response = self.fixture.post(provider, candidate)
                self.assertEqual(response.status_code, 409, response.content)
                self.assertNotIn('item', response.json())
                self.assertEqual(self.fixture.post(provider, candidate, action='link').status_code, 200)

    def test_drm_is_rejected_without_revoking_copy(self):
        self.assert_restricted({'has_drm': True})

    def test_http_is_rejected_without_protocol_upgrade(self):
        self.assert_restricted({'url': 'http://media.example/video.mp4', 'protocol': 'http'})

    def test_secret_or_required_headers_are_rejected(self):
        for header in ('Cookie', 'Authorization'):
            self.assert_restricted({'http_headers': {header: 'private-fixture'}})
        for header in ('Referer', 'Origin'):
            self.assert_restricted({'http_headers': {header: SOURCE}, 'required_headers': [header]})

    def test_cookie_requirement_and_unretained_headers_are_rejected(self):
        self.assert_restricted({'requires_cookie': True})
        self.assert_restricted({'headers_present': True})

    def test_split_tracks_are_rejected_without_probing_or_merging(self):
        self.assert_restricted({'requires_merge': True})

    def test_silent_standalone_video_can_open_without_new_download_permission(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                candidate = self.fixture.candidates(provider, {'vcodec': 'h264', 'acodec': 'none'})[0]
                choice = self.fixture.selection(provider).choice(candidate)
                self.assertEqual(choice['audio_tracks'], 'absent')
                self.assertTrue(choice['can_open'])
                self.assertFalse(choice['can_download'])
                self.assertFalse(choice['can_preview'])
                response = self.fixture.post(provider, candidate)
                self.assertEqual(response.status_code, 200, response.content)
                self.assertNotIn('browser_direct_fetch_safe', response.json()['item'])

    def test_known_byte_cdn_expiry_is_rechecked_before_token_expiry(self):
        now = int(time.time())
        for provider in DOUYIN_PROVIDERS:
            for host in ('v26-default.365yg.com', 'v3-dy-o.zjcdn.com', 'v3-dy-a-x.ixigua.com'):
                with self.subTest(provider=provider, host=host):
                    expiry = now + 5
                    url = f'https://{host}/signature/{expiry:08x}/video/tos/fixture.mp4'
                    candidate = self.fixture.douyin_candidates(provider, [url])[0]
                    self.assert_open(provider, candidate)
                    with patch('xhs_download.services.ytdlp_selection.time', return_value=expiry):
                        self.assertIsNotNone(resolve_candidate_record(candidate['resource_token'], candidate['candidate_id']))
                        response = self.fixture.post(provider, candidate)
                    self.assertEqual(response.status_code, 409, response.content)
                    self.assertIn('已过期', response.json()['message'])
                    self.assertIn('重新解析', response.json()['message'])
                    self.assertNotIn('item', response.json())
                    self.assertEqual(self.fixture.post(provider, candidate, action='link').status_code, 200)

    def test_byte_cdn_expiry_also_applies_to_generic_selected_endpoint(self):
        now = int(time.time())
        url = f'https://v26-default.365yg.com/signature/{now - 1:08x}/video/tos/fixture.mp4'
        candidate = self.fixture.candidates('yt_dlp_generic', {'url': url})[0]
        self.assertIsNotNone(resolve_candidate_record(candidate['resource_token'], candidate['candidate_id']))
        response = self.fixture.post('yt_dlp_generic', candidate)
        self.assertEqual(response.status_code, 409, response.content)
        self.assertIn('已过期', response.json()['message'])

    def test_streaming_manifests_are_rejected(self):
        for protocol, ext in (('m3u8_native', 'm3u8'), ('http_dash_segments', 'mpd')):
            self.assert_restricted({'url': f'https://media.example/stream.{ext}', 'protocol': protocol, 'ext': ext})

    def test_non_sensitive_headers_do_not_imply_fetch_cors_or_secret_context(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                candidate = self.fixture.candidates(provider, {
                    'http_headers': {'User-Agent': 'fixture', 'Accept': '*/*', 'Referer': SOURCE,
                                     'Origin': 'https://source.example', 'Sec-Fetch-Mode': 'navigate'}})[0]
                self.assert_open(provider, candidate)

    def test_cross_candidate_and_cross_endpoint_tokens_are_rejected(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                first, second = self.fixture.candidates(provider)
                self.assertEqual(self.fixture.post(provider, first, resource_token=second['resource_token']).status_code, 403)
                other = 'kukutool_generic' if provider == 'yt_dlp_generic' else 'yt_dlp_generic'
                self.assertEqual(self.fixture.post(other, first).status_code, 403)

    def test_expired_signed_token_is_rejected_while_bundle_remains_cached(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                old_timestamp = signing.b62_encode(int(time.time()) - TTL - 10)
                with patch.object(signing.TimestampSigner, 'timestamp', return_value=old_timestamp):
                    candidate = self.fixture.candidates(provider)[0]
                token = signing.loads(candidate['resource_token'], salt=SALT)
                self.assertIsNotNone(cache.get('upstream:candidate-bundle:' + token['bundle']))
                response = self.fixture.post(provider, candidate)
                self.assertEqual(response.status_code, 403, response.content)
                self.assertIn('重新解析', response.json()['message'])

    def test_missing_bundle_cannot_be_renewed_by_open(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                candidate = self.fixture.candidates(provider)[0]
                cache.clear()
                self.assertEqual(self.fixture.post(provider, candidate).status_code, 403)

    def test_current_anonymous_admission_is_rechecked(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                candidate = self.fixture.candidates(provider)[0]
                self.assertEqual(self.fixture.post(provider, candidate).status_code, 200)
                for mode in (ProviderAccessMode.STAFF, ProviderAccessMode.DISABLED):
                    self.fixture.runtime.third_party_mode = mode
                    denied = self.fixture.post(provider, candidate)
                    self.assertTrue(denied.wsgi_request.user.is_anonymous)
                    self.assertEqual(denied.status_code, 403, denied.content)
                self.fixture.runtime.third_party_mode = ProviderAccessMode.PUBLIC

    def test_staff_success_does_not_grant_anonymous_access(self):
        self.fixture.runtime.third_party_mode = ProviderAccessMode.STAFF
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                candidate = self.fixture.candidates(provider)[0]
                self.assertEqual(self.fixture.staff_open(provider, candidate).status_code, 200)
                response = self.fixture.post(provider, candidate)
                self.assertTrue(response.wsgi_request.user.is_anonymous)
                self.assertEqual(response.status_code, 403, response.content)
                self.assertNotIn('item', response.json())

    def test_current_allowlist_and_provider_disable_cannot_be_bypassed(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                candidate = self.fixture.candidates(provider)[0]
                allowed = self.fixture.runtime.third_party_allowed_public_platforms
                self.fixture.runtime.third_party_allowed_public_platforms = 'upstream:gallery_dl_generic'
                self.assertEqual(self.fixture.post(provider, candidate).status_code, 403)
                self.fixture.runtime.third_party_allowed_public_platforms = allowed
                self.fixture.enabled.return_value = False
                self.assertEqual(self.fixture.post(provider, candidate).status_code, 403)
                self.fixture.enabled.return_value = True

    def test_public_dns_is_revalidated_before_issuing_navigation_link(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                candidate = self.fixture.candidates(provider)[0]
                with patch('socket.getaddrinfo', return_value=[
                    (socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, '', ('127.0.0.1', 443)),
                ]):
                    self.assertEqual(self.fixture.post(provider, candidate).status_code, 403)

    def test_client_url_authority_injection_and_missing_csrf_are_rejected(self):
        for provider in PROVIDERS:
            with self.subTest(provider=provider):
                candidate = self.fixture.candidates(provider)[0]
                for extra in ({'url': 'https://media.example/other.mp4'}, {'can_open': True}, {'is_staff': True}):
                    self.assertEqual(self.fixture.post(provider, candidate, **extra).status_code, 400)
                response = Client(enforce_csrf_checks=True).post(self.fixture.endpoint(provider),
                    {'candidate_id': candidate['candidate_id'], 'resource_token': candidate['resource_token'],
                     'action': 'open'}, content_type='application/json')
                self.assertEqual(response.status_code, 403)
