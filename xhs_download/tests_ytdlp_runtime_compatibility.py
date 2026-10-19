"""Real installed runtimes; only native extraction/HTTP fixtures are substituted."""
import json
import os
import shlex
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from . import tests_execution_path_parity as parity
from .services import quality_execution as quality
from .services import upstream_matching as matching
from .services.common import XhsParseError
from .services.provider_lab.contracts import ProviderLabError
from .services.upstream_routes import route_cache_identity


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
                                     'LOCATION': 'ytdlp-compatibility'}},
                   YTDLP_PROVIDER_ENABLED=True)
class YtdlpRuntimeCompatibilityTests(SimpleTestCase):
    transport = parity.WorkerEntryParityTests.transport

    def setUp(self):
        matching._IDENTITIES.clear()
        matching._CACHE.clear()
        self.python = parity.runtime_python('yt_dlp_generic')
        self.identity = parity.runtime_identity(self.python)
        self.assertTrue(quality.runtime_version_supported('yt_dlp_generic', self.identity['distribution']))

    def test_actual_package_metadata_and_module_are_the_selected_official_release(self):
        self.assertEqual(tuple(map(int, self.identity['distribution'].split('.'))),
                         tuple(map(int, self.identity['module'].split('.'))))
        print('ytdlp_runtime_under_test=' + json.dumps(self.identity, sort_keys=True))
        with override_settings(YTDLP_BINARY=shlex.quote(self.python) + ' -m yt_dlp'):
            self.assertTrue(quality.provider_available('yt_dlp_generic'))
            _, version, _ = matching._context('yt_dlp_generic')
        self.assertEqual(version, self.identity['distribution'])

    def test_compatibility_is_an_explicit_set_not_a_minimum_version(self):
        for version in ('2026.3.17', '2026.03.17', '2026.6.9', '2026.06.09'):
            self.assertTrue(quality.runtime_version_supported('yt_dlp_generic', version))
        for version in ('2026.6.10', '2027.1.1', '2026.6.9.dev1', '2026.6.9+patched', '', None):
            self.assertFalse(quality.runtime_version_supported('yt_dlp_generic', version))
        with patch.object(matching, '_context', return_value=(['unused'], '2027.1.1', 'x')), \
                patch.object(matching, '_call') as call:
            with self.assertRaises(ProviderLabError) as error:
                quality._execute_upstream('yt_dlp_generic', parity.SOURCE, 5)
        self.assertEqual((error.exception.code, error.exception.detail),
                         ('PROVIDER_POLICY_REJECTED', 'UPSTREAM_VERSION_UNSUPPORTED'))
        call.assert_not_called()

    def shared(self, scenario):
        calls = []
        with override_settings(YTDLP_BINARY=shlex.quote(self.python) + ' -m yt_dlp'), \
                patch.object(matching, 'run_external_command',
                             side_effect=self.transport('yt_dlp_generic', scenario, calls)), \
                patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]), \
                patch('socket.socket.connect', side_effect=AssertionError('parent network forbidden')):
            result = quality.execute_provider('yt_dlp_generic', parity.SOURCE)
        self.assertEqual(calls, ['identity', 'match', 'parse'])
        self.assertEqual(result.metadata['upstream_version'], self.identity['distribution'])
        return result

    def test_real_worker_to_shared_candidates_preserves_types_order_language_and_unknowns(self):
        for scenario, count in [('audio', 1), ('video', 1), ('streams', 3), ('multilingual', 4),
                                ('probe', 1), ('probe_rejected', 1)]:
            with self.subTest(scenario=scenario):
                matching._IDENTITIES.clear()
                result = self.shared(scenario)
                rows = result.metadata['upstream_candidates']
                self.assertEqual(len(rows), count)
                self.assertEqual([r['ordinal'] for r in rows], list(range(1, count + 1)))
                if scenario == 'audio':
                    self.assertEqual(result.post_type, 'audio')
                    self.assertEqual((rows[0]['media_type'], rows[0]['language']), ('audio', 'en'))
                if scenario == 'video':
                    self.assertEqual((rows[0]['height'], rows[0]['video_tracks'], rows[0]['audio_tracks']),
                                     (1080, 'present', 'present'))
                if scenario == 'streams':
                    self.assertEqual([r['protocol'] for r in rows], ['m3u8_native', 'http_dash_segments', 'https'])
                    self.assertEqual(rows[1]['audio_tracks'], 'absent')
                    self.assertIsNone(rows[1]['requires_merge'])
                if scenario == 'multilingual':
                    self.assertEqual([r['language'] for r in rows], ['en', 'zh', None, None])
                    self.assertEqual([r['media_type'] for r in rows[:3]], ['audio', 'audio', 'video'])
                    self.assertEqual((rows[3]['audio_tracks'], rows[3]['video_tracks']), ('unknown', 'unknown'))
                    self.assertIsNone(rows[2]['requires_merge'])
                if scenario == 'probe_rejected':
                    self.assertEqual((rows[0]['audio_tracks'], rows[0]['video_tracks']), ('unknown', 'unknown'))
                if scenario == 'probe':
                    self.assertEqual((rows[0]['audio_tracks'], rows[0]['video_tracks']), ('present', 'present'))
                    self.assertIsNone(rows[0]['acodec'])
                    self.assertIsNone(rows[0]['vcodec'])

    def test_real_default_headers_survive_processing_and_allow_selected_descriptor(self):
        from .services.upstream_candidates import resolve_candidate_record
        from .services.ytdlp_selection import capability, selected_descriptor
        for scenario in ('audio', 'video'):
            with self.subTest(scenario=scenario):
                matching._IDENTITIES.clear()
                candidate = self.shared(scenario).metadata['upstream_candidates'][0]
                with patch('socket.getaddrinfo', return_value=[(2, 1, 6, '', ('8.8.8.8', 443))]):
                    record = resolve_candidate_record(candidate['resource_token'], candidate['candidate_id'])
                    self.assertIn('sec-fetch-mode', {key.lower() for key in record['headers']})
                    self.assertTrue(capability(candidate)['can_download'])
                    descriptor = selected_descriptor(record)
                self.assertEqual(descriptor['candidate_id'], candidate['candidate_id'])
                self.assertEqual(descriptor['type'], candidate['media_type'])

    def test_context_policy_revision_invalidates_normalized_parse_cache(self):
        from .services.upstream_routes import parse_cache_key
        from .services import ytdlp_selection
        route = {'cache_identity': 'fixture-route'}
        with patch.object(quality, '_python_for', return_value=self.python), \
                patch.object(matching, '_executor', return_value=([], 'fixed-runtime')):
            before = parse_cache_key(parity.SOURCE, route, 'yt_dlp_generic')
            with patch.object(ytdlp_selection, 'CONTEXT_POLICY_REVISION', 999):
                after = parse_cache_key(parity.SOURCE, route, 'yt_dlp_generic')
        self.assertNotEqual(before, after)

    def test_native_rejections_and_late_import_are_not_configuration_absence(self):
        cases = [('auth', 'PROVIDER_PROCESS_FAILED', 'UPSTREAM_AUTH_REQUIRED'),
                 ('unsupported', 'PROVIDER_PROCESS_FAILED', 'UPSTREAM_UPSTREAM_UNAVAILABLE'),
                 ('no_formats', 'PROVIDER_RESULT_EMPTY', 'UPSTREAM_NO_FORMATS'),
                 ('local_import', 'PROVIDER_EXECUTOR_FAILED', 'UPSTREAM_RUNTIME_IMPORT_ERROR')]
        for scenario, code, detail in cases:
            matching._IDENTITIES.clear()
            with self.subTest(scenario=scenario), self.assertRaises(ProviderLabError) as caught:
                self.shared(scenario)
            self.assertEqual((caught.exception.code, caught.exception.detail), (code, detail))
            self.assertNotIn('private-token', str(caught.exception))
        matching._IDENTITIES.clear()
        with self.assertRaises(XhsParseError):
            self.shared('empty')

    def test_actual_identity_and_match_caches_do_not_cross_runtime_versions(self):
        alternative = os.environ.get('YTDLP_COMPAT_ALTERNATE_PYTHON')
        runtimes = [self.python, alternative, self.python] if alternative else [self.python, self.python]
        calls, contexts, matches = [], [], []
        for python in runtimes:
            with override_settings(YTDLP_BINARY=shlex.quote(python) + ' -m yt_dlp'), \
                    patch.object(matching, 'run_external_command',
                                 side_effect=self.transport('yt_dlp_generic', 'audio', calls)):
                contexts.append(matching._context('yt_dlp_generic'))
                matches.append(matching._cached('yt_dlp_generic', 'match', parity.SOURCE))
        self.assertEqual(contexts[0][2], contexts[-1][2])
        self.assertEqual(matches[0], matches[-1])
        self.assertEqual(calls.count('match'), 2 if alternative else 1)
        if alternative:
            self.assertNotEqual(contexts[0][1], contexts[1][1])
            self.assertNotEqual(contexts[0][2], contexts[1][2])
            route = dict(source_host='example.org', revision=1, effective_provider_order=['yt_dlp_generic'],
                         provider_options={}, format_preferences={})
            self.assertNotEqual(route_cache_identity(route, matches[0]), route_cache_identity(route, matches[1]))

    def test_real_child_rejects_stale_request_version_and_parent_rejects_stale_match(self):
        with override_settings(YTDLP_BINARY=shlex.quote(self.python) + ' -m yt_dlp'):
            command, version, _ = matching._context('yt_dlp_generic')
            stale = '2026.3.17' if version != '2026.3.17' else '2026.6.9'
            with self.assertRaises(matching.UpstreamMatchingError) as error:
                matching._call(command, {'action': 'match', 'provider_id': 'yt_dlp_generic',
                                        'version': stale, 'url': parity.SOURCE})
            self.assertEqual(error.exception.code, 'version_changed')

            with self.assertRaises(matching.UpstreamMatchingError) as error:
                matching.parse_upstream_url({'provider_id': 'yt_dlp_generic', 'version': stale,
                                             'extractor_id': 'Fixture'}, parity.SOURCE)
            self.assertEqual(error.exception.code, 'version_changed')

    def test_malformed_worker_envelopes_are_protocol_errors_without_raw_output(self):
        for raw in ('private-token', '[]', '{"ok":true}', '{"ok":false,"error":"private-token"}',
                    '{"ok":false,"error":[]}', '{"ok":false,"error":{}}'):
            with self.subTest(raw=raw), patch.object(matching, 'run_external_command',
                    return_value=SimpleNamespace(stdout=raw, returncode=0)), \
                    self.assertRaises(matching.UpstreamMatchingError) as caught:
                matching._call(['unused'], {})
            self.assertEqual(caught.exception.code, 'invalid_protocol')
            self.assertNotIn('private-token', str(caught.exception))

    def test_real_runtime_switch_between_match_and_parse_is_rejected(self):
        alternate = os.environ.get('YTDLP_COMPAT_ALTERNATE_PYTHON')
        if not alternate:
            # Still exercise the real child's stale-version rejection above;
            # the Python 3.12 CI job supplies both actual installed runtimes.
            return
        calls = []
        transport = self.transport('yt_dlp_generic', 'audio', calls)
        def run(command, **kwargs):
            if json.loads(kwargs['input_text'])['action'] == 'parse':
                command = [alternate, *command[1:]]
            return transport(command, **kwargs)
        with override_settings(YTDLP_BINARY=shlex.quote(self.python) + ' -m yt_dlp'), \
                patch.object(matching, 'run_external_command', side_effect=run), \
                self.assertRaises(ProviderLabError) as caught:
            quality._execute_upstream('yt_dlp_generic', parity.SOURCE, 10)
        self.assertEqual(calls, ['identity', 'match', 'parse'])
        self.assertEqual((caught.exception.code, caught.exception.detail),
                         ('PROVIDER_SCHEMA_INVALID', 'UPSTREAM_VERSION_CHANGED'))
