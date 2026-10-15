"""Offline process contracts; native extraction/HTTP only are replaced.

The runpy bootstrap is test-only: unlike production it installs synthetic native
extractors before running the real file as __main__. It never imports the Worker
or probe first, inserts project paths, or replaces handle/probe/serialization.
"""
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
from contextlib import ExitStack
from unittest.mock import patch

from django.test import SimpleTestCase, override_settings

from .services import upstream_matching as matching
from .services import upstream_parsehub as parsehub
from .services.quality_execution import _execute_upstream
from .services.quality_execution import execute_provider
from .services.provider_lab.contracts import ProviderLabError
from .services.common import XhsParseError
from .services.platforms.external_tools import run_external_command

ROOT = Path(__file__).resolve().parents[1]
SOURCE = 'https://example.org/work/audio'

# DNS/connect denial is independent of the production guard and remains active
# throughout import, extraction and optional probing. No archived URL is used.
BOOTSTRAP = r'''
import io, json, os, runpy, socket, sys, time
from pathlib import Path
from unittest.mock import patch
target, provider, scenario = sys.argv[1:4]
fixtures = Path(sys.argv[4])
class UnexpectedNetwork(BaseException): pass
def deny(*args, **kwargs): raise UnexpectedNetwork('offline transport')
def audit(event, args):
    if event in {'socket.connect', 'socket.getaddrinfo', 'socket.sendto'}: deny()
sys.addaudithook(audit)
socket.getaddrinfo = deny
socket.socket.connect = deny
assert sys.flags.isolated and sys.flags.dont_write_bytecode
assert 'tools.upstream_match_worker' not in sys.modules
assert 'upstream_match_worker' not in sys.modules
assert 'upstream_metadata_probe' not in sys.modules
if provider in ('yt_dlp_generic', 'gallery_dl_generic'):
    if provider == 'yt_dlp_generic':
        import yt_dlp.extractor
        from yt_dlp.extractor.common import InfoExtractor
        class FixtureIE(InfoExtractor):
            _VALID_URL = r'https://example.org/work/(?P<id>[^/]+)'
            def _real_extract(self, url):
                if scenario == 'recorded_audio':
                    return json.loads((fixtures/'88e9b92a7c517fb33ce435a4.json').read_text())['records'][0]
                if scenario == 'timeout': time.sleep(2)
                if scenario == 'local_import': raise ModuleNotFoundError('private-token')
                if scenario == 'empty': return {'id':'audio','title':'fixture','formats':[]}
                item = {'url':'https://media.example/audio.mp3','ext':'mp3',
                        'vcodec':'none','acodec':'mp3','language':'en'}
                if scenario in ('video', 'probe', 'probe_rejected'):
                    item = {'url':'https://media.example/video.mp4','ext':'mp4','height':1080}
                    if scenario == 'video': item.update(vcodec='avc1', acodec='aac')
                formats = [item]
                if scenario == 'streams':
                    formats = [
                        {'url':'https://media.example/a.m3u8','ext':'mp4','protocol':'m3u8_native'},
                        {'url':'https://media.example/a.mpd','ext':'mp4','protocol':'http_dash_segments',
                         'vcodec':'avc1','acodec':'none'}, item]
                return {'id':'audio','title':'fixture','formats':formats,
                        'thumbnails':[{'url':['invalid optional decoration']}]}
        yt_dlp.extractor.gen_extractor_classes = lambda: [FixtureIE]
        if scenario.startswith('probe'):
            import urllib.request, urllib.error
            def box(kind, body): return (8+len(body)).to_bytes(4,'big')+kind+body
            def track(kind): return box(b'trak',box(b'mdia',box(b'hdlr',b'\0'*8+kind+b'\0'*12)))
            data = box(b'moov', track(b'vide')+track(b'soun'))
            class Response(io.BytesIO):
                status = 206
                headers = {'Content-Range':f'bytes 0-{len(data)-1}/{len(data)}'}
                def geturl(self): return 'https://media.example/video.mp4'
            def read(opener, request, timeout):
                assert request.get_header('Range') == 'bytes=0-131071'
                assert timeout > 0
                if scenario == 'probe_rejected':
                    raise urllib.error.HTTPError(request.full_url,403,'private',{},io.BytesIO())
                return Response(data)
            urllib.request.OpenerDirector.open = read
    else:
        import gallery_dl.extractor
        class FixtureGallery:
            pattern = r'https://example.org/work/(?P<id>[^/]+)'
            category, subcategory = 'fixture', 'post'
            def __init__(self, match): pass
            def __iter__(self):
                if scenario == 'recorded_gallery':
                    yield from json.loads((fixtures/'5d6c88d0a3cb75de9a7b28d4.json').read_text())['records']
                    return
                yield (2, {'title':'fixture'})
                yield (3, 'https://media.example/1.jpg', {'extension':'jpg','width':1200})
                yield (3, 'https://media.example/2.jpg', {'extension':'jpg','width':800})
        gallery_dl.extractor.extractors = lambda: [FixtureGallery]
elif provider == 'parsehub_generic':
    os.environ['PYCRYPTODOME_DISABLE_GMP'] = '1'
    from parsehub.parsers.parser.weixin import WXParser
    from parsehub.types import RichTextParseResult
    async def extract(self, url):
        if scenario == 'local_import': raise ModuleNotFoundError('private-token')
        if scenario == 'recorded_body':
            data=json.loads((fixtures/'b57196ed22696db0ca991f21.json').read_text())
            return RichTextParseResult(markdown_content=data['records'][0]['fields']['markdown_content'])
        return RichTextParseResult(markdown_content='body ![](https://media.example/1.jpg) ![](https://media.example/2.jpg)')
    WXParser._do_parse = extract
sys.argv = [target]
runpy.run_path(target, run_name='__main__')
'''


