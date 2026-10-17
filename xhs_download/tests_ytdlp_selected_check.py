"""Selected checks use the real isolated Worker; only final HTTP is a fixture."""
import json
import shlex
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .tests_ytdlp_selection import YtdlpSelectionTests
from .tests_execution_path_parity import runtime_python
from .services import upstream_matching as matching
from .services.platforms.external_tools import run_external_command
from .services.upstream_candidates import resolve_candidate_record
from .services.ytdlp_selection import capability

HTTP_BOOTSTRAP = r'''
import io, runpy, socket, sys, time, urllib.request, urllib.error
target, scenario = sys.argv[1:3]
assert sys.flags.isolated and sys.flags.dont_write_bytecode
assert 'tools.upstream_match_worker' not in sys.modules
assert 'upstream_metadata_probe' not in sys.modules
class UnexpectedNetwork(BaseException): pass
def deny(*a, **k): raise UnexpectedNetwork('offline HTTP fixture')
def audit(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto'}: deny()
sys.addaudithook(audit)
socket.getaddrinfo = deny
socket.socket.connect = deny
def box(kind, body): return (8+len(body)).to_bytes(4,'big')+kind+body
def track(kind): return box(b'trak',box(b'mdia',box(b'hdlr',b'\0'*8+kind+b'\0'*12)))
data = box(b'moov', track(b'vide')+(b'' if scenario=='silent' else track(b'soun')))
if scenario == 'missing': data = box(b'mdat',b'not a movie')
if scenario == 'tail': data = box(b'mdat', b'x' * 262144) + data
calls = 0
class Response(io.BytesIO):
    def __init__(self, request, start, end):
        super().__init__(data[start:end+1])
        self.status = 200 if scenario=='ignored' else 206
        self.headers = {'Content-Range':f'bytes {start}-{end}/{len(data)}'}
        self.url = request.full_url
    def geturl(self): return self.url
def read(opener, request, timeout):
    global calls
    calls += 1
    assert calls <= 2 and 0 < timeout <= 5
    assert request.full_url == 'https://cdn.example.org/720.mp4'
    value = request.get_header('Range').removeprefix('bytes=')
    start, end = map(int,value.split('-'))
    assert end-start+1 <= 131072
    if scenario == 'timeout': time.sleep(10)
    if scenario == 'denied': raise urllib.error.HTTPError(request.full_url,403,'private',{},io.BytesIO())
    return Response(request,start,min(end,len(data)-1))
urllib.request.OpenerDirector.open = read
sys.argv = [target]
runpy.run_path(target,run_name='__main__')
'''


