"""Resolver/HTTP-boundary tests, not live Provider success evidence."""
import socket
from unittest.mock import patch

import requests

from .services.common import XhsDownloadError, XhsSecurityError, request_get_with_retry
from .services.downloads import probe_media_size
from .services.quality import QualityProbe
from .tests_request_redirect_boundaries import BoundaryNetworkCase


class DNSRetryTests(BoundaryNetworkCase):
    url = 'https://public.example/a'

    def test_temporary_dns_once_then_success_retries_before_sending(self):
        resolved = self.resolve('public.example', 443)
        self.routes[self.url] = (200, {}, b'ok')
        with patch('socket.getaddrinfo', side_effect=[socket.gaierror(socket.EAI_AGAIN, 'temporary'), resolved]) as dns:
            response = request_get_with_retry(self.url, retries=3)
        self.assertEqual(response.content, b'ok')
        self.assertEqual(dns.call_count, 2)
        self.assertEqual(self.urls(), [self.url])
        self.sleep.assert_called_once()

    def test_persistent_temporary_dns_is_bounded_without_http(self):
        with patch('socket.getaddrinfo', side_effect=socket.gaierror(socket.EAI_AGAIN, 'temporary')) as dns:
            with self.assertRaises(XhsDownloadError) as failure:
                request_get_with_retry(self.url, retries=3)
        self.assertNotIsInstance(failure.exception, XhsSecurityError)
        self.assertTrue(failure.exception.temporary)
        self.assertEqual(dns.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)
        self.assertEqual(self.urls(), [])

    def test_nonexistent_dns_is_terminal_resolution_failure(self):
        with patch('socket.getaddrinfo', side_effect=socket.gaierror(socket.EAI_NONAME, 'nonexistent')) as dns:
            with self.assertRaises(XhsDownloadError) as failure:
                request_get_with_retry(self.url, retries=3)
        self.assertFalse(failure.exception.temporary)
        self.assertNotIsInstance(failure.exception, XhsSecurityError)
        self.assertIn('DNS', str(failure.exception))
        self.assertNotIn('跳转', str(failure.exception))
        self.assertEqual(dns.call_count, 1)
        self.sleep.assert_not_called()
        self.assertEqual(self.urls(), [])

    def test_private_dns_is_terminal_security_failure(self):
        self.dns['public.example'] = ['10.0.0.7']
        with self.assertRaises(XhsSecurityError) as failure:
            request_get_with_retry(self.url, retries=3)
        self.assertNotIn('跳转', str(failure.exception))
        self.sleep.assert_not_called()
        self.assertEqual(self.urls(), [])

    def test_temporary_dns_backoff_uses_original_deadline(self):
        with patch('socket.getaddrinfo', side_effect=socket.gaierror(socket.EAI_AGAIN, 'temporary')) as dns:
            with self.assertRaises(requests.Timeout):
                request_get_with_retry(self.url, retries=8, sleep_seconds=1, deadline_monotonic=1002)
        self.assertEqual(dns.call_count, 2)
        self.assertEqual(self.sleep.call_count, 1)
        self.assertEqual(self.urls(), [])

    def test_dns_consumes_deadline_no_send_or_retry(self):
        def late_resolution(*args, **kwargs):
            self.advance(6)
            return self.resolve(*args, **kwargs)
        with patch('socket.getaddrinfo', side_effect=late_resolution) as dns:
            with self.assertRaises(requests.Timeout):
                request_get_with_retry(self.url, retries=3, deadline_monotonic=1005)
        self.assertEqual(dns.call_count, 1)
        self.sleep.assert_not_called()
        self.assertEqual(self.urls(), [])

    def test_empty_dns_is_resolution_failure_not_forbidden_address(self):
        self.dns['public.example'] = []
        with self.assertRaises(XhsDownloadError) as failure:
            request_get_with_retry(self.url, retries=3)
        self.assertNotIsInstance(failure.exception, XhsSecurityError)
        self.assertFalse(failure.exception.temporary)
        self.assertEqual(self.urls(), [])
        self.sleep.assert_not_called()

    def test_service_source_dns_failure_is_network_not_unsupported(self):
        from .services.quality_execution import _execute_service
        from .services.provider_lab.contracts import ProviderLabError
        with patch('socket.getaddrinfo', side_effect=socket.gaierror(socket.EAI_AGAIN, 'temporary')), \
                patch('xhs_download.services.quality_execution.service_configured', return_value=True):
            with patch('xhs_download.services.platforms.tiantianjiexi_generic.TianTianJieXiGenericProvider.parse') as parse:
                with self.assertRaises(ProviderLabError) as failure:
                    _execute_service('tiantianjiexi_generic', self.url, 5, None)
        self.assertEqual(failure.exception.code, 'PROVIDER_UPSTREAM_FAILED')
        self.assertEqual(failure.exception.detail, 'SERVICE_NETWORK_ERROR')
        parse.assert_not_called()

    def test_candidate_and_resource_validation_remain_fail_closed(self):
        from .services.upstream_candidates import candidate_url
        from .services.upstream_delivery import safe_resource_url
        with patch('socket.getaddrinfo', side_effect=socket.gaierror(socket.EAI_AGAIN, 'temporary')):
            self.assertEqual(candidate_url(self.url), '')
            self.assertEqual(safe_resource_url(self.url), '')
        self.assertEqual(self.urls(), [])