class WorkerEntryParityTests(SimpleTestCase):
    def setUp(self):
        matching._IDENTITIES.clear()
        matching._CACHE.clear()

    def transport(self, provider, scenario, calls):
        def run(command, **kwargs):
            self.assertEqual(command[1:3], ['-I', '-B'])
            calls.append(json.loads(kwargs['input_text'])['action'])
            return run_external_command(
                [*command[:3], '-c', BOOTSTRAP, command[3], provider, scenario,
                 str(ROOT/'xhs_download/fixtures/native_replay')], **kwargs)
        return run

    def execute(self, provider, scenario, *, timeout=10):
        calls = []
        setting = 'YTDLP_BINARY' if provider == 'yt_dlp_generic' else 'GALLERYDL_BINARY'
        module = 'yt_dlp' if provider == 'yt_dlp_generic' else 'gallery_dl'
        with override_settings(**{setting: shlex.quote(sys.executable) + ' -m ' + module}), \
                patch.object(matching, 'run_external_command', side_effect=self.transport(provider, scenario, calls)):
            result = _execute_upstream(provider, SOURCE, timeout)
        self.assertEqual(calls, ['identity', 'match', 'parse'])
        return result[0]

    def test_audio_complete_video_and_streams_keep_real_main_and_postprocessing(self):
        for scenario in ('audio', 'video', 'streams'):
            with self.subTest(scenario=scenario):
                self.setUp()
                result = self.execute('yt_dlp_generic', scenario)
                self.assertEqual(result['id'], 'audio')
                self.assertEqual(len(result['formats']), 3 if scenario == 'streams' else 1)
                self.assertTrue(all('upstream_bmff_track_evidence' not in row for row in result['formats']))
                if scenario == 'audio':
                    self.assertEqual(result['formats'][0]['language'], 'en')
                    self.assertEqual(result['formats'][0]['upstream_track_evidence']['vcodec'], 'none')

    def test_optional_probe_uses_real_guarded_reader_and_preserves_unknown_on_rejection(self):
        for scenario, complete in [('probe', True), ('probe_rejected', False)]:
            with self.subTest(scenario=scenario):
                self.setUp()
                result = self.execute('yt_dlp_generic', scenario)
                row = result['formats'][0]
                evidence = row['upstream_bmff_track_evidence']
                self.assertEqual(evidence['complete_moov'], complete)
                self.assertEqual(evidence['audio'], 'present' if complete else 'unknown')
                self.assertIsNone(row['upstream_track_evidence']['acodec'])

    def test_gallery_message_order_survives_actual_entry(self):
        rows = self.execute('gallery_dl_generic', 'images')
        self.assertEqual([row[0] for row in rows], [2, 3, 3])
        self.assertEqual([row[2]['width'] for row in rows[1:]], [1200, 800])

    def test_recorded_audio_and_gallery_inputs_through_main(self):
        audio = self.execute('yt_dlp_generic', 'recorded_audio')
        self.assertEqual(audio['formats'][0]['ext'], 'mp3')
        self.assertEqual(audio['duration'], 30)
        rows = self.execute('gallery_dl_generic', 'recorded_gallery')
        media = [row for row in rows if row[0] == 3]
        self.assertTrue(any(row[2].get('width') == 1920 and row[2].get('duration') == 105 for row in media))

    def test_configured_interpreter_and_worker_paths_with_spaces(self):
        with tempfile.TemporaryDirectory(prefix='parity paths ') as directory:
            root = Path(directory)
            runtime = root / 'runtime alias'
            runtime.symlink_to(Path(sys.executable).parent.parent, target_is_directory=True)
            python = runtime / 'bin' / Path(sys.executable).name
            worker = root / 'upstream_match_worker.py'
            shutil.copyfile(matching.WORKER, worker)
            shutil.copyfile(matching.WORKER.with_name('upstream_metadata_probe.py'),
                            root / 'upstream_metadata_probe.py')
            calls = []
            with override_settings(YTDLP_BINARY=shlex.quote(str(python)) + ' -m yt_dlp'), \
                    patch.object(matching, 'WORKER', worker), \
                    patch.object(matching, 'run_external_command', side_effect=self.transport('yt_dlp_generic', 'probe', calls)):
                result, _, _ = _execute_upstream('yt_dlp_generic', SOURCE, 10)
            self.assertTrue(result['formats'][0]['upstream_bmff_track_evidence']['complete_moov'])

    def test_parent_deadline_and_local_import_have_distinct_safe_errors(self):
        for scenario, expected in [('timeout', 'PROVIDER_TIMEOUT'), ('local_import', 'PROVIDER_EXECUTOR_FAILED')]:
            with self.subTest(scenario=scenario):
                self.setUp()
                with self.assertRaises(ProviderLabError) as caught:
                    self.execute('yt_dlp_generic', scenario, timeout=1.5)
                self.assertEqual(caught.exception.code, expected)
                self.assertNotIn('private-token', str(caught.exception))
                if scenario == 'local_import':
                    self.assertEqual(caught.exception.detail, 'UPSTREAM_RUNTIME_IMPORT_ERROR')

    def test_empty_extraction_is_not_an_executor_configuration_failure(self):
        with override_settings(YTDLP_PROVIDER_ENABLED=True,
                YTDLP_BINARY=shlex.quote(sys.executable)+' -m yt_dlp'), \
                patch.object(matching,'run_external_command',
                             side_effect=self.transport('yt_dlp_generic','empty',[])), \
                patch('socket.getaddrinfo',return_value=[(2,1,6,'',('8.8.8.8',443))]), \
                patch('socket.socket.connect',side_effect=AssertionError('network forbidden')):
            with self.assertRaises(XhsParseError) as caught:
                execute_provider('yt_dlp_existing',SOURCE)
        self.assertIn('没有返回有效', str(caught.exception))

    def test_parsehub_pinned_runtime_enters_main_without_project_path_injection(self):
        python = os.environ.get('PARSEHUB_COMPAT_PYTHON', '')
        self.assertTrue(python and Path(python).is_file())
        calls = []
        with override_settings(PARSEHUB_UPSTREAM_ENABLED=True, PARSEHUB_UPSTREAM_PYTHON=python), \
                patch.object(parsehub, 'run_external_command',
                    side_effect=self.transport('parsehub_generic', 'body', calls)):
            rows, match, _ = _execute_upstream('parsehub_generic', 'https://mp.weixin.qq.com/s/offline-parity', 15)
        self.assertEqual(match['extractor_id'], 'WXParser')
        self.assertEqual([row[1] for row in rows], ['https://media.example/1.jpg', 'https://media.example/2.jpg'])
        self.assertEqual(calls, ['match', 'parse'])
        calls.clear()
        with override_settings(PARSEHUB_UPSTREAM_ENABLED=True, PARSEHUB_UPSTREAM_PYTHON=python), \
                patch.object(parsehub, 'run_external_command',
                    side_effect=self.transport('parsehub_generic', 'recorded_body', calls)):
            rows, _, _ = _execute_upstream('parsehub_generic', 'https://mp.weixin.qq.com/s/offline-parity', 15)
        self.assertEqual(len(rows), 30)

    def test_parsehub_late_import_is_execution_failure_not_missing_configuration(self):
        with override_settings(PARSEHUB_UPSTREAM_ENABLED=True,
                PARSEHUB_UPSTREAM_PYTHON=os.environ['PARSEHUB_COMPAT_PYTHON']), \
                patch.object(parsehub, 'run_external_command',
                    side_effect=self.transport('parsehub_generic', 'local_import', [])):
            with self.assertRaises(ProviderLabError) as caught:
                _execute_upstream('parsehub_generic', 'https://mp.weixin.qq.com/s/offline-parity', 15)
        self.assertEqual(caught.exception.code, 'PROVIDER_EXECUTOR_FAILED')
        self.assertEqual(caught.exception.detail, 'UPSTREAM_RUNTIME_IMPORT_ERROR')

    def test_unmodified_worker_identity_from_project_and_temporary_cwd(self):
        # No bootstrap on this case. Identity alone does not prove extraction.
        with override_settings(YTDLP_BINARY=shlex.quote(sys.executable) + ' -m yt_dlp'):
            command, _ = matching._executor('yt_dlp_generic')
        with tempfile.TemporaryDirectory(prefix='parity cwd ') as directory:
            for cwd in (ROOT, Path(directory)):
                done = subprocess.run(command, input=json.dumps({'action':'identity', 'provider_id':'yt_dlp_generic'}),
                    cwd=cwd, capture_output=True, text=True, timeout=10,
                    env={'PATH':os.environ.get('PATH',''), 'HOME':directory, 'YTDLP_NO_PLUGINS':'1'})
                self.assertEqual(done.returncode, 0)
                self.assertEqual(json.loads(done.stdout)['payload']['version'], '2026.3.17')

    def test_native_census_entry_preserves_observations_without_product_probe(self):
        from .management.commands.capture_upstream import collect
        from .services.platforms import external_tools
        python = os.environ['PARSEHUB_COMPAT_PYTHON']
        with override_settings(YTDLP_PROVIDER_ENABLED=True, GALLERYDL_PROVIDER_ENABLED=True,
                YTDLP_BINARY=shlex.quote(sys.executable)+' -m yt_dlp',
                GALLERYDL_BINARY=shlex.quote(sys.executable)+' -m gallery_dl',
                PARSEHUB_UPSTREAM_ENABLED=True, PARSEHUB_UPSTREAM_PYTHON=python), \
                patch('socket.getaddrinfo', return_value=[(2,1,6,'',('8.8.8.8',443))]), \
                patch('socket.socket.connect', side_effect=AssertionError('parent network forbidden')):
            for provider, scenario in [('yt_dlp_generic','recorded_audio'),
                    ('gallery_dl_generic','recorded_gallery'), ('parsehub_generic','recorded_body')]:
                with self.subTest(provider=provider):
                    calls=[]
                    def run(command, **kwargs):
                        calls.append(command)
                        self.assertEqual(Path(command[3]).name, 'upstream_census_worker.py')
                        return run_external_command([*command[:3],'-c',BOOTSTRAP,command[3],provider,scenario,
                            str(ROOT/'xhs_download/fixtures/native_replay')], **kwargs)
                    with patch.object(external_tools,'run_external_command',side_effect=run):
                        source = 'https://mp.weixin.qq.com/s/offline-parity' if provider=='parsehub_generic' else SOURCE
                        result=collect(provider,source,timeout=10)
                    self.assertEqual(result['status'],'RETURNED')
                    self.assertTrue(result['observation']['records'])
                    self.assertEqual(result['application_compatibility'],'NOT_EVALUATED')
                    self.assertEqual(len(calls),1)

    def test_initial_missing_module_is_not_a_late_runtime_failure(self):
        from tools.upstream_match_worker import safe_error
        self.assertEqual(safe_error(ModuleNotFoundError('private'), 'identity')['error'], 'unavailable')
        self.assertEqual(safe_error(ModuleNotFoundError('private'), 'ytdlp_probe')['error'], 'runtime_import_error')
        self.assertEqual(safe_error(ValueError('network_denied'), 'ytdlp_probe')['error'], 'network_denied')