@override_settings(CACHES={'default': {'BACKEND': 'django.core.cache.backends.locmem.LocMemCache'}})
class SelectedTrackCheckTests(SimpleTestCase):
    setUp = YtdlpSelectionTests.setUp
    video = YtdlpSelectionTests.video
    result = YtdlpSelectionTests.result
    candidate = YtdlpSelectionTests.candidate
    gates = YtdlpSelectionTests.gates
    post = YtdlpSelectionTests.post

    def candidates(self, **changes):
        return self.result([self.video('1080',1080),
                            self.video('720',720,acodec=None,vcodec=None,**changes)]).metadata['upstream_candidates']

    def transport(self, scenario, calls, entered=None, release=None):
        def run(command, **kwargs):
            self.assertEqual(command[1:3], ['-I','-B'])
            action = json.loads(kwargs['input_text'])['action']
            calls.append(action)
            if action == 'probe_tracks' and entered is not None:
                entered.set()
                self.assertTrue(release.wait(5))
            return run_external_command([*command[:3], '-c', HTTP_BOOTSTRAP, command[3], scenario], **kwargs)
        return run

    def check(self, candidate, scenario='complete'):
        matching._IDENTITIES.clear()
        calls = []
        with override_settings(YTDLP_BINARY=shlex.quote(runtime_python('yt_dlp_generic'))+' -m yt_dlp'), \
                patch.object(matching, 'run_external_command', side_effect=self.transport(scenario,calls)):
            response = self.post(candidate,'check')
        return response,calls

    def test_unknown_second_format_real_worker_check_then_descriptor_only_updates_that_candidate(self):
        self.gates()
        first,second = self.candidates()
        self.assertTrue(capability(first)['can_download'])
        self.assertFalse(capability(second)['can_download'])
        self.assertTrue(capability(second)['can_check'])
        self.assertEqual(self.post(second).status_code,409)
        before = resolve_candidate_record(first['resource_token'],first['candidate_id'])
        response,calls = self.check(second,'tail')
        self.assertEqual(response.status_code,200,response.content)
        self.assertEqual(calls,['identity','probe_tracks'])
        checked=response.json()['candidate']
        self.assertTrue(checked['can_download'])
        self.assertFalse(checked['can_check'])
        self.assertEqual(checked['selected_track_check']['requests'],2)
        self.assertLessEqual(checked['selected_track_check']['bytes_read'],262144)
        self.assertIsNone(checked['acodec']); self.assertIsNone(checked['vcodec'])
        self.assertEqual(checked['audio_tracks'],'present')
        self.assertEqual(checked['video_tracks'],'present')
        self.assertEqual(resolve_candidate_record(first['resource_token'],first['candidate_id']),before)
        descriptor=self.post(second).json()['item']
        self.assertEqual(descriptor['candidate_id'],second['candidate_id'])
        self.assertEqual(descriptor['selected_format_id'],'720')
        self.assertEqual(descriptor['url'],'https://cdn.example.org/720.mp4')
        repeated,again=self.check(second)
        self.assertEqual(repeated.status_code,200)
        self.assertEqual(again,[])

    def test_bounded_negative_checks_are_retained_not_retried_or_called_complete(self):
        self.gates()
        for scenario,reason in [('silent','complete_track_table'),('missing','moov_not_found'),
                                ('ignored','range_rejected'),('denied','range_rejected'),('timeout','deadline')]:
            with self.subTest(scenario=scenario):
                _,candidate=self.candidates()
                response,calls=self.check(candidate,scenario)
                self.assertEqual(response.status_code,200,response.content)
                result=response.json()['candidate']
                self.assertFalse(result['can_download']); self.assertFalse(result['can_check'])
                self.assertEqual(result['selected_track_check']['reason'],reason)
                self.assertEqual(calls.count('probe_tracks'),1)
                self.assertEqual(self.post(candidate).status_code,409)
                repeated,again=self.check(candidate)
                self.assertEqual(repeated.json()['candidate']['selected_track_check']['reason'],reason)
                self.assertEqual(again,[])

    def test_complete_and_restricted_candidates_never_start_probe(self):
        self.gates()
        for changes,status in [({},200),({'protocol':'m3u8_native'},409),({'has_drm':True},409),
                               ({'acodec':'none'},409),({'http_headers':{'Referer':'https://site.example.org/'}},409),
                               ({'ext':'webm','acodec':None,'vcodec':None},409)]:
            with self.subTest(changes=changes):
                candidate=self.candidate(**changes)
                response,calls=self.check(candidate)
                self.assertEqual(response.status_code,status,response.content)
                self.assertEqual(calls,[])

    def test_expired_or_denied_or_tampered_check_cannot_run(self):
        self.gates()
        _,candidate=self.candidates()
        with patch.object(matching,'run_external_command') as run:
            self.assertEqual(self.post(candidate,'check',url='https://evil.example/file').status_code,400)
            self.assertEqual(self.post(candidate,'check',candidate_id='other').status_code,403)
            with patch('xhs_download.ytdlp_selection_views._admitted',return_value=False):
                self.assertEqual(self.post(candidate,'check').status_code,403)
            cache.clear()
            self.assertEqual(self.post(candidate,'check').status_code,403)
        run.assert_not_called()

    def test_original_expiry_and_download_policy_still_bound_after_check(self):
        self.gates()
        _,candidate=self.candidates()
        record=resolve_candidate_record(candidate['resource_token'],candidate['candidate_id'])
        response,_=self.check(candidate)
        self.assertEqual(response.status_code,200)
        with patch('xhs_download.views.check_download_cost_preflight',
                   return_value=('VISITOR_VIDEO_DOWNLOAD_DISABLED','disabled',403)):
            self.assertEqual(self.post(candidate,'check').status_code,403)
            self.assertEqual(self.post(candidate).status_code,403)
        with patch('time.time',return_value=record['expires_at']+1), \
                patch.object(matching,'run_external_command') as run:
            self.assertEqual(self.post(candidate,'check').status_code,403)
            self.assertEqual(self.post(candidate).status_code,403)
        run.assert_not_called()

    def test_runtime_change_does_not_read_media_or_enable_save(self):
        self.gates()
        _,candidate=self.candidates()
        with patch.object(matching,'_context',return_value=(['unused'],'2027.1.1','changed')), \
                patch.object(matching,'_call') as call:
            response=self.post(candidate,'check')
        call.assert_not_called()
        self.assertEqual(response.status_code,200)
        self.assertEqual(response.json()['check']['reason'],'runtime_changed')
        self.assertFalse(response.json()['candidate']['can_download'])
        self.assertEqual(self.post(candidate).status_code,409)

    def test_concurrent_check_claim_is_single_flight_and_does_not_change_first_format(self):
        self.gates()
        _,candidate=self.candidates()
        entered,release=threading.Event(),threading.Event()
        calls=[]; matching._IDENTITIES.clear()
        with override_settings(YTDLP_BINARY=shlex.quote(runtime_python('yt_dlp_generic'))+' -m yt_dlp'), \
                patch.object(matching,'run_external_command',side_effect=self.transport('complete',calls,entered,release)), \
                ThreadPoolExecutor(max_workers=1) as pool:
            future=pool.submit(self.post,candidate,'check')
            try:
                self.assertTrue(entered.wait(5))
                self.assertEqual(self.post(candidate,'check').status_code,409)
            finally:
                release.set()
            self.assertEqual(future.result().status_code,200)
        self.assertEqual(calls.count('probe_tracks'),1)
