"""Native eligibility through the real probe; only final HTTP/DNS is replaced."""
from unittest.mock import patch

import requests
from django.core.cache import cache
from django.test import SimpleTestCase

from .services.provider_strategy import browser_transport_hint
from .services.quality import (
    QualityProbe, attachment_filename, native_attachment_expiry,
    native_attachment_name_is_eligible,
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
            ("attachment; filename=bad.bin; filename*=UTF-8''12345%2Emp4", '12345.mp4'),
            ("attachment; filename*=UTF-8''12345%2Emp4; filename=bad.bin", '12345.mp4'),
            ("attachment; filename=good.mp4; filename*=UTF-8''bad.bin", 'bad.bin'),
            ("attachment; filename*=UTF-8''bad.bin; filename=good.mp4", 'bad.bin'),
            ('attachment; filename="literal%2Emp4"', 'literal%2Emp4'),
        )
        for header, expected in cases:
            with self.subTest(header=header):
                self.assertEqual(attachment_filename(header), expected)

    def test_invalid_extended_parameter_can_only_use_valid_regular_fallback(self):
        for value in ("UTF-8''broken%2.mp4", "UTF-8''broken%GG.mp4", "UTF-8''%FF.mp4",
                      "X-UNKNOWN''source.mp4", "UTF-8'source.mp4", '"UTF-8\'\'source.mp4"'):
            with self.subTest(value=value):
                self.assertFalse(native_attachment_name_is_eligible(
                    'attachment; filename*=' + value, 'video/mp4'))
                self.assertEqual(attachment_filename(
                    'attachment; filename=source.mp4; filename*=' + value), 'source.mp4')
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

    def test_contextual_probe_never_publishes_and_revokes_old_contextless_fact(self):
        self.routes[self.url] = self.media()
        for name in ('Referer', 'referer', 'Origin', 'Cookie', 'Authorization',
                     'Proxy-Authorization', 'X-Access-Token', 'User-Agent', 'Accept'):
            with self.subTest(header=name):
                self.probe()
                self.assert_native(True)
                self.assertEqual(native_attachment_expiry(self.url, {name: 'context'}), 0)
                for field in ('headers', 'http_headers'):
                    self.assertEqual(browser_transport_hint(
                        {**self.item, field: {name: 'context'}})['download_strategy'], 'browser_streaming')
                self.assertTrue(self.probe(headers={name: 'context'}).reachable)
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