KUKU_TRANSPORT = r'''
// Test-only final HTTP transport; execute the complete real helper CLI.
const profile={id:'fixture',authRoute:'auth-fixture',authResponseFields:{key:'key',seed:'seed'},
 parseRequestFields:{key:'key',payload:'payload',iv:'iv',version:'revision'}};
const deny=()=>{throw new Error('unexpected real network');};
require('node:net').Socket.prototype.connect=deny;
require('node:dns').lookup=deny;
globalThis.fetch=async(url,options={})=>{
 const u=new URL(url); if(u.origin!=='https://dy.kukutool.com')deny();
 const json=value=>new Response(JSON.stringify(value),{headers:{'content-type':'application/json'}});
 if(u.pathname==='/')return new Response('<script src="/fixture.js"></script>');
 if(u.pathname==='/fixture.js')return new Response(JSON.stringify({activeProfileId:'fixture',profiles:[profile]}));
 if(u.pathname==='/api/auth-fixture')return json({key:'synthetic-key',seed:'synthetic-seed'});
 if(u.pathname==='/api/parse'){
  if(options.method!=='POST')throw new Error('wrong method');
  return json({status:0,data:{title:'fixture',audio_url:'https://media.example/audio.mp3'}});
 }
 throw new Error('unexpected HTTP step');
};
'''


