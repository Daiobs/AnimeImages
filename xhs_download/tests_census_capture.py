import json
from unittest.mock import patch

from django.test import SimpleTestCase
from tools.census_capture import Capture
from tools import upstream_match_worker as worker
from xhs_download.management.commands.capture_upstream import Command
from django.core.management.base import CommandError


class CensusCaptureTests(SimpleTestCase):
    def test_nested_unknown_formats_and_secrets(self):
        capture = Capture(secrets=('synthetic-private-key',))
        capture({'formats': [{'url': f'https://example.org/{i}.mp4?sig=expiry',
                              'unknown': {'relation': [None, False]}} for i in range(120)],
                 'headers': {'Authorization': 'synthetic-private-key'},
                 'message': 'echo synthetic-private-key', 'password': 'hidden'})
        body = json.dumps(capture.summary())
        self.assertNotIn('synthetic-private-key', body)
        self.assertNotIn('hidden', body)
        self.assertEqual(len(capture.records[0]['formats']), 120)
        self.assertIn('sig=expiry', body)
        self.assertEqual(capture.records[0]['formats'][-1]['unknown']['relation'], [None, False])

    def test_no_property_evaluation(self):
        class Native:
            def __init__(self): self.result = {'id': 'fixture'}
            @property
            def dangerous(self): raise AssertionError('must not evaluate')
        capture = Capture()
        capture(Native())
        self.assertEqual(capture.records[0]['fields']['result']['id'], 'fixture')

    def test_bound_is_not_unsupported(self):
        capture = Capture(limit=40000)
        capture({'body': 'x' * 9000})
        self.assertTrue(capture.summary()['truncated'])
        self.assertIn('record_byte_limit', capture.summary()['not_exported_types'])

    def test_ytdlp_observation_precedes_mapping_failure(self):
        from yt_dlp.extractor.common import InfoExtractor
        class FixtureIE(InfoExtractor):
            _VALID_URL = r'https://example.org/(?P<id>1)'
            def _real_extract(self, url):
                return {'id': '1', 'title': 'fixture', 'unknown': {'value': 'preserved'},
                        'formats': [{'url': 'https://example.org/video.mp4', 'height': 2160}]}
        capture = Capture()
        with patch.object(worker, 'compact_descriptor', side_effect=ValueError('mapping_failed')):
            with self.assertRaises(ValueError):
                worker.parse_ytdlp(FixtureIE, 'https://example.org/1', 1, observe=capture)
        self.assertEqual(capture.records[0]['unknown']['value'], 'preserved')
        self.assertEqual(capture.records[0]['formats'][0]['height'], 2160)
        self.assertEqual(capture.attempts, 1)

    def test_command_requires_explicit_local_authorization(self):
        with self.assertRaises(CommandError):
            Command().handle(authorization='wrong', describe=True)

    def test_internal_census_does_not_consult_public_route_filter(self):
        from tools import upstream_census_worker as census
        capture = Capture()
        class Fixture:
            @staticmethod
            def ie_key(): return 'Fixture'
        def result(*args, **kwargs): kwargs['observe']({'id': '1', 'unknown': True})
        from contextlib import nullcontext
        with patch.object(census.boundary, 'package_identity', return_value={'version':'v'}), \
             patch.object(census.boundary, 'extractor_classes', return_value=[Fixture]), \
             patch.object(census.boundary, 'suitable', return_value=True), \
             patch.object(census.boundary, 'eligible', side_effect=AssertionError('public route filter')), \
             patch.object(census.boundary, 'entry', return_value={'extractor_id':'Fixture'}), \
             patch.object(census.boundary, 'install_network_guard'), \
             patch.object(census.boundary, 'runtime_budget', return_value=nullcontext({'requests':1,'bytes':0})), \
             patch.object(census.boundary, 'parse_ytdlp', side_effect=result):
            response = census.collect({'provider':'yt_dlp_generic','url':'https://example.org/1','version':'v'}, capture)
        self.assertEqual(response['status'], 'RETURNED')
        self.assertTrue(response['executed'])
