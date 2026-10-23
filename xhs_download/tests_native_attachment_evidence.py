"""Native eligibility through the real probe; only final HTTP/DNS is replaced."""
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase

from .services.provider_strategy import browser_transport_hint
from .services.downloads import probe_media_size
from .services.request_context import request_context_compatible, request_context_fingerprint
from .services.quality import (
    QualityProbe, attachment_filename, native_attachment_expiry,
    native_attachment_name_is_eligible, ensure_native_attachment_evidence,
)
from .tests_request_redirect_boundaries import BoundaryNetworkCase


class AttachmentFilenameTests(SimpleTestCase):
    def test_rfc_parameter_decoding_and_priority(self):
        cases = (
            ('attachment; filename=12345.mp4', '12345.mp4'),
            ('ATTACHMENT; FILENAME="source; clip.MP4"', 'source; clip.MP4'),
            ('attachment; filename="clip\\ name.mp4"', 'clip name.mp4'),
            ("attachment; filename*=UTF-8''%E4%BD%9C%E5%93%81%2Emp4", '\u4f5c\u54c1.mp4'),
            ("attachment; filename*=ISO-8859-1'en'%A3rates.mp4", '\u00a3rates.mp4'),
            ("attachment; filename*=windows-1252''%80rates.mp4", '\u20acrates.mp4'),
            ("attachment; filename*=GB18030''%D7%F7%C6%B7.mp4", '\u4f5c\u54c1.mp4'),
            ("attachment; filename=bad.bin; filename*=UTF-8''12345%2Emp4", '12345.mp4'),
            ("attachment; filename*=UTF-8''12345%2Emp4; filename=bad.bin", '12345.mp4'),
            ("attachment; filename=good.mp4; filename*=UTF-8''bad.bin", 'bad.bin'),
            ("attachment; filename*=UTF-8''bad.bin; filename=good.mp4", 'bad.bin'),
            ('attachment; filename="literal%2Emp4"', 'literal%2Emp4'),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(attachment_filename(header), expected)

    def test_uncertain_extended_parameter_never_claims_lower_priority_filename(self):
        for value in ("UTF-8''broken%2.mp4", "UTF-8''broken%GG.mp4", "UTF-8''%FF.mp4",
                      "X-UNKNOWN''source.mp4", "UTF-8'source.mp4", '"UTF-8\'\'source.mp4"'):
            with self.subTest(value=value):
                self.assertFalse(native_attachment_name_is_eligible(
                    'attachment; filename*=' + value, 'video/mp4'))
                for header in ('attachment; filename=source.mp4; filename*=' + value,
                               'attachment; filename*=' + value + '; filename=source.mp4'):
                    self.assertEqual(attachment_filename(header), '')
                    self.assertFalse(native_attachment_name_is_eligible(header, 'video/mp4'))
        self.assertFalse(native_attachment_name_is_eligible(
            "attachment; filename=source.mp4; filename*=UTF-8''", 'video/mp4'))

    def test_malformed_or_ambiguous_headers_are_not_evidence(self):
        for header in ('inline; filename=source.mp4', 'attachment',
                       'attachment; filename="source.mp4', 'attachment; filename=source.mp4 trailing',
                       'attachment; filename=one.mp4; FILENAME=two.mp4',
                       "attachment; filename*=UTF-8''one.mp4; filename*=UTF-8''two.mp4",
                       "attachment; filename*0*=UTF-8''source; filename*1*=.mp4",
                       'attachment; filename="source.mp4"\r\nX-Other: value',
                       'attachment; filename="source.mp4"; broken',
                       'attachment; filename="source.mp4", inline'):
            with self.subTest(header=header):
                self.assertFalse(native_attachment_name_is_eligible(header, 'video/mp4'))

    def test_mime_extension_and_reasonable_basename_are_required(self):
        for mime, filename, expected in (
            ('video/mp4', '12345.mp4', True), ('video/mp4; charset=binary', 'source.MP4', True),
            ('video/webm', '12345.webm', True), ('video/quicktime', '12345.mov', True),
            ('video/mp4', 'source.bin', False), ('video/mp4', 'source.webm', False),
            ('video/webm', 'source.mp4', False), ('video/quicktime', 'source.mp4', False),
            ('application/octet-stream', 'source.mp4', False), ('', 'source.mp4', False),
            ('video/unknown', 'source.mp4', False), ('text/html', 'source.mp4', False),
            ('video/mp4', '.mp4', False), ('video/mp4', 'source', False),
            ('video/mp4', '../source.mp4', False), ('video/mp4', ' source.mp4', False),
            ('video/mp4', 'source.mp4 ', False), ('video/mp4', '...mp4', False),
            ('video/mp4', 'source\x00.mp4', False), ('video/mp4', 'source\u202e.mp4', False),
            ('video/mp4', 'a' * 256 + '.mp4', False),
        ):
            with self.subTest(mime=mime, filename=filename):
                self.assertEqual(native_attachment_name_is_eligible(
                    f'attachment; filename="{filename}"', mime), expected)


class RequestContextTests(SimpleTestCase):
    def test_defaults_are_not_known_requirements_and_only_named_sec_header_is_optional(self):
        defaults = {'User-Agent': 'browser', 'Accept': '*/*', 'Referer': 'https://source.example/',
                    'Origin': 'https://source.example', 'sEc-FeTcH-mOdE': 'navigate'}
        self.assertTrue(request_context_compatible(defaults))
        self.assertEqual(request_context_fingerprint(defaults), request_context_fingerprint())
        for header in ('Sec-Fetch-Site', 'Sec-Fetch-Dest', 'Sec-Secret', 'X-Api-Key', 'Cookie',
                       'Authorization', 'Proxy-Authorization'):
            with self.subTest(header=header):
                self.assertFalse(request_context_compatible({**defaults, header: 'value'}))
        self.assertFalse(request_context_compatible(defaults, required_headers=('rEfErEr',)))

    def test_explicit_required_conditions_cannot_be_overridden_by_false_flags(self):
        for context in ({'required_headers': ('Referer',)}, {'required_headers': ('User-Agent',)},
                        {'requires_cookie': True}, {'requires_context': True}, {'ip_bound': True},
                        {'headers_present': True}):
            with self.subTest(context=context):
                self.assertFalse(request_context_compatible(None, **context))
                self.assertNotEqual(request_context_fingerprint(None, **context), request_context_fingerprint())
        self.assertFalse(request_context_compatible({'Authorization': 'Bearer secret'}, requires_context=False))
        self.assertTrue(request_context_compatible({'Accept': '*/*'}, headers_present=True))
        self.assertFalse(request_context_compatible('unavailable'))

    def test_fingerprint_preserves_required_values_without_exposing_credentials(self):
        first = request_context_fingerprint({'Authorization': 'Bearer one'})
        self.assertEqual(first, request_context_fingerprint({'authorization': 'Bearer one'}))
        self.assertNotEqual(first, request_context_fingerprint({'Authorization': 'Bearer two'}))
        self.assertNotIn('Bearer', first)
        self.assertNotEqual(
            request_context_fingerprint({'Referer': 'https://one.example'}, required_headers=('Referer',)),
            request_context_fingerprint({'Referer': 'https://two.example'}, required_headers=('referer',)))
        self.assertEqual(request_context_fingerprint(requires_context=None),
                         request_context_fingerprint(requires_context=False))

    def test_malformed_required_headers_are_opaque_not_exceptions_or_empty_requirements(self):
        for required in (True, False, 0, 42, 1.5, {}, {'Referer': True}, b'Referer',
                         '', ' ', [None], ['Referer', 42], [['Referer']], [{'name': 'Referer'}]):
            with self.subTest(required=required):
                self.assertFalse(request_context_compatible(required_headers=required))
                fingerprint = request_context_fingerprint(required_headers=required)
                self.assertEqual(len(fingerprint), 64)
                self.assertNotEqual(fingerprint, request_context_fingerprint())
        for required in (None, (), [], set(), frozenset()):
            with self.subTest(empty=required):
                self.assertTrue(request_context_compatible(required_headers=required))
        self.assertEqual(request_context_fingerprint(required_headers=[' Origin ', 'REFERER', 'referer']),
                         request_context_fingerprint(required_headers=('origin', 'referer')))


class NativeAttachmentEvidenceTests(BoundaryNetworkCase):
    url = 'https://upos-sz-mirrorcos.bilivideo.com/opaque-resource'

    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.item = dict(type='video', url=self.url, browser_direct_fetch_safe=True,
                         requires_merge=False, requires_cookie=False)

    def media(self, *, disposition='attachment; filename="12345.mp4"', size='12345', mime='video/mp4'):
        headers = {'Content-Type': mime, 'Content-Disposition': disposition}
        if size is not None:
            headers['Content-Length'] = size
        return 200, headers, b'media bytes must not be read'

    def probe(self, **kwargs):
        return QualityProbe().probe(self.url, expected_type='video', **kwargs)

    def assert_native(self, expected):
        self.assertEqual(bool(native_attachment_expiry(self.url)), expected)
        self.assertEqual(browser_transport_hint(self.item)['download_strategy'],
                         'browser_native' if expected else 'browser_streaming')

    def tearDown(self):
        for response in self.responses:
            response.close.assert_called_once()
            self.assertEqual(response.raw.read_count, 0)
        super().tearDown()

    def test_source_basename_and_encoded_extension_enable_native_without_url_suffix(self):
        for disposition in ('attachment; filename="12345.mp4"',
                            "attachment; filename=bad.bin; filename*=UTF-8''12345%2Emp4"):
            with self.subTest(disposition=disposition):
                self.routes[self.url] = self.media(disposition=disposition)
                result = self.probe()
                self.assertTrue(result.reachable)
                self.assert_native(True)
        self.assertEqual([req.method for req, _ in self.sent], ['HEAD', 'HEAD'])

    def test_head_is_not_published_while_range_is_pending_and_inline_wins(self):
        def inline_range(request):
            self.assertEqual(request.method, 'GET')
            self.assert_native(False)
            return self.media(disposition='inline; filename="12345.mp4"')
        self.routes[self.url] = [self.media(size=None), inline_range]
        result = self.probe()
        self.assertEqual(result.method, 'RANGE')
        self.assertTrue(result.reachable)
        self.assert_native(False)

    def test_range_safety_rejection_revokes_head(self):
        self.routes[self.url] = [self.media(size=None), (302, {'Location': 'http://127.0.0.1/private'}, b'')]
        result = self.probe()
        self.assertTrue(result.security_rejected)
        self.assertFalse(result.reachable)
        self.assertEqual(self.urls(), [self.url, self.url])
        self.assert_native(False)

    def test_range_network_failure_preserves_final_head_without_refreshing_expiry(self):
        observed = 2000000000
        clock = [observed]
        def failed_range(request):
            clock[0] += 2
            raise requests.ConnectionError('ordinary transport failure')
        self.routes[self.url] = [self.media(size=None), failed_range, failed_range]
        with patch('xhs_download.services.quality.time.time', side_effect=lambda: clock[0]):
            result = self.probe()
            self.assertTrue(result.reachable)
            self.assertEqual(result.method, 'HEAD')
            self.assertEqual(native_attachment_expiry(self.url), (observed + 60) * 1000)
            self.assert_native(True)

    def test_range_explicit_negative_or_terminal_failure_supersedes_head(self):
        for fallback in ((403, {}, b''), self.media(mime='text/html'),
                         (302, {}, b''), requests.TooManyRedirects('loop')):
            with self.subTest(fallback=fallback):
                self.routes[self.url] = [self.media(size=None), fallback]
                result = self.probe()
                self.assertFalse(result.reachable)
                self.assertEqual(result.method, 'RANGE')
                self.assert_native(False)

    def test_range_bad_filename_or_mime_cannot_reuse_head(self):
        for disposition, mime in (('attachment; filename=bad.bin', 'video/mp4'),
                                  ('attachment; filename=bad.webm', 'video/mp4'),
                                  ('attachment', 'video/mp4'),
                                  ('attachment; filename=source.mp4', 'application/octet-stream')):
            with self.subTest(disposition=disposition, mime=mime):
                self.routes[self.url] = [self.media(size=None), self.media(disposition=disposition, mime=mime)]
                self.assertEqual(self.probe().method, 'RANGE')
                self.assert_native(False)

    def test_later_negative_probe_revokes_old_fact(self):
        for negative in (self.media(disposition='inline'), (404, {}, b''),
                         self.media(disposition='attachment; filename=source.bin')):
            with self.subTest(negative=negative):
                self.routes[self.url] = self.media()
                self.probe()
                self.assert_native(True)
                self.routes[self.url] = negative
                self.probe(allow_fallback=False)
                self.assert_native(False)

    def test_range_first_uses_final_head_and_disabled_fallback_uses_primary(self):
        self.routes[self.url] = [self.media(size=None), self.media(disposition='inline')]
        self.assertEqual(self.probe(strategy='range_first').method, 'HEAD')
        self.assert_native(False)
        self.routes[self.url] = self.media(size=None)
        self.assertEqual(self.probe(allow_fallback=False).method, 'HEAD')
        self.assert_native(True)

    def test_default_headers_in_normal_size_probe_naturally_publish_native_evidence(self):
        self.routes[self.url] = self.media()
        self.assertEqual(probe_media_size(self.url), 12345)
        request = self.sent[0][0]
        self.assertEqual(request.method, 'HEAD')
        self.assertEqual(request.headers['Referer'], 'https://www.bilibili.com/')
        self.assertTrue(request.headers['User-Agent'])
        self.assertTrue(request.headers['Accept'])
        self.assert_native(True)
        self.assertGreater(ensure_native_attachment_evidence(self.url, authorized_resource=True), 0)
        self.assertEqual(len(self.sent), 1)

    def test_required_context_never_publishes_or_revokes_unrelated_evidence(self):
        self.routes[self.url] = self.media()
        for name in ('Cookie', 'Authorization', 'Proxy-Authorization', 'X-Access-Token', 'Sec-Fetch-Site'):
            with self.subTest(header=name):
                self.probe()
                self.assert_native(True)
                self.assertEqual(native_attachment_expiry(self.url, {name: 'context'}), 0)
                for field in ('headers', 'http_headers'):
                    self.assertEqual(browser_transport_hint(
                        {**self.item, field: {name: 'context'}}), {})
                self.assertTrue(self.probe(headers={name: 'context'}).reachable)
                self.assertEqual(native_attachment_expiry(self.url, {name: 'context'}), 0)
                self.assert_native(True)
        for context in ({'required_headers': ('Referer',)}, {'requires_context': True},
                        {'requires_cookie': True}, {'ip_bound': True}):
            with self.subTest(context=context):
                headers = {'Referer': 'https://source.example/'}
                result = self.probe(headers=headers, **context)
                self.assertTrue(result.reachable)
                self.assertEqual(result.native_attachment_expires_at, 0)
                self.assertEqual(native_attachment_expiry(self.url, headers, **context), 0)
                calls = len(self.sent)
                self.assertEqual(ensure_native_attachment_evidence(
                    self.url, headers, authorized_resource=True, **context), 0)
                self.assertEqual(len(self.sent), calls)
                self.assert_native(True)

    def test_final_negative_revokes_defaults_but_contextual_failure_is_isolated(self):
        self.routes[self.url] = self.media()
        self.probe(headers={'User-Agent': 'browser', 'Referer': 'https://source.example'})
        self.assert_native(True)
        self.routes[self.url] = 403, {}, b''
        self.probe(headers={'Authorization': 'secret'}, allow_fallback=False)
        self.assert_native(True)
        self.probe(headers={'Accept': '*/*'}, allow_fallback=False)
        self.assert_native(False)

    def test_malformed_requirements_cannot_reuse_positive_evidence_or_trigger_probe(self):
        self.routes[self.url] = self.media()
        self.probe()
        for required in (42, False, ['Referer', None]):
            with self.subTest(required=required):
                self.assertEqual(native_attachment_expiry(self.url, required_headers=required), 0)
                self.assertEqual(ensure_native_attachment_evidence(
                    self.url, required_headers=required, authorized_resource=True), 0)
        self.assertEqual(len(self.sent), 1)
        self.assert_native(True)

    def test_ensure_caches_negative_facts_without_repeated_requests_and_expires_them(self):
        self.routes[self.url] = self.media(disposition='inline')
        now = 2000000000
        with patch('xhs_download.services.quality.time.time', return_value=now):
            for _ in range(2):
                self.assertEqual(ensure_native_attachment_evidence(self.url), 0)
            self.assertEqual(len(self.sent), 1)
        self.routes[self.url] = self.media()
        with patch('xhs_download.services.quality.time.time', return_value=now + 61):
            self.assertGreater(ensure_native_attachment_evidence(self.url), 0)
            self.assertEqual(len(self.sent), 2)

    def test_ensure_does_not_extend_positive_ttl_or_retry_unknown_result(self):
        self.routes[self.url] = self.media()
        now = 2000000000
        with patch('xhs_download.services.quality.time.time', return_value=now):
            expiry = ensure_native_attachment_evidence(self.url)
        with patch('xhs_download.services.quality.time.time', return_value=now + 30):
            self.assertEqual(ensure_native_attachment_evidence(self.url), expiry)
            self.assertEqual(len(self.sent), 1)
        cache.clear()
        self.routes[self.url] = requests.ConnectionError('unavailable')
        self.assertEqual(ensure_native_attachment_evidence(self.url), 0)
        calls = len(self.sent)
        self.assertEqual(ensure_native_attachment_evidence(self.url), 0)
        self.assertEqual(len(self.sent), calls)

    def test_authorized_unknown_cdn_is_safe_without_cors_or_domain_exceptions(self):
        url = 'https://new-cdn.example/opaque-resource'
        self.routes[url] = self.media()
        self.assertEqual(ensure_native_attachment_evidence(url), 0)
        self.assertEqual(self.sent, [])
        self.assertGreater(ensure_native_attachment_evidence(url, {'Referer': 'https://source.example'},
                                                            authorized_resource=True), 0)
        self.assertEqual(self.urls(), [url])
        self.assertNotIn('Access-Control-Allow-Origin', self.responses[0].headers)

    def test_authorized_unknown_cdn_keeps_url_dns_and_redirect_safety(self):
        for url in ('https://127.0.0.1/private', 'https://[::1]/private',
                    'https://new-cdn.example:8080/video', 'https://user:pass@new-cdn.example/video',
                    'file:///etc/passwd', 'https://new-cdn.example/with space'):
            with self.subTest(url=url):
                result = QualityProbe(authorized_resource=True).probe(url)
                self.assertTrue(result.security_rejected)
                self.assertEqual(self.sent, [])
        url = 'https://new-cdn.example/video'
        self.dns['new-cdn.example'] = ['8.8.8.8', '10.0.0.1']
        self.assertTrue(QualityProbe(authorized_resource=True).probe(url).security_rejected)
        self.assertEqual(self.sent, [])
        self.dns['new-cdn.example'] = ['8.8.8.8']
        self.routes[url] = 302, {'Location': 'http://169.254.169.254/metadata'}, b''
        self.assertTrue(QualityProbe(authorized_resource=True).probe(url).security_rejected)
        self.assertEqual(self.urls(), [url])

    def test_unknown_charset_cannot_create_native_evidence_from_lower_priority_name(self):
        self.routes[self.url] = self.media(disposition="attachment; filename=good.mp4; filename*=unknown''bad.bin")
        self.assertEqual(ensure_native_attachment_evidence(self.url), 0)
        self.assert_native(False)

    def test_final_response_hosts_expire_and_negative_evidence_removes_them(self):
        final = 'https://new-cdn.example/selected.mp4'
        self.routes[self.url] = 302, {'Location': final}, b''
        self.routes[final] = self.media(disposition='inline')
        QualityProbe(authorized_resource=True).probe(self.url, expected_type='video')
        hint = browser_transport_hint(self.item)
        self.assertEqual(hint['browser_response_hosts'][-1], 'new-cdn.example')
        self.assertEqual(hint['download_strategy'], 'browser_streaming')
        with patch('xhs_download.services.quality.time.time', return_value=hint['browser_response_hosts_expires_at'] / 1000):
            self.assertNotIn('browser_response_hosts', browser_transport_hint(self.item))
        self.routes[final] = 403, {}, b''
        QualityProbe(authorized_resource=True).probe(self.url, expected_type='video')
        self.assertNotIn('browser_response_hosts', browser_transport_hint(self.item))
        self.assert_native(False)

    def test_expired_evidence_and_expired_selection_are_not_native(self):
        self.routes[self.url] = self.media()
        self.probe()
        expiry = native_attachment_expiry(self.url)
        with patch('xhs_download.services.quality.time.time', return_value=expiry / 1000):
            self.assert_native(False)
        for expiry in (0, 1, 'invalid', float('inf')):
            self.assertEqual(browser_transport_hint(
                {**self.item, 'selection_expires_at': expiry})['download_strategy'], 'browser_streaming')

    def test_evidence_never_authorizes_another_url_or_unsafe_resource(self):
        self.routes[self.url] = self.media()
        self.probe()
        self.assertEqual(native_attachment_expiry(self.url + '?different=1'), 0)
        for fields in ({'browser_direct_fetch_safe': False}, {'requires_context': True},
                       {'requires_cookie': True}, {'has_drm': True}, {'requires_merge': True}):
            self.assertEqual(browser_transport_hint({**self.item, **fields}), {})