@override_settings(CACHES={'default':{'BACKEND':'django.core.cache.backends.locmem.LocMemCache',
                                     'LOCATION':'process-parity-services'}},
    THIRD_PARTY_PROVIDER_ENABLED=True, THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=True,
    TIANTIANJIEXI_PROVIDER_ENABLED=True, TIANTIANJIEXI_PHONE='synthetic-account',
    TIANTIANJIEXI_PASSWORD='synthetic-password', TIANTIANJIEXI_API_BASE_URL='https://service.example/parse')
class ServiceEntryParityTests(SimpleTestCase):
    def setUp(self):
        from django.core.cache import cache
        cache.clear()
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.folder = self.stack.enter_context(tempfile.TemporaryDirectory(prefix='service parity '))
        self.stack.enter_context(patch('socket.getaddrinfo', return_value=[(2,1,6,'',('8.8.8.8',443))]))
        self.stack.enter_context(patch('socket.socket.connect', side_effect=AssertionError('network forbidden')))
        self.stack.enter_context(patch('xhs_download.services.platforms.tiantian_serial.lock_root',
                                      return_value=Path(self.folder)))

    def test_tiantian_real_client_transport_and_business_rejection(self):
        import requests
        from urllib.parse import parse_qs
        from django.core.cache import cache
        for code in (200, 451):
            cache.clear()
            requests_seen = []
            def send(session, request, **kwargs):
                requests_seen.append(request)
                self.assertEqual(request.method, 'POST')
                self.assertEqual(parse_qs(request.body)['link'], [SOURCE])
                response = requests.Response()
                response.status_code = 200
                response._content = json.dumps({'code':code,'msg':'\u6682\u4e0d\u652f\u6301\u8be5\u94fe\u63a5' if code==451 else 'ok',
                    'data':{'title':'fixture','audio_url':'https://media.example/audio.mp3'} if code==200 else {}}).encode()
                return response
            with patch.object(requests.Session, 'send', send):
                if code == 200:
                    result = execute_provider('tiantian_existing', SOURCE, service_authorized=True)
                    self.assertEqual(result.metadata['upstream_candidates'][0]['media_type'], 'audio')
                    self.assertEqual(result.post_type, 'audio')
                else:
                    with self.assertRaises(ProviderLabError) as caught:
                        execute_provider('tiantian_existing', SOURCE, service_authorized=True)
                    self.assertEqual(caught.exception.code, 'PROVIDER_UNSUPPORTED')
                    self.assertEqual(caught.exception.detail, 'SERVICE_BUSINESS_CODE_451')
            self.assertEqual(len(requests_seen), 1)

    def test_kukutool_real_helper_cli_to_shared_audio_candidate(self):
        from .services.platforms import douyin_kukutool as kuku
        node = shutil.which('node')
        self.assertIsNotNone(node)
        preload = Path(self.folder) / 'offline HTTP.cjs'
        preload.write_text(KUKU_TRANSPORT)
        real_run = subprocess.run
        calls = []
        def run(command, **kwargs):
            calls.append(command)
            self.assertEqual(command, [node, str(kuku.KUKUTOOL_HELPER), SOURCE])
            self.assertNotIn('NODE_OPTIONS', kwargs['env'])
            return real_run([command[0], '--require', str(preload), *command[1:]], **kwargs)
        with patch.object(kuku, 'get_kukutool_runtime_status', return_value={
                'checked':True,'available':True,'node_binary':node}), \
                patch.object(kuku.subprocess, 'run', side_effect=run):
            result = execute_provider('kukutool_existing', SOURCE, service_authorized=True)
        self.assertEqual(len(calls), 1)
        self.assertEqual(result.metadata['upstream_candidates'][0]['media_type'], 'audio')
        self.assertEqual(result.post_type, 'audio')


