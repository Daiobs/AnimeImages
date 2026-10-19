import os
from types import SimpleNamespace
from unittest.mock import patch

from django.core.cache import cache
from django.test import RequestFactory, SimpleTestCase, override_settings

from .services.batch_parse.runtime import batch_parse_rate_limit_results
from .services.download_handoff import (
    DownloadHandoffError, issue_download_handoff, load_download_handoff,
)
from .services.observability import request_privacy_fields, salted_hash
from .services.provider_access import (
    provider_access_summary, request_rollout_bucket, request_rollout_identity,
)
from .services.rate_limits import get_client_identity, get_client_ip
from .services.region_routing import get_request_country_code
from .services.user_feedback import feedback_rate_limit_result


@override_settings(
    DOWNLOAD_TRUST_X_FORWARDED_FOR=True,
    DOWNLOAD_TRUSTED_PROXY_CIDRS=('127.0.0.1/32', '2001:db8:1::/64'),
)
class ClientIdentityBoundaryTests(SimpleTestCase):
    def setUp(self):
        cache.clear()
        self.factory = RequestFactory()

    def request(self, client='198.51.100.8', *, peer='127.0.0.1', **headers):
        request = self.factory.get('/', REMOTE_ADDR=peer, **{
            'HTTP_X_FORWARDED_FOR': client, **headers,
        })
        request.user = SimpleNamespace(is_authenticated=False, is_staff=False, is_active=False)
        return request

    def test_trusted_boundary_ignores_forged_left_and_other_headers(self):
        request = self.request('192.0.2.77, 198.51.100.8',
                               HTTP_X_REAL_IP='192.0.2.88',
                               HTTP_FORWARDED='for=192.0.2.99')
        self.assertEqual(get_client_ip(request), '198.51.100.8')

    def test_oversized_untrusted_prefix_cannot_select_the_proxy_bucket(self):
        first = self.request()
        for prefix in ('a' * 4096, ','.join(['192.0.2.77'] * 100)):
            with self.subTest(prefix_length=len(prefix)):
                padded = self.request(prefix + ', 198.51.100.8')
                self.assertEqual(get_client_ip(padded), '198.51.100.8')
                self.assertEqual(request_rollout_bucket('third_party', first),
                                 request_rollout_bucket('third_party', padded))

    def test_untrusted_peer_and_disabled_or_unconfigured_trust(self):
        self.assertEqual(get_client_ip(self.request(peer='203.0.113.5')), '203.0.113.5')
        with override_settings(DOWNLOAD_TRUST_X_FORWARDED_FOR=False):
            self.assertEqual(get_client_ip(self.request()), '127.0.0.1')
        for ranges in ((), ('0.0.0.0/0',), ('::/0',), ('bad-cidr',),
                       ('127.0.0.1/32', 'bad-cidr')):
            with self.subTest(ranges=ranges), override_settings(DOWNLOAD_TRUSTED_PROXY_CIDRS=ranges):
                self.assertEqual(get_client_ip(self.request()), '127.0.0.1')

    def test_multiple_explicit_proxies_and_canonical_addresses(self):
        self.assertEqual(get_client_ip(self.request('198.51.100.8, 2001:db8:1::2')), '198.51.100.8')
        self.assertEqual(get_client_ip(self.request('2001:0DB8:2:0::3', peer='2001:db8:1::9')), '2001:db8:2::3')
        self.assertEqual(get_client_ip(self.request('::ffff:198.51.100.8', peer='::ffff:127.0.0.1')), '198.51.100.8')
        self.assertEqual(get_client_ip(self.request(peer='2001:0db8:2:0::3')), '2001:db8:2::3')

    def test_trusted_ranges_use_existing_environment_reader(self):
        with override_settings(DOWNLOAD_TRUSTED_PROXY_CIDRS=None), patch.dict(
            os.environ, {'DOWNLOAD_TRUSTED_PROXY_CIDRS': '127.0.0.1/32,2001:db8:1::/64'}
        ):
            self.assertEqual(get_client_ip(self.request()), '198.51.100.8')

    @override_settings(DOWNLOAD_TRUST_X_FORWARDED_FOR=False)
    def test_default_rollout_ignores_all_client_supplied_identity_headers(self):
        first = self.request(peer='198.51.100.8', HTTP_USER_AGENT='browser-a')
        other = self.request('192.0.2.77', peer='198.51.100.8',
                             HTTP_USER_AGENT='browser-b', HTTP_X_REAL_IP='192.0.2.88')
        self.assertEqual(request_rollout_identity(first), request_rollout_identity(other))
        self.assertEqual(request_rollout_bucket('third_party', first), request_rollout_bucket('third_party', other))

    def test_invalid_chain_never_skips_boundary(self):
        for value in ('', '198.51.100.8,', '198.51.100.8, bad',
                      '198.51.100.8, [2001:db8:1::2]', '198.51.100.8, 127.0.0.1:42',
                      '198.51.100.8, fe80::1%en0', 'a' * 2049,
                      ','.join(['127.0.0.1'] * 21)):
            with self.subTest(value=value[:50]):
                self.assertEqual(get_client_ip(self.request(value)), '127.0.0.1')
        # An untrusted valid boundary ends parsing; arbitrary claims to its left are irrelevant.
        self.assertEqual(get_client_ip(self.request('bad, 198.51.100.8')), '198.51.100.8')
        self.assertEqual(get_client_ip(self.request(peer='not-an-ip')), 'unknown')
        self.assertEqual(get_client_ip(self.request(peer='')), 'unknown')

    def test_anonymous_rollout_uses_same_identity_and_preserves_modes(self):
        first = self.request(HTTP_USER_AGENT='browser-a')
        spoofed = self.request('192.0.2.77, 198.51.100.8', HTTP_USER_AGENT='browser-b')
        self.assertEqual(request_rollout_identity(first), 'ip:198.51.100.8')
        self.assertEqual(request_rollout_bucket('third_party', first), request_rollout_bucket('third_party', spoofed))
        setting = SimpleNamespace(third_party_mode='disabled', third_party_rollout_percent=100,
                                  third_party_disabled_reason='')
        for mode, percent, allowed in [('disabled', 100, False), ('staff', 100, False),
                                       ('public_gray', 0, False), ('public_gray', 100, True),
                                       ('public', 0, True)]:
            setting.third_party_mode, setting.third_party_rollout_percent = mode, percent
            for request in (first, spoofed):
                result = provider_access_summary('third_party', request, setting, hard_enabled_override=True)
                self.assertEqual(result['request_allowed'], allowed)
        first.user = SimpleNamespace(is_authenticated=True, pk=123, is_active=True, is_staff=False)
        self.assertEqual(request_rollout_identity(first), 'user:123')

    def test_batch_and_feedback_consumers_have_independent_client_quotas(self):
        first, other = self.request(), self.request('198.51.100.9')
        with patch('xhs_download.services.batch_parse.runtime.batch_parse_runtime_setting_or_none', return_value=None):
            with override_settings(BATCH_PARSE_IP_INTERVAL_SECONDS=20,
                                   BATCH_PARSE_GLOBAL_MINUTE_LIMIT=100, BATCH_PARSE_DAILY_IP_LIMIT=100):
                self.assertTrue(dict(batch_parse_rate_limit_results(first, 1))['ip_interval'].allowed)
                self.assertFalse(dict(batch_parse_rate_limit_results(first, 1))['ip_interval'].allowed)
                self.assertTrue(dict(batch_parse_rate_limit_results(other, 1))['ip_interval'].allowed)
        with patch('xhs_download.services.user_feedback.FEEDBACK_RATE_LIMIT', 1):
            self.assertTrue(feedback_rate_limit_result(first).allowed)
            self.assertFalse(feedback_rate_limit_result(self.request('192.0.2.7, 198.51.100.8')).allowed)
            self.assertTrue(feedback_rate_limit_result(other).allowed)

    def test_geoip_observation_and_resource_authorization_agree(self):
        first = self.request('192.0.2.77, 198.51.100.8')
        with override_settings(TIKHUB_TRUST_COUNTRY_HEADER=False), patch(
            'xhs_download.services.region_routing.lookup_geoip_country_code', return_value='CN'
        ) as geoip:
            self.assertEqual(get_request_country_code(first), 'CN')
            geoip.assert_called_once_with('198.51.100.8')
        self.assertEqual(request_privacy_fields(first)['ip_hash'], salted_hash('198.51.100.8'))
        token = issue_download_handoff(
            item={'type': 'image', 'url': 'https://sns-webpic-qc.xhscdn.com/test.jpg'},
            base_filename='synthetic', client_identity=get_client_identity(first),
        )
        self.assertEqual(load_download_handoff(token, client_identity=get_client_identity(self.request()), consume=False).base_filename, 'synthetic')
        with self.assertRaises(DownloadHandoffError):
            load_download_handoff(token, client_identity=get_client_identity(self.request('198.51.100.9')), consume=False)
