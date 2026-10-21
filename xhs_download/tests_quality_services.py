from unittest.mock import Mock, patch
from dataclasses import replace

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services import quality_execution as execution
from .services.provider_lab.contracts import ProviderLabError
from .services.provider_lab.providers import parse_result_to_lab, run_provider, PROVIDER_REGISTRY


SOURCE = 'https://example.org/post/123'
MEDIA = 'https://cdn.example.org/media'


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class SharedServiceExecutionTests(SimpleTestCase):
    def test_explicit_tiantian_unsupported_is_not_http_or_account_failure(self):
        from .services.platforms.tiantianjiexi_generic import ensure_api_success, TianTianJieXiUnsupportedError, TianTianJieXiApiError
        with self.assertRaises(TianTianJieXiUnsupportedError) as caught:
            ensure_api_success({'code':451, 'msg':'暂不支持该链接~'})
        with self.mock_client('tiantianjiexi_generic') as client:
            client.side_effect=caught.exception
            with self.assertRaises(ProviderLabError) as mapped:
                execution.execute_provider('tiantian_existing', SOURCE, service_authorized=True)
        self.assertEqual(mapped.exception.code, 'PROVIDER_UNSUPPORTED')
        with self.assertRaises(TianTianJieXiApiError) as unknown:
            ensure_api_success({'code':451, 'msg':'different meaning'})
        self.assertNotIsInstance(unknown.exception, TianTianJieXiUnsupportedError)

    def test_business_codes_are_bounded_and_not_http_statuses(self):
        from .services.platforms.tiantianjiexi_generic import ensure_api_success, TianTianJieXiApiError
        for value, expected in [(403, '403'), (-2, '-2'), ('502', '502'),
                                (1001, '1001'), ('-1001', '-1001'),
                                (99999, '99999'), ('-99999', '-99999'),
                                ('secret-value', 'UNKNOWN'), (123456789, 'UNKNOWN'),
                                (100000, 'UNKNOWN'), ('-100000', 'UNKNOWN'),
                                (None, 'UNKNOWN'), ('４０３', 'UNKNOWN')]:
            with self.assertRaises(TianTianJieXiApiError) as caught:
                ensure_api_success({'code': value, 'message': 'private upstream text'})
            self.assertEqual(caught.exception.diagnostic_detail, 'SERVICE_BUSINESS_CODE_' + expected)
        # The diagnostic change must not turn accepted business responses into failures.
        ensure_api_success({'code': 200})

    def test_five_digit_business_code_reaches_safe_lab_detail(self):
        from .services.platforms.tiantianjiexi_generic import ensure_api_success, TianTianJieXiApiError
        from .services.provider_lab.providers import _failure_result
        for code in (1001, -1001, 99999, -99999):
            with self.subTest(code=code), self.assertRaises(TianTianJieXiApiError) as caught:
                ensure_api_success({'code': code, 'data': '', 'msg': 'upstream failure'})
            detail = caught.exception.diagnostic_detail
            result = _failure_result(
                descriptor=PROVIDER_REGISTRY['tiantian_existing'], request_id='safe',
                elapsed_ms=1, executed=True,
                error=ProviderLabError('PROVIDER_UPSTREAM_FAILED', detail=detail),
            )
            self.assertEqual(result['error_detail'], f'SERVICE_BUSINESS_CODE_{code}')

    def test_json_phase_details_are_allowlisted_through_helper_and_lab(self):
        from .services.platforms.douyin_kukutool import kukutool_failure_detail
        from .services.provider_lab.providers import _failure_result
        detail = 'SERVICE_JSON_AUTH_HTTP_200_TYPE_HTML'
        self.assertEqual(kukutool_failure_detail('KUKUTOOL_SAFE_ERROR:' + detail), detail)
        result = _failure_result(descriptor=PROVIDER_REGISTRY['kukutool_existing'], request_id='safe',
            elapsed_ms=1, executed=True, error=ProviderLabError('PROVIDER_UPSTREAM_FAILED', detail=detail))
        self.assertEqual(result['error_detail'], detail)
        for bad in [detail + '\nsecret', 'SERVICE_BUSINESS_CODE_1234567',
                    'SERVICE_BUSINESS_CODE_100000', 'SERVICE_BUSINESS_CODE_-100000',
                    'SERVICE_BUSINESS_CODE_１００１', 'SERVICE_BUSINESS_CODE_1001\nsecret',
                    'SERVICE_JSON_AUTH_HTTP_200_TYPE_text/html', {'secret': 'value'}]:
            self.assertEqual(ProviderLabError('PROVIDER_UPSTREAM_FAILED', detail=bad).detail, '')

    def test_empty_media_result_has_distinct_safe_detail(self):
        from .services.platforms.tiantianjiexi_generic import TianTianJieXiGenericProvider
        from .services.provider_reliability import ProviderResultInvalidError
        with self.assertRaises(ProviderResultInvalidError) as caught:
            TianTianJieXiGenericProvider().validate_payload({'images': [], 'videos': []})
        with self.mock_client('tiantianjiexi_generic') as client:
            client.side_effect = caught.exception
            with self.assertRaises(ProviderLabError) as mapped:
                execution.execute_provider('tiantian_existing', SOURCE, service_authorized=True)
        self.assertEqual(mapped.exception.code, 'PROVIDER_RESULT_EMPTY')
        self.assertEqual(mapped.exception.detail, 'SERVICE_EMPTY_RESULT')

    def test_tiantian_transport_errors_are_classified_without_network(self):
        import requests
        import tempfile
        from pathlib import Path
        from .services.platforms.tiantianjiexi_generic import TianTianJieXiApiClient, TianTianJieXiApiError
        response = Mock(status_code=403)
        cases = [(requests.HTTPError('private', response=response), 'SERVICE_HTTP_403'),
                 (requests.exceptions.SSLError('private'), 'SERVICE_TLS_ERROR'),
                 (requests.ConnectionError('private'), 'SERVICE_NETWORK_ERROR'),
                 (requests.exceptions.JSONDecodeError('private', 'x', 0), 'SERVICE_INVALID_JSON')]
        for error, detail in cases:
            session = Mock()
            session.post.side_effect = error
            with tempfile.TemporaryDirectory() as directory, \
                    patch('xhs_download.services.platforms.tiantian_serial.lock_root', return_value=Path(directory)), \
                    patch('xhs_download.services.platforms.tiantianjiexi_generic.api_configured', return_value=True):
                with self.assertRaises(TianTianJieXiApiError) as caught:
                    TianTianJieXiApiClient(session=session).parse_url(SOURCE)
            self.assertEqual(caught.exception.diagnostic_detail, detail)
            self.assertNotIn('private', str(caught.exception))

    def test_helper_failure_is_not_executor_missing_and_details_are_safe(self):
        from .services.platforms.douyin_kukutool import KukutoolHelperError, kukutool_failure_detail
        for stderr, expected in (
            ('KUKUTOOL_SAFE_ERROR:SERVICE_HTTP_403\nsecret body', 'SERVICE_HTTP_403'),
            ('KUKUTOOL_SAFE_ERROR:https://secret.invalid/key', 'SERVICE_HELPER_FAILED'),
            ('raw credentials', 'SERVICE_HELPER_FAILED')):
            detail = kukutool_failure_detail(stderr)
            self.assertEqual(detail, expected)
            with self.mock_client('kukutool_generic') as client:
                client.side_effect = KukutoolHelperError(stderr, detail)
                with self.assertRaises(ProviderLabError) as caught:
                    execution.execute_provider('kukutool_existing', SOURCE, service_authorized=True)
                self.assertEqual(caught.exception.code, 'PROVIDER_UPSTREAM_FAILED')
                self.assertEqual(caught.exception.detail, expected)
                self.assertNotIn('secret', str(caught.exception))

    def test_tiantian_diagnostic_detail_survives_mapping(self):
        from .services.platforms.tiantianjiexi_generic import TianTianJieXiApiError
        for detail in ('SERVICE_HTTP_418', 'SERVICE_NETWORK_ERROR', 'SERVICE_TLS_ERROR', 'SERVICE_INVALID_JSON'):
            with self.mock_client('tiantianjiexi_generic') as client:
                client.side_effect = TianTianJieXiApiError('private payload', diagnostic_detail=detail)
                with self.assertRaises(ProviderLabError) as caught:
                    execution.execute_provider('tiantian_existing', SOURCE, service_authorized=True)
                self.assertEqual(caught.exception.detail, detail)

    def test_timeout_phase_is_preserved_without_raw_error(self):
        from .services.upstream_parsehub import ParseHubBridgeError
        match = {'provider_id': 'parsehub_generic'}
        for during_match in (True, False):
            with self.subTest(during_match=during_match), \
                    patch('xhs_download.services.upstream_parsehub._call') as call, \
                    patch('xhs_download.services.upstream_parsehub.valid_match', return_value=True), \
                    patch.object(execution, '_select_match', return_value=match), \
                    patch('xhs_download.services.upstream_parsehub.parse_upstream_url') as parse:
                call.return_value = [match]
                (call if during_match else parse).side_effect = ParseHubBridgeError('execution_timeout')
                with self.assertRaises(ProviderLabError) as caught:
                    execution._execute_upstream('parsehub_generic', SOURCE, 25)
                self.assertEqual(caught.exception.code, 'PROVIDER_TIMEOUT')
                self.assertEqual(caught.exception.detail, 'MATCH_DEADLINE' if during_match else 'PARSE_DEADLINE')

    def test_failure_detail_is_allowlisted_and_serialized(self):
        from .services.provider_lab.providers import _failure_result
        for supplied, expected in (('PARSE_DEADLINE', 'PARSE_DEADLINE'),
                                   ('secret https://example.org/token', '')):
            result = _failure_result(descriptor=PROVIDER_REGISTRY['parsehub_repo'],
                request_id='safe', elapsed_ms=5, executed=True,
                error=ProviderLabError('PROVIDER_TIMEOUT', detail=supplied))
            self.assertEqual(result['error_detail'], expected)
            self.assertEqual(result['status'], 'timeout')

    def test_service_errors_keep_safe_classification_without_exception_text(self):
        from .services.platforms.tiantianjiexi_generic import (
            TianTianJieXiAuthError, TianTianJieXiRateLimitError, TianTianJieXiApiError)
        for error, code in ((TianTianJieXiAuthError, 'PROVIDER_AUTH_FAILED'),
                            (TianTianJieXiRateLimitError, 'PROVIDER_RATE_LIMITED'),
                            (TianTianJieXiApiError, 'PROVIDER_UPSTREAM_FAILED'),
                            (OSError, 'PROVIDER_EXECUTOR_FAILED'),
                            (RuntimeError, 'PROVIDER_PROCESS_FAILED')):
            with self.subTest(code=code), self.mock_client('tiantianjiexi_generic') as client:
                client.side_effect = error('secret credential and upstream body')
                with self.assertRaises(ProviderLabError) as caught:
                    execution.execute_provider('tiantian_existing', SOURCE, service_authorized=True)
                self.assertEqual(caught.exception.code, code)
                self.assertNotIn('secret', str(caught.exception))

    def test_late_configuration_failure_is_not_counted_as_execution(self):
        descriptor = replace(PROVIDER_REGISTRY['parsehub_repo'], enabled_setting='',
                             availability=lambda: True,
                             runner=lambda **kwargs: (_ for _ in ()).throw(ProviderLabError('PROVIDER_NOT_CONFIGURED')))
        with patch.dict(PROVIDER_REGISTRY, {'parsehub_repo': descriptor}):
            result = run_provider(provider_id='parsehub_repo', source=SOURCE, user_id=7)
        self.assertEqual(result['status'], 'not_configured')
        self.assertFalse(result['executed'])

    def setUp(self):
        cache.clear()
        self.gate = patch.object(execution, 'service_configured', return_value=True)
        self.gate.start()
        self.addCleanup(self.gate.stop)
        guard = patch('xhs_download.services.upstream_candidates.validate_public_download_url',
                      side_effect=lambda url: url)
        guard.start()
        self.addCleanup(guard.stop)

    def mock_client(self, provider):
        module, name = ('tiantianjiexi_generic', 'TianTianJieXiGenericProvider') if provider == 'tiantianjiexi_generic' else (
            'kukutool_generic', 'KukutoolGenericProvider')
        return patch(f'xhs_download.services.platforms.{module}.{name}.parse')

    def test_run_provider_consent_reaches_real_shared_service_executor(self):
        for lab_id, provider in (('tiantian_existing', 'tiantianjiexi_generic'),
                                 ('kukutool_existing', 'kukutool_generic')):
            descriptor = replace(PROVIDER_REGISTRY[lab_id], enabled_setting='', availability=lambda: True)
            confirmations = {name: True for name in descriptor.requires_confirmation}
            with self.subTest(provider=provider), patch.dict(PROVIDER_REGISTRY, {lab_id: descriptor}), \
                    self.mock_client(provider) as client:
                client.return_value = {'images': [MEDIA + '/image.jpg']}
                for missing in descriptor.requires_confirmation:
                    result = run_provider(provider_id=lab_id, source=SOURCE, user_id=7,
                        confirmations={key: value for key, value in confirmations.items() if key != missing})
                    self.assertFalse(result['executed'])
                    client.assert_not_called()
                result = run_provider(provider_id=lab_id, source=SOURCE, user_id=7,
                    confirmations=confirmations)
                self.assertEqual(result['status'], 'success')
                self.assertTrue(result['executed'])
                self.assertEqual(len(result['assets']), 1)
                client.assert_called_once()

    def test_direct_service_runner_does_not_imply_consent(self):
        for lab_id, provider in (('tiantian_existing', 'tiantianjiexi_generic'),
                                 ('kukutool_existing', 'kukutool_generic')):
            with self.subTest(provider=provider), self.mock_client(provider) as client:
                with self.assertRaises(ProviderLabError):
                    PROVIDER_REGISTRY[lab_id].runner(source=SOURCE, request_id='no-consent', user_id=7)
                client.assert_not_called()

    def test_match_is_network_free_exact_and_requires_configuration(self):
        with patch('socket.getaddrinfo', side_effect=AssertionError('network forbidden')):
            for provider in execution.SERVICE_PROVIDER_IDS:
                with self.mock_client(provider) as client:
                    match = execution.service_match(provider, SOURCE)
                    self.assertEqual(match, dict(provider_id=provider, extractor_id=provider,
                        version=execution.SERVICE_VERSIONS[provider], label=provider))
                    self.assertIsNone(execution._python_for(provider))
                    client.assert_not_called()
        with patch.object(execution, 'service_configured', return_value=False):
            self.assertFalse(execution.provider_available('tiantian_existing'))
            with self.assertRaises(ProviderLabError):
                execution.service_match('tiantian_existing', SOURCE)

    def test_service_match_rejects_private_credentials_and_invalid_sources(self):
        for source in ('http://127.0.0.1/post', 'https://user:password@example.org/post',
                       'https://example.org:8443/post', 'file:///tmp/media'):
            with self.subTest(source=source), self.assertRaises(ProviderLabError):
                execution.service_match('tiantian_existing', source)

    def test_authorization_required_before_client_or_runtime(self):
        for provider in execution.SERVICE_PROVIDER_IDS:
            with self.mock_client(provider) as client, patch.object(execution, '_python_for') as runtime:
                for authorization in (False, None, 'true', 1):
                    with self.assertRaises(ProviderLabError):
                        execution.execute_provider(provider, SOURCE, service_authorized=authorization)
                client.assert_not_called()
                runtime.assert_not_called()

    def test_exact_match_options_and_backend_rejected_before_calls(self):
        for provider in execution.SERVICE_PROVIDER_IDS:
            match = execution.service_match(provider, SOURCE)
            with self.mock_client(provider) as client:
                for field in ('provider_id', 'extractor_id', 'version'):
                    with self.assertRaises(ProviderLabError):
                        execution.execute_provider(provider, SOURCE, service_authorized=True,
                            expected_match={**match, field: 'different'})
                for options in ({'backend': 'arbitrary'}, {'options': {}}):
                    with self.assertRaises(ProviderLabError):
                        execution.execute_provider(provider, SOURCE, service_authorized=True, **options)
                client.assert_not_called()

    def test_both_clients_share_candidate_only_lab_and_route_results(self):
        payload = {'title': 'Service fixture', 'videos': [
            {'url': MEDIA, 'format_id': '2160', 'height': 2160, 'acodec': 'none',
             'requires_merge': True, 'audio_tracks': 'absent', 'video_tracks': 'present',
             'quality': '2160p', 'size': 12345},
            {'url': MEDIA + '/stream.m3u8', 'protocol': 'm3u8_native'},
            {'url': MEDIA + '/protected', 'requires_cookie': True},
        ], 'images': [MEDIA + '/image.jpg'], 'audio_url': MEDIA + '/audio.m4a'}
        for provider in execution.SERVICE_PROVIDER_IDS:
            with self.subTest(provider=provider), self.mock_client(provider) as client:
                client.return_value = payload
                match = execution.service_match(provider, SOURCE)
                result = execution.execute_provider(provider, SOURCE, service_authorized=True,
                    expected_match=match, timeout_seconds=12)
                client.assert_called_once_with(SOURCE, timeout_seconds=12)
                self.assertEqual(result.assets, ())
                self.assertEqual(result.metadata['selected_provider'], provider)
                self.assertEqual(result.metadata['title'], 'Service fixture')
                self.assertEqual(result.metadata['retained_count'], 5)
                candidates = result.metadata['upstream_candidates']
                self.assertEqual(candidates[0]['audio_tracks'], 'absent')
                self.assertEqual(candidates[0]['height'], 2160)
                self.assertEqual(candidates[0]['size_bytes'], 12345)
                self.assertEqual(candidates[0]['format_label'], '2160p')
                self.assertTrue(candidates[0]['requires_merge'])
                self.assertEqual(candidates[-1]['media_type'], 'audio')
                from .services.upstream_candidates import resolve_candidate_record
                record = resolve_candidate_record(candidates[0]['resource_token'], candidates[0]['candidate_id'])
                self.assertEqual(record['provider_id'], provider)
                self.assertEqual(record['match'], match)
                self.assertNotIn('provider_manifest', result.metadata)
                lab_id = 'tiantian_existing' if provider == 'tiantianjiexi_generic' else 'kukutool_existing'
                lab, resources = parse_result_to_lab(result, provider_id=lab_id,
                    user_id=7, request_id='service-fixture')
                self.assertEqual(len(lab['assets']), 5)
                self.assertEqual(resources, [])

    def test_nested_kuku_variants_keep_unknown_and_explicit_facts(self):
        with self.mock_client('kukutool_generic') as client:
            client.return_value = {'videos': [{'video_fullinfo': [
                {'url': MEDIA, 'format_id': 'alt', 'upstream_role': 'original', 'vcodec': 'none'}]}]}
            result = execution.execute_provider('kukutool_existing', SOURCE, service_authorized=True)
        self.assertEqual(result.metadata['retained_count'], 1)
        # The enclosing object has no URL; it is a group, not a rejected asset.
        self.assertEqual(result.metadata['rejected_count'], 0)
        self.assertEqual(result.metadata['upstream_candidates'][0]['format_id'], 'alt')
        self.assertEqual(result.assets, ())

    def test_service_nested_materialization_is_bounded_with_exact_partial_count(self):
        with self.mock_client('kukutool_generic') as client:
            client.return_value = {'videos': [{'url': MEDIA, 'video_fullinfo': [
                {'url': f'{MEDIA}/{index}', 'format_id': str(index)} for index in range(1500)]}]}
            result = execution.execute_provider('kukutool_existing', SOURCE, service_authorized=True)
        self.assertEqual(result.metadata['declared_count'], 1501)
        self.assertEqual(result.metadata['examined_count'], 1000)
        self.assertEqual(result.metadata['retained_count'], 1000)
        self.assertEqual(result.metadata['unexpanded_count'], 501)
        self.assertTrue(result.metadata['truncated'])

    def test_service_byte_budget_reports_unexpanded_without_mutating_urls(self):
        urls = [f'{MEDIA}/{index}?signature=' + 'a' * 7000 for index in range(400)]
        with self.mock_client('tiantianjiexi_generic') as client:
            client.return_value = {'images': urls}
            result = execution.execute_provider('tiantian_existing', SOURCE, service_authorized=True)
        metadata = result.metadata
        self.assertGreater(metadata['retained_count'], 0)
        self.assertLess(metadata['retained_count'], 400)
        self.assertEqual(metadata['unexpanded_count'], 400 - metadata['retained_count'])
        self.assertEqual(metadata['examined_count'], 400)
        from .services.upstream_candidates import resolve_candidate_record
        first = metadata['upstream_candidates'][0]
        self.assertEqual(resolve_candidate_record(first['resource_token'], first['candidate_id'])['url'], urls[0])

    def test_existing_runtime_and_account_configuration_gates_are_reused(self):
        self.gate.stop()
        with patch('xhs_download.services.platforms.tiantianjiexi_generic.provider_configured', return_value=False):
            self.assertFalse(execution.provider_available('tiantian_existing'))
        with override_settings(THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True), \
                patch.object(execution, 'runtime_bool', return_value=True), \
                patch('xhs_download.services.platforms.douyin_kukutool.get_kukutool_runtime_status',
                      return_value={'available': False}):
            self.assertFalse(execution.provider_available('kukutool_existing'))

    def test_client_errors_are_redacted_and_timeout_is_bounded(self):
        with self.mock_client('tiantianjiexi_generic') as client:
            client.side_effect = RuntimeError('secret credential https://private.invalid')
            with self.assertRaises(ProviderLabError) as caught:
                execution.execute_provider('tiantian_existing', SOURCE,
                    service_authorized=True, timeout_seconds=900)
            self.assertNotIn('secret', str(caught.exception))
            client.assert_called_once_with(SOURCE, timeout_seconds=30)

    def test_more_than_300_candidates_survive_real_cache_and_lab_mapping(self):
        with self.mock_client('tiantianjiexi_generic') as client:
            client.return_value = {'images': [f'{MEDIA}/{index}.jpg' for index in range(1001)]}
            result = execution.execute_provider('tiantian_existing', SOURCE, service_authorized=True)
        self.assertEqual(result.metadata['retained_count'], 1000)
        self.assertEqual(result.metadata['unexpanded_count'], 1)
        lab, resources = parse_result_to_lab(result, provider_id='tiantian_existing',
            user_id=7, request_id='service-many')
        self.assertEqual(len(lab['assets']), 1000)
        self.assertEqual(resources, [])
        from .services.upstream_candidates import resolve_candidate_record
        tail = result.metadata['upstream_candidates'][-1]
        record = resolve_candidate_record(tail['resource_token'], tail['candidate_id'])
        self.assertEqual(record['url'], f'{MEDIA}/999.jpg')
        self.assertEqual(result.assets, ())