VIDEODL_NATIVE_STUB = r'''
import json, runpy, socket, sys, importlib.metadata
from pathlib import Path
from types import ModuleType, SimpleNamespace
target, fixture = sys.argv[1:]
def deny(*args, **kwargs): raise AssertionError('offline network')
socket.getaddrinfo=deny
socket.socket.connect=deny
class NoNetwork(BaseException): pass
def audit(event,args):
    if event in {'socket.connect','socket.getaddrinfo','socket.sendto'}: raise NoNetwork()
sys.addaudithook(audit)
data=json.loads(Path(fixture).read_text())
class Backend:
    @classmethod
    def belongto(cls,source): return True
    def parsefromurl(self,source,**kwargs):
        return [SimpleNamespace(**{**row['fields'],'with_valid_download_url':True}) for row in data['records'][0]]
package=ModuleType('videodl')
package.__license__='PolyForm-Noncommercial-1.0.0'
modules=ModuleType('videodl.modules')
modules.VideoClientBuilder=SimpleNamespace(REGISTERED_MODULES={'AcFunVideoClient':Backend})
modules.CommonVideoClientBuilder=SimpleNamespace(REGISTERED_MODULES={})
modules.BuildVideoClient=lambda config: Backend()
modules.BuildCommonVideoClient=lambda config: Backend()
client=ModuleType('videodl.videodl')
client.VideoClient=Backend
sys.modules.update({'videodl':package,'videodl.modules':modules,'videodl.videodl':client})
original_version=importlib.metadata.version
original_dist=importlib.metadata.distribution
importlib.metadata.version=lambda name: '0.9.1' if name=='videofetch' else original_version(name)
importlib.metadata.distribution=lambda name: SimpleNamespace(read_text=lambda _:json.dumps({
    'vcs_info':{'commit_id':'6cd1de0fe1209380b183b654c65e98a629637873'}})) if name=='videofetch' else original_dist(name)
sys.argv=[target]
runpy.run_path(target,run_name='__main__')
'''