class QualityProbeBoundaryTests(BoundaryNetworkCase):
    url = 'https://v3-web.douyinvod.com/video.mp4'

    def assert_closed_without_body_reads(self):
        for response in self.responses:
            response.close.assert_called_once()
            self.assertTrue(response.raw.closed)
            self.assertEqual(response.raw.read_count, 0)

    def test_private_second_hop_no_send_no_fallback(self):
        self.routes[self.url] = (302, {'Location': 'http://127.0.0.1/private'}, b'')
        result = QualityProbe().probe(self.url)
        self.assertFalse(result.reachable)
        self.assertEqual(self.urls(), [self.url])
        self.assert_closed_without_body_reads()

    def test_missing_location_is_reported_and_closed_without_fallback(self):
        self.routes[self.url] = (302, {}, b'')
        result = QualityProbe().probe(self.url)
        self.assertFalse(result.reachable)
        self.assertIn('Location', result.error)
        self.assertEqual(self.urls(), [self.url])
        self.assert_closed_without_body_reads()

    def test_private_target_size_consumer_returns_unknown_without_sending(self):
        self.dns['v3-web.douyinvod.com'] = ['10.0.0.7']
        self.assertEqual(probe_media_size(self.url), 0)
        self.assertEqual(self.urls(), [])

    def test_public_hostname_resolving_private_no_send(self):
        self.dns['v3-web.douyinvod.com'] = ['10.0.0.7']
        result = QualityProbe().probe(self.url)
        self.assertFalse(result.reachable)
        self.assertEqual(self.urls(), [])

    def test_invalid_initial_targets_report_terminal_failure_without_http(self):
        for url in ('https://[broken', 'ftp://v3-web.douyinvod.com/video',
                    'https://v3-web.douyinvod.com:8082/video',
                    'https://user:password@v3-web.douyinvod.com/video'):
            with self.subTest(url=url):
                result = QualityProbe().probe(url)
                self.assertFalse(result.reachable)
                self.assertTrue(result.terminal)
        self.assertEqual(self.urls(), [])

    def test_domain_policy_blocks_unapproved_intermediate_before_send(self):
        self.routes[self.url] = (302, {'Location': 'https://unapproved.example/a'}, b'')
        result = QualityProbe().probe(self.url)
        self.assertFalse(result.reachable)
        self.assertEqual(self.urls(), [self.url])
        self.assert_closed_without_body_reads()

    def test_head_relative_and_public_multihop_preserves_method(self):
        middle = 'https://v3-web.douyinvod.com/next'
        final = 'https://v11-default.365yg.com/final'
        self.routes[self.url] = (302, {'Location': '/next'}, b'')
        self.routes[middle] = (307, {'Location': final}, b'')
        self.routes[final] = (200, {'Content-Length': '1234', 'Content-Type': 'video/mp4'}, b'must not read')
        result = QualityProbe().probe(self.url, expected_type='video')
        self.assertTrue(result.reachable)
        self.assertEqual(result.final_url, final)
        self.assertEqual(result.size_bytes, 1234)
        self.assertEqual(result.method, 'HEAD')
        self.assertEqual([req.method for req, _ in self.sent], ['HEAD'] * 3)
        self.assertNotIn('Referer', self.sent[0][0].headers)
        self.assert_closed_without_body_reads()

    def test_range_relative_redirect_preserves_range_and_method(self):
        target = 'https://v3-web.douyinvod.com/next'
        self.routes[self.url] = (303, {'Location': '/next'}, b'')
        self.routes[target] = (206, {'Content-Range': 'bytes 0-0/4567', 'Content-Type': 'video/mp4'}, b'')
        result = QualityProbe().probe(self.url, strategy='range_first')
        self.assertTrue(result.reachable)
        self.assertEqual(result.size_bytes, 4567)
        self.assertEqual([req.method for req, _ in self.sent], ['GET', 'GET'])
        self.assertTrue(all(req.headers['Range'] == 'bytes=0-0' for req, _ in self.sent))
        self.assert_closed_without_body_reads()

    def test_head_405_then_range_206_actual_size_consumer(self):
        self.routes[self.url] = [
            (405, {}, b'no HEAD'),
            (206, {'Content-Range': 'bytes 0-0/5678', 'Content-Length': '1', 'Content-Type': 'video/mp4'}, b'x'),
        ]
        self.assertEqual(probe_media_size(self.url), 5678)
        self.assertEqual([req.method for req, _ in self.sent], ['HEAD', 'GET'])
        self.assertEqual(self.sent[1][0].headers['Range'], 'bytes=0-0')
        self.assert_closed_without_body_reads()

    def test_head_missing_size_falls_back_and_range_ignored_is_not_read(self):
        self.routes[self.url] = [
            (200, {'Content-Type': 'video/mp4'}, b''),
            (200, {'Content-Length': '900000000', 'Content-Type': 'video/mp4'}, b'must not read'),
        ]
        result = QualityProbe().probe(self.url)
        self.assertTrue(result.reachable)
        self.assertEqual(result.size_bytes, 900000000)
        self.assertEqual(result.method, 'RANGE')
        self.assert_closed_without_body_reads()

    def test_reachable_head_with_unknown_size_survives_network_fallback_failure(self):
        self.routes[self.url] = [
            (200, {'Content-Type': 'video/mp4'}, b''),
            requests.ConnectionError('fixture network failure'),
            requests.ConnectionError('fixture network failure'),
        ]
        result = QualityProbe().probe(self.url)
        self.assertTrue(result.reachable)
        self.assertEqual(result.size_bytes, 0)
        self.assertEqual(result.method, 'HEAD')
        self.assert_closed_without_body_reads()

    def test_fallback_security_refusal_does_not_return_prior_head_success(self):
        self.routes[self.url] = [
            (200, {'Content-Type': 'video/mp4'}, b''),
            (302, {'Location': 'http://127.0.0.1/private'}, b''),
        ]
        result = QualityProbe().probe(self.url)
        self.assertFalse(result.reachable)
        self.assertTrue(result.security_rejected)
        self.assertEqual(self.urls(), [self.url, self.url])
        self.assert_closed_without_body_reads()

    def test_shared_deadline_prevents_fallback_send(self):
        def slow_head(request):
            self.advance(7)
            return 405, {}, b''
        self.routes[self.url] = slow_head
        result = QualityProbe().probe(self.url, timeout=6)
        self.assertFalse(result.reachable)
        self.assertEqual(self.urls(), [self.url])
        self.assert_closed_without_body_reads()

    def test_redirect_budget_shared_between_head_and_range(self):
        urls = [self.url] + [f'https://v3-web.douyinvod.com/hop{i}' for i in range(3)]
        for initial, target in zip(urls, urls[1:]):
            self.routes[initial] = (302, {'Location': target}, b'')
        self.routes[urls[-1]] = (405, {}, b'')
        result = QualityProbe().probe(self.url)
        self.assertFalse(result.reachable)
        # HEAD used three of the five redirects; Range cannot start another full chain.
        self.assertEqual(self.urls(), urls + urls[:3])
        self.assert_closed_without_body_reads()

    def test_html_is_not_media_and_unknown_length_stays_zero(self):
        self.routes[self.url] = (200, {'Content-Type': 'text/html'}, b'login')
        result = QualityProbe().probe(self.url, expected_type='video')
        self.assertFalse(result.reachable)
        self.assertIn('Unexpected content type', result.error)
        self.routes[self.url] = (200, {'Content-Type': 'video/mp4'}, b'')
        result = QualityProbe().probe(self.url)
        self.assertTrue(result.reachable)
        self.assertEqual(result.size_bytes, 0)
        self.assert_closed_without_body_reads()
