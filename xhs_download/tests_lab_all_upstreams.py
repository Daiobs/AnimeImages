from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, RequestFactory

from .services.provider_lab import providers
from .services.provider_lab.contracts import ProviderLabError


class AllUpstreamDescriptorTests(SimpleTestCase):
    def descriptor(self, provider_id, runner=None, available=True):
        return replace(providers.PROVIDER_REGISTRY[provider_id], enabled_setting='',
                       availability=lambda:available, runner=runner or Mock(return_value=({'status':'success'}, [])))

    def test_all_integrated_channels_visible_including_unavailable(self):
        with patch.object(providers,'_shared_available',return_value=False), \
                patch.object(providers,'_external_available',return_value=False):
            rows = {item['provider_id']:item for item in providers.list_provider_descriptors()}
        self.assertIn('kukutool_existing',rows)
        self.assertIn('tiantian_existing',rows)
        self.assertEqual(rows['kukutool_existing']['execution_provider_id'],'kukutool_generic')
        self.assertEqual(rows['tiantian_existing']['execution_provider_id'],'tiantianjiexi_generic')
        self.assertFalse(rows['kukutool_existing']['available'])
        self.assertTrue(rows['kukutool_existing']['unexecuted_reason'])

    def test_baseline_is_not_independent_or_route_selectable(self):
        row = providers.PROVIDER_REGISTRY['animeimages_current'].as_dict()
        self.assertFalse(row['independent_upstream'])
        self.assertFalse(row['route_selectable'])
        self.assertFalse(row['default_selected'])
        self.assertEqual(row['route_unavailable_reason'],'COMPOSITE_BASELINE')

    def test_capability_follows_shared_aliases_not_runtime_configuration(self):
        descriptor = self.descriptor('kukutool_existing',available=False)
        with patch('xhs_download.services.quality_execution.PROVIDER_ALIASES',{'kukutool_existing':'kukutool_generic'}):
            row = descriptor.as_dict()
        self.assertTrue(row['route_selectable'])
        self.assertFalse(row['available'])

    def test_service_consent_is_per_request_and_server_enforced(self):
        runner = Mock(return_value=({'status':'success'}, []))
        descriptor = self.descriptor('tiantian_existing',runner)
        with patch.dict(providers.PROVIDER_REGISTRY,{'tiantian_existing':descriptor}):
            for consent in (None, {'paid_service':True}, {'external_sharing':False}):
                result = providers.run_provider(provider_id='tiantian_existing',source='https://example.org/post',user_id=1,confirmations=consent)
                self.assertFalse(result['executed'])
                self.assertEqual(result['unexecuted_reason'],'CONFIRMATION_REQUIRED')
            runner.assert_not_called()
            result = providers.run_provider(provider_id='tiantian_existing',source='https://example.org/post',user_id=1,
                confirmations={'external_sharing':True})
            self.assertTrue(result['executed'])
            runner.assert_called_once()
            result = providers.run_provider(provider_id='tiantian_existing',source='https://example.org/post',user_id=1)
            self.assertFalse(result['executed'])
            runner.assert_called_once()

    def test_unavailable_with_consent_does_not_call_runner(self):
        runner = Mock()
        descriptor = self.descriptor('kukutool_existing',runner,available=False)
        with patch.dict(providers.PROVIDER_REGISTRY,{'kukutool_existing':descriptor}):
            result = providers.run_provider(provider_id='kukutool_existing',source='https://example.org/post',user_id=1,
                confirmations={'external_sharing':True})
        self.assertFalse(result['executed'])
        self.assertEqual(result['unexecuted_reason'],'PROVIDER_NOT_CONFIGURED')
        runner.assert_not_called()

    def test_consent_rejects_coercion_and_unrecognized_fields(self):
        for consent in ({'paid_service':1}, {'external_sharing':'true'}, {'all':True}, True):
            with self.assertRaises(ProviderLabError):
                providers.validate_confirmations(consent)

    def test_unsupported_provider_does_not_cancel_another_independent_call(self):
        first = Mock(side_effect=ProviderLabError('PROVIDER_UNSUPPORTED'))
        second = Mock(return_value=({'status':'success'}, []))
        with patch.dict(providers.PROVIDER_REGISTRY,{
                'yt_dlp_existing':self.descriptor('yt_dlp_existing',first),
                'gallery_dl_existing':self.descriptor('gallery_dl_existing',second)}):
            failed = providers.run_provider(provider_id='yt_dlp_existing',source='https://example.org/post',user_id=1)
            success = providers.run_provider(provider_id='gallery_dl_existing',source='https://example.org/post',user_id=1)
        self.assertEqual(failed['status'],'unsupported')
        self.assertTrue(failed['executed'])
        self.assertEqual(success['status'],'success')
        second.assert_called_once()

    def test_run_endpoint_enforces_confirmation_and_accepts_explicit_grant(self):
        from .provider_lab_views import provider_lab_run_view
        runner = Mock(return_value=({'status':'success'}, []))
        descriptor = self.descriptor('kukutool_existing',runner)
        with patch.dict(providers.PROVIDER_REGISTRY,{'kukutool_existing':descriptor}), \
                patch('xhs_download.provider_lab_views.require_provider_lab_access'), \
                patch('xhs_download.provider_lab_views.consume_fixed_window',return_value=SimpleNamespace(allowed=True)), \
                patch('xhs_download.provider_lab_views.reserve_single_flight',return_value=True):
            for consent, expected_calls in ((None,0),({'external_sharing':True},1)):
                body = {'source':'https://example.org/post','provider_id':'kukutool_existing'}
                if consent is not None:
                    body['confirmations'] = consent
                request = RequestFactory().post('/admin/provider-lab/run/',json.dumps(body),content_type='application/json')
                request.user = SimpleNamespace(pk=1)
                request._dont_enforce_csrf_checks = True
                response = provider_lab_run_view(request)
                self.assertEqual(response.status_code,200)
                result = json.loads(response.content)
                self.assertEqual(result['executed'],bool(expected_calls))
                self.assertEqual(runner.call_count,expected_calls)