class VideodlEntryParityTests(SimpleTestCase):
    def test_real_worker_protocol_and_recorded_native_export(self):
        # CI does not install videodl. This stubs its package identity and native
        # backend boundary, NOT Worker main/normalization/parent protocol. Actual
        # installed pin checks remain a separate local --check-only observation.
        from .services.provider_lab import runtime
        root = ROOT/'tools/provider_lab/runtime/workers'
        with tempfile.TemporaryDirectory(prefix='videodl entry ') as directory:
            directory = Path(directory)
            for name in ('videodl_worker.py','worker_common.py'):
                shutil.copyfile(root/name, directory/name)
            wrapper=directory/'bootstrap.py'
            wrapper.write_text(VIDEODL_NATIVE_STUB)
            real_process=runtime._execute_external_process
            def process(command, **kwargs):
                self.assertEqual(command, [sys.executable, str((directory/'videodl_worker.py').resolve())])
                self.assertNotEqual(Path(kwargs['cwd']), ROOT)
                self.assertEqual(kwargs['env']['PYTHONNOUSERSITE'], '1')
                return real_process([command[0],str(wrapper),command[1],
                    str(ROOT/'xhs_download/fixtures/native_replay/50db251dc5c7b043b8feb342.json')],**kwargs)
            with patch.object(runtime,'_execute_external_process',side_effect=process):
                result=runtime.execute_provider_worker(python=sys.executable,worker=directory/'videodl_worker.py',
                    request_id='offline-parity',source=SOURCE,timeout_seconds=5,operation='backend',
                    backend_id='AcFunVideoClient',backend_category='dedicated')
        self.assertEqual(result['result']['status'],'success')
        self.assertEqual([item['role'] for item in result['result']['assets']],['cover','primary'])
        self.assertTrue(result['resources'][1]['url'].endswith('.m3u8'))
