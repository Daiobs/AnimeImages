"""Linux-only deployment acceptance using real deploy/web UIDs in a disposable container."""

import argparse
import json
import os
from pathlib import Path
import pwd
import subprocess
import sys
import unittest

SOURCE = Path(__file__).resolve().parents[3]
ROOT = Path('/permission-tests')


def command(args, *, user=None, check=True, input=None):
    if user:
        args = ['runuser', '-u', user, '--', *args]
    result = subprocess.run(args, text=True, input=input, capture_output=True)
    if check and result.returncode:
        raise AssertionError(f'{args!r}\n{result.stdout}\n{result.stderr}')
    return result


def deploy_python(code, *args):
    return command([sys.executable, '-B', '-c', code, *map(str, args)], user='deploy')


def setup_container():
    if sys.platform != 'linux' or os.geteuid() != 0 or not Path('/.dockerenv').exists():
        raise SystemExit('Run only inside a disposable Linux Docker container as root.')
    for name in ('deploy', 'web', 'staticweb', 'staticdenied'):
        command(['useradd', '--create-home', '--shell', '/bin/bash', name])
    command(['groupadd','sharedcode'])
    for name in ('deploy','web','staticweb'):
        command(['usermod','-a','-G','sharedcode',name])
    ROOT.mkdir(mode=0o755)
    command(['chown', 'deploy:deploy', str(ROOT)])
    Path('/etc/sudoers.d/permission-tests').write_text('deploy ALL=(ALL) NOPASSWD: ALL\n')
    Path('/etc/sudoers.d/permission-tests').chmod(0o440)
    command(['visudo', '-cf', '/etc/sudoers.d/permission-tests'])
    Path('/usr/local/bin/systemctl').write_text('''#!/bin/sh
printf '%s\\n' "$*" >> /permission-tests/service-events
case "$*" in
  *User*) printf 'web\\n';;
  *Group*) printf 'web\\n';;
  *ActiveState*) cat /permission-tests/service-state;;
  *MainPID*) printf '0\\n';;
  *is-active*) printf 'inactive\\n'; exit 3;;
  *restart*|*status*|*daemon-reload*) exit 0;;
  *) echo 'Unexpected systemctl arguments' >&2; exit 90;;
esac
''')
    Path('/usr/local/bin/systemctl').chmod(0o755)
    deploy_python("from pathlib import Path; p=Path('/permission-tests/service-events'); p.touch(); p.chmod(0o666)")
    deploy_python("from pathlib import Path; Path('/permission-tests/service-state').write_text('inactive\\n')")


CREATE = r'''
import os, pathlib, subprocess, sys
root = pathlib.Path(sys.argv[1])
root.mkdir(mode=0o755)
repo = root / 'repo'
repo.mkdir(mode=0o755)
def git(*args):
    return subprocess.check_output(['git', '-C', str(repo), *args], text=True).strip()
git('init', '-b', 'main')
git('config', 'user.email', 'fixture@example.invalid')
git('config', 'user.name', 'Permission fixture')
(repo / 'probe.py').write_text('VALUE = 42\n')
(repo / 'templates').mkdir()
(repo / 'templates/page.html').write_text('hello {{ name }}')
(repo / 'static').mkdir()
(repo / 'static/probe.css').write_text('body { color: black; }')
git('add', '.')
git('commit', '-m', 'baseline')
(repo / 'new_probe.py').write_text('VALUE = 43\n')
git('add', '.')
git('commit', '-m', 'target')
target = git('rev-parse', 'HEAD')
git('reset', '--hard', 'HEAD~1')
os.umask(0o077)
git('reset', '--hard', target)
archive = root / 'archive'
archive.mkdir(mode=0o755)
archive.chmod(0o755)
producer = subprocess.Popen(['git', '-C', str(repo), 'archive', target], stdout=subprocess.PIPE)
subprocess.run(['tar', '-x', '-C', str(archive)], stdin=producer.stdout, check=True)
producer.stdout.close()
assert producer.wait() == 0
print(oct((repo/'new_probe.py').stat().st_mode & 0o777))
print(oct((archive/'probe.py').stat().st_mode & 0o777))
'''


WEB_CHECK = r'''
import pathlib, sys
from django.conf import settings
from django.template import Context, Engine
root = pathlib.Path(sys.argv[1])
assert root.joinpath('probe.py').read_text()
sys.path.insert(0, str(root))
import probe
assert probe.VALUE == 42
settings.configure()
assert Engine(dirs=[str(root/'templates')]).get_template('page.html').render(Context({'name':'web'})) == 'hello web'
assert 'color' in (root/'static/probe.css').read_text()
print('web read/import/template/static OK')
'''


class OldCounterexample(unittest.TestCase):
    def test_umask077_archive_and_reset_deny_real_web_user(self):
        root = ROOT / 'old-counterexample'
        evidence = deploy_python(CREATE, root)
        self.assertEqual(evidence.stdout.splitlines(), ['0o600', '0o600'])
        denied = command(['cat', str(root/'repo/new_probe.py')], user='web', check=False)
        self.assertNotEqual(denied.returncode, 0)
        denied = command([sys.executable, '-B', '-c', WEB_CHECK, str(root/'archive')], user='web', check=False)
        self.assertNotEqual(denied.returncode, 0)
        self.assertIn('PermissionError', denied.stderr)
        print('BEFORE: outer umask 077 -> reset/archive files 0600; real web UID denied')


FIXTURE = r"""
import json, pathlib, shutil, subprocess, sys
root, source = map(pathlib.Path, sys.argv[1:])
root.mkdir(mode=0o755)
tool = root/'tooling'
tool.mkdir(mode=0o755)
(tool/'scripts').mkdir()
for name in ('deploy.sh', 'pre_deploy_check.sh', 'check_deploy_readability.py', 'restart.sh', 'health_check.sh'):
    shutil.copy2(source/'scripts'/name, tool/'scripts'/name)
def git(repo, *args):
    return subprocess.check_output(['git','-C',str(repo),*args],text=True).strip()
def init(repo):
    git(repo,'init','-b','main')
    git(repo,'config','user.email','fixture@example.invalid')
    git(repo,'config','user.name','Permission fixture')
init(tool)
git(tool,'add','.')
git(tool,'commit','-m','actual deploy tooling')
tool_sha=git(tool,'rev-parse','HEAD')
repo=root/'app'
repo.mkdir(mode=0o755)
init(repo)
files={
 'probe.py':'VALUE = 42\n',
 'djangoProject/__init__.py':"import os, pwd\nif os.geteuid() == pwd.getpwnam('web').pw_uid:\n    assert 'FIXTURE_SECRET' not in os.environ, 'production environment leaked to consumer probe'\n",
 'djangoProject/settings.py': '''SECRET_KEY='synthetic-offline'
INSTALLED_APPS=['django.contrib.staticfiles']
DATABASES={'default':{'ENGINE':'django.db.backends.sqlite3','NAME':':memory:'}}
from pathlib import Path
BASE_DIR=Path(__file__).resolve().parent.parent
STATIC_URL='/static/'
STATIC_ROOT=BASE_DIR/'staticfiles'
STATICFILES_DIRS=[BASE_DIR/'static']
TEMPLATES=[{'BACKEND':'django.template.backends.django.DjangoTemplates','DIRS':[BASE_DIR/'templates']}]
ROOT_URLCONF='djangoProject.urls'
''',
 'djangoProject/urls.py':'urlpatterns=[]\n',
 'templates/page.html':'hello {{ name }}',
 'static/probe.css':'body { color: black; }',
 'requirements.txt':'# Fixture dependencies are preinstalled in the real isolated venv.\n',
 'manage.py': '''import os, sys
from pathlib import Path
os.environ.setdefault('DJANGO_SETTINGS_MODULE','djangoProject.settings')
with open(os.environ['FIXTURE_EVENTS'],'a') as f: f.write('manage '+ ' '.join(sys.argv[1:])+'\\n')
from django.core.management import execute_from_command_line
execute_from_command_line(sys.argv)
if sys.argv[1] == 'collectstatic' and os.environ.get('FIXTURE_BREAK_STATIC') == '1':
    Path(__file__).parent.joinpath('staticfiles/probe.css').chmod(0o600)
if sys.argv[1] == 'collectstatic' and os.environ.get('FIXTURE_BREAK_CODE') == '1':
    Path(__file__).parent.joinpath('probe.py').chmod(0o600)
if sys.argv[1] == 'collectstatic' and os.environ.get('FIXTURE_STATIC_ESCAPE') == '1':
    p = Path(__file__).parent.joinpath('staticfiles/probe.css')
    p.unlink()
    p.symlink_to('/etc/hosts')
''',
}
for name,content in files.items():
    p=repo/name
    p.parent.mkdir(parents=True,exist_ok=True)
    p.write_text(content)
git(repo,'add','.')
git(repo,'commit','-m','before')
before=git(repo,'rev-parse','HEAD')
(repo/'new_probe.py').write_text('VALUE = 43\n')
git(repo,'add','.')
git(repo,'commit','-m','target')
target=git(repo,'rev-parse','HEAD')
remote=root/'origin.git'
subprocess.run(['git','clone','--bare',str(repo),str(remote)],check=True,stdout=subprocess.DEVNULL)
git(repo,'remote','add','origin',str(remote))
git(repo,'reset','--hard',before)
private=root/'private'
private.mkdir(mode=0o700)
(private/'backup.sql').write_text('synthetic-private-backup')
(private/'backup.sql').chmod(0o600)
env=private/'env'
env.write_text('FIXTURE_EVENTS='+str(root/'events')+'\nPIP_NO_INDEX=1\nPIP_DISABLE_PIP_VERSION_CHECK=1\nRUN_HEALTH_CHECK=0\nFIXTURE_SECRET=synthetic-secret\n')
env.chmod(0o600)
state=private/'state'
state.mkdir(mode=0o700)
stage=root/'stage'
stage.mkdir(mode=0o755)
(root/'events').write_text('')
print(json.dumps(dict(root=str(root),app=str(repo),tool=str(tool),before=before,target=target,tool_sha=tool_sha,env=str(env),state=str(state),stage=str(stage))))
"""


class RealApplicationAcceptance(unittest.TestCase):
    def test_real_wsgi_template_and_static_as_web(self):
        code = r'''
import os, pathlib, pwd, sys
assert os.geteuid() == pwd.getpwnam('web').pw_uid != 0
root = pathlib.Path('/source')
assert not os.access(root, os.W_OK)
sys.path.insert(0, str(root))
from django.core.wsgi import get_wsgi_application
application = get_wsgi_application()
assert callable(application)
from django.conf import settings
assert settings.DATABASES['default']['ENGINE'] == 'django.db.backends.sqlite3'
assert settings.DATABASES['default']['NAME'] == ':memory:'
assert not settings.THIRD_PARTY_PROVIDER_ENABLED
assert not settings.THIRD_PARTY_PUBLIC_ACCESS_ENABLED
from django.template.loader import get_template
template = get_template('base.html')
assert pathlib.Path(template.origin.name).resolve() == root/'templates/base.html'
assert (root/'static/brand/favicon.svg').read_bytes()
print('real AnimeImages WSGI/template/static OK as web UID')
'''
        result = command([
            'env', '-i', 'PATH=/usr/local/bin:/usr/bin:/bin',
            'PYTHONDONTWRITEBYTECODE=1', 'DJANGO_ENV=development',
            'DJANGO_SECRET_KEY=synthetic-permission-test-not-a-real-secret',
            'DJANGO_DATABASE_URL=sqlite::memory:',
            'DJANGO_SETTINGS_MODULE=djangoProject.settings',
            'THIRD_PARTY_PROVIDER_ENABLED=False',
            'THIRD_PARTY_PUBLIC_ACCESS_ENABLED=False',
            'THIRD_PARTY_KUKUTOOL_PROVIDER_ENABLED=False',
            '/opt/permission-runtime/bin/python', '-I', '-B', '-c', code,
        ], user='web')
        self.assertIn('real AnimeImages WSGI/template/static OK', result.stdout)


class DeploymentAcceptance(unittest.TestCase):
    def setUp(self):
        self.root = ROOT/self._testMethodName
        output = deploy_python(FIXTURE, self.root, SOURCE)
        self.fixture = json.loads(output.stdout.splitlines()[-1])
        deploy_python("from pathlib import Path; Path('/permission-tests/service-events').write_text('')")
        deploy_python("from pathlib import Path; Path('/permission-tests/service-state').write_text('inactive\\n')")

    def run_deploy(self, *, shell='umask 077; exec bash "$1"', **overrides):
        f = self.fixture
        env = dict(APP_DIR=f['app'], ENV_FILE=f['env'], DEPLOY_USER='deploy', DEPLOY_GROUP='deploy',
                   DEPLOY_BRANCH='main', DEPLOY_TOOL_SHA=f['tool_sha'], DEPLOY_TARGET_SHA=f['target'],
                   WEB_USER='web', STATIC_USER='staticweb', CODE_UMASK='0022',
                   DEPLOY_STATE_DIR=f['state'], DEPLOY_STAGE_PARENT=f['stage'],
                   PYTHON_BIN='/opt/permission-runtime/bin/python', SERVICE_NAME='fixture',
                   PATH='/usr/local/bin:/usr/bin:/bin', PYTHONDONTWRITEBYTECODE='1', RUN_HEALTH_CHECK='0')
        env.update(overrides)
        return command(['env', '-i', *[f'{k}={v}' for k,v in env.items()], 'bash', '-c',
                        shell, 'fixture', f["tool"]+'/scripts/deploy.sh'],
                       user='deploy', check=False)

    def head(self):
        return command(['git','-C',self.fixture['app'],'rev-parse','HEAD'],user='deploy').stdout.strip()

    def assert_no_switch_or_restart(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertEqual(self.head(), self.fixture['before'])
        events = command(['cat', '/permission-tests/service-events'],user='deploy').stdout
        self.assertNotIn('restart', events)
        self.assertEqual(command(['cat',str(self.root/'events')],user='deploy').stdout,'')

    def test_prepare_only_does_not_switch_or_restart(self):
        result = self.run_deploy(DEPLOY_PREPARE_ONLY='1')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.head(), self.fixture['before'])
        self.assertNotIn('restart',command(['cat','/permission-tests/service-events'],user='deploy').stdout)
        self.assertEqual(command(['cat',str(self.root/'events')],user='deploy').stdout,'')

    def test_success_reads_imports_templates_static_without_exposing_private_backup(self):
        result = self.run_deploy()
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.head(),self.fixture['target'])
        result = command(['/opt/permission-runtime/bin/python','-B','-c',WEB_CHECK,self.fixture['app']],user='web')
        self.assertIn('web read/import/template/static OK',result.stdout)
        command(['cat',self.fixture['app']+'/new_probe.py'],user='web')
        command(['cat',self.fixture['app']+'/staticfiles/probe.css'],user='staticweb')
        for user in ('web', 'staticweb', 'staticdenied'):
            for path in (self.root/'private/backup.sql', Path(self.fixture['env'])):
                denied=command(['cat',str(path)],user=user,check=False)
                self.assertNotEqual(denied.returncode,0)
            self.assertNotEqual(command(['ls',self.fixture['state']],user=user,check=False).returncode,0)
        denied=command(['/opt/permission-runtime/bin/python','-B','-c',
                        "open(__import__('sys').argv[1],'ab').close()",self.fixture['app']+'/probe.py'],user='web',check=False)
        self.assertNotEqual(denied.returncode,0)
        events=command(['cat',str(self.root/'events')],user='deploy').stdout
        self.assertIn('manage migrate',events)
        self.assertIn('manage collectstatic',events)
        self.assertIn('restart',command(['cat','/permission-tests/service-events'],user='deploy').stdout)

    def test_existing_mode600_rejected_before_switch(self):
        command(['chmod','600',self.fixture['app']+'/probe.py'],user='deploy')
        self.assert_no_switch_or_restart(self.run_deploy())
        mode=command(['stat','-c','%a',self.fixture['app']+'/probe.py'],user='deploy').stdout.strip()
        self.assertEqual(mode,'600')

    def test_nontraversable_existing_directory_rejected_before_switch(self):
        command(['chmod','700',self.fixture['app']+'/templates'],user='deploy')
        self.assert_no_switch_or_restart(self.run_deploy())

    def test_consumer_owned_readonly_file_rejected_before_switch(self):
        path = self.fixture['app']+'/static/probe.css'
        command(['chown', 'web:web', path])
        command(['chmod', '444', path])
        result = self.run_deploy(DEPLOY_PREPARE_ONLY='1')
        self.assert_no_switch_or_restart(result)
        self.assertIn('consumer owns application artifact', result.stderr)
        command(['chmod', 'u+w', path], user='web')
        command([sys.executable, '-B', '-c',
                 "from pathlib import Path; import sys; Path(sys.argv[1]).write_text('consumer changed content')",
                 path], user='web')

    def test_consumer_owned_readonly_directory_rejected_before_switch(self):
        path = self.fixture['app']+'/templates'
        command(['chown', 'web:web', path])
        command(['chmod', '555', path])
        result = self.run_deploy(DEPLOY_PREPARE_ONLY='1')
        self.assert_no_switch_or_restart(result)
        self.assertIn('consumer owns application artifact', result.stderr)
        command(['chmod', 'u+w', path], user='web')
        command(['touch', path+'/consumer-created'], user='web')

    def test_consumer_writable_ancestor_rejected_before_switch(self):
        command(['chgrp', 'sharedcode', str(self.root)], user='deploy')
        command(['chmod', '775', str(self.root)], user='deploy')
        result = self.run_deploy(DEPLOY_PREPARE_ONLY='1')
        self.assert_no_switch_or_restart(result)
        self.assertIn('consumer can replace application artifacts via', result.stderr)
        command([sys.executable, '-B', '-c', '''from pathlib import Path
import sys
app = Path(sys.argv[1])
app.rename(app.with_name('replaced-app'))
app.mkdir()
(app/'consumer-created').write_text('replacement')
''', self.fixture['app']], user='web')

    def test_sticky_tmp_allows_deploy_owned_candidate(self):
        result = self.run_deploy(DEPLOY_PREPARE_ONLY='1', DEPLOY_STAGE_PARENT='/tmp')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.head(), self.fixture['before'])
        self.assertNotIn('restart', command(['cat', '/permission-tests/service-events'], user='deploy').stdout)
        self.assertEqual(command(['cat', str(self.root/'events')], user='deploy').stdout, '')

    def test_multihop_static_link_rejects_consumer_controlled_intermediate(self):
        hop = self.root/'consumer-hop'
        command(['mkdir', str(hop)])
        command(['ln', '-s', self.fixture['app']+'/static/probe.css', str(hop/'middle')])
        command(['chown', 'staticweb:staticweb', str(hop)])
        command(['chmod', '555', str(hop)])
        command(['ln', '-s', str(hop/'middle'), self.fixture['app']+'/static/alias.css'], user='deploy')
        result = command(['/opt/permission-runtime/bin/python', '-I', '-B',
                          self.fixture['tool']+'/scripts/check_deploy_readability.py',
                          '--expected-uid', str(pwd.getpwnam('staticweb').pw_uid),
                          '--root', self.fixture['app']+'/static', '--static'],
                         user='staticweb', check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('consumer owns application artifact', result.stderr)
        command(['chmod', 'u+w', str(hop)], user='staticweb')
        command(['rm', str(hop/'middle')], user='staticweb')
        command(['ln', '-s', '/etc/hosts', str(hop/'middle')], user='staticweb')

    def test_consumer_owned_runtime_alias_rejected(self):
        alias = Path('/tmp')/self._testMethodName
        command(['ln', '-s', '/opt/permission-runtime', str(alias)], user='web')
        paths = command(['git', '-C', self.fixture['app'], 'ls-tree', '-r',
                         '--name-only', '-z', 'HEAD'], user='deploy').stdout
        result = command(['/opt/permission-runtime/bin/python', '-I', '-B',
                          self.fixture['tool']+'/scripts/check_deploy_readability.py',
                          '--expected-uid', str(pwd.getpwnam('web').pw_uid),
                          '--root', self.fixture['app'], '--runtime-root', str(alias),
                          '--paths-stdin'], user='web', input=paths, check=False)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('consumer owns application artifact', result.stderr)
        command(['rm', str(alias)], user='web')
        command(['ln', '-s', self.fixture['app'], str(alias)], user='web')

    def test_wrong_tool_sha_rejected_before_switch(self):
        self.assert_no_switch_or_restart(self.run_deploy(DEPLOY_TOOL_SHA='0'*40))

    def test_wrong_web_identity_rejected_before_switch(self):
        self.assert_no_switch_or_restart(self.run_deploy(WEB_USER='staticweb'))

    def test_candidate_static_identity_denied_before_switch(self):
        command(['chgrp', 'sharedcode', self.fixture['app']], user='deploy')
        result = self.run_deploy(CODE_UMASK='0027', DEPLOY_GROUP='sharedcode',
                                 STATIC_USER='staticdenied')
        self.assert_no_switch_or_restart(result)
        self.assertIn('[readability] FAIL:', result.stderr)
        self.assertIn('Permission denied', result.stderr)
        self.assertIn('/static', result.stderr)

    def assert_synthetic_backup_private(self):
        path = self.root/'private/scoped-backup.sql'
        self.assertEqual(command(['stat', '-c', '%a', str(path)], user='deploy').stdout.strip(), '600')
        for user in ('web', 'staticweb', 'staticdenied'):
            self.assertNotEqual(command(['cat', str(path)], user=user, check=False).returncode, 0)

    def test_scoped_synthetic_backup_success_preserves_outer_mask(self):
        result = self.run_deploy(DEPLOY_PREPARE_ONLY='1', shell='''
umask 0022
(umask 077; bash -c 'printf synthetic-backup > "$APP_DIR/../private/scoped-backup.sql"') &&
{ printf 'OUTER_MASK=%s\\n' "$(umask)"; exec bash "$1"; }
''')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('OUTER_MASK=0022', result.stdout)
        self.assertEqual(self.head(), self.fixture['before'])
        self.assert_synthetic_backup_private()

    def test_scoped_synthetic_backup_failure_and_gate(self):
        result = self.run_deploy(shell='''
umask 0022
(umask 077; bash -c 'printf partial-backup > "$APP_DIR/../private/scoped-backup.sql"; exit 42') && exec bash "$1"
status=$?
printf 'OUTER_MASK=%s\\n' "$(umask)"
exit "$status"
''')
        self.assertEqual(result.returncode, 42)
        self.assertIn('OUTER_MASK=0022', result.stdout)
        self.assert_no_switch_or_restart(result)
        self.assertEqual(command(['cat', '/permission-tests/service-events'], user='deploy').stdout, '')
        self.assert_synthetic_backup_private()

    def test_scoped_synthetic_backup_failure_set_e_gate(self):
        result = self.run_deploy(shell='''
set -e
umask 0022
(umask 077; bash -c 'printf partial-backup > "$APP_DIR/../private/scoped-backup.sql"; exit 42')
exec bash "$1"
''')
        self.assertEqual(result.returncode, 42)
        self.assert_no_switch_or_restart(result)
        self.assertEqual(command(['cat', '/permission-tests/service-events'], user='deploy').stdout, '')
        self.assert_synthetic_backup_private()

    def test_collected_static_external_file_link_never_restarts(self):
        deploy_python("from pathlib import Path; p=Path(__import__('sys').argv[1]); p.write_text(p.read_text()+'FIXTURE_STATIC_ESCAPE=1\\n')", self.fixture['env'])
        result = self.run_deploy()
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(self.head(), self.fixture['target'])
        self.assertIn('artifact link escapes public root', result.stderr)
        self.assertIn('manage collectstatic', command(['cat', str(self.root/'events')], user='deploy').stdout)
        self.assertNotIn('restart', command(['cat', '/permission-tests/service-events'], user='deploy').stdout)

    def test_nontraversable_stage_parent_rejected_before_switch(self):
        command(['chmod','700',self.fixture['stage']],user='deploy')
        self.assert_no_switch_or_restart(self.run_deploy())

    def test_post_reset_dependency_failure_never_restarts(self):
        deploy_python("from pathlib import Path; p=Path(__import__('sys').argv[1]); p.write_text(p.read_text()+'PIP_CONSTRAINT=/nonexistent-fixture-constraint\\n')",self.fixture['env'])
        result=self.run_deploy()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.head(),self.fixture['target'])
        self.assertIn(self.fixture['before'],result.stdout+result.stderr)
        self.assertNotIn('restart',command(['cat','/permission-tests/service-events'],user='deploy').stdout)
        self.assertEqual(command(['cat',str(self.root/'events')],user='deploy').stdout,'')

    def test_unreadable_collected_static_never_restarts(self):
        deploy_python("from pathlib import Path; p=Path(__import__('sys').argv[1]); p.write_text(p.read_text()+'FIXTURE_BREAK_STATIC=1\\n')",self.fixture['env'])
        result=self.run_deploy()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.head(),self.fixture['target'])
        self.assertIn('manage collectstatic',command(['cat',str(self.root/'events')],user='deploy').stdout)
        self.assertNotIn('restart',command(['cat','/permission-tests/service-events'],user='deploy').stdout)

    def test_active_service_rejected_before_switch(self):
        deploy_python("from pathlib import Path; Path('/permission-tests/service-state').write_text('active\\n')")
        self.assert_no_switch_or_restart(self.run_deploy())

    def test_final_code_unreadable_never_restarts(self):
        deploy_python("from pathlib import Path; p=Path(__import__('sys').argv[1]); p.write_text(p.read_text()+'FIXTURE_BREAK_CODE=1\\n')",self.fixture['env'])
        result=self.run_deploy()
        self.assertNotEqual(result.returncode,0,result.stdout+result.stderr)
        self.assertEqual(self.head(),self.fixture['target'])
        self.assertIn('manage collectstatic',command(['cat',str(self.root/'events')],user='deploy').stdout)
        self.assertNotIn('restart',command(['cat','/permission-tests/service-events'],user='deploy').stdout)

    def test_private_record_creation_failure_never_switches(self):
        output=deploy_python('''from pathlib import Path
import sys
p=Path(sys.argv[1])/'long-private'
p.mkdir(mode=0o700)
while len(str(p)) < 4060:
    p=p/('d'*min(200,4060-len(str(p))-1))
    p.mkdir(mode=0o700)
print(p)
''',self.root)
        result=self.run_deploy(DEPLOY_STATE_DIR=output.stdout.strip())
        self.assert_no_switch_or_restart(result)
        self.assertIn('File name too long',result.stderr)

    def test_actual_code_backup_failure_never_switches(self):
        # Bypass Git's stat check only in this fixture so real tar reaches the unreadable file.
        command(['git', '-C', self.fixture['app'], 'update-index', '--assume-unchanged', 'probe.py'], user='deploy')
        command(['chmod', '000', self.fixture['app']+'/probe.py'], user='deploy')
        result = self.run_deploy()
        self.assert_no_switch_or_restart(result)
        self.assertIn('tar:', result.stderr)
        self.assertIn('Permission denied', result.stderr)

    def test_actual_code_backup_private_and_preserves_content_and_mode(self):
        command(['chmod', '664', self.fixture['app']+'/probe.py'], user='deploy')
        result = self.run_deploy(DEPLOY_PREPARE_ONLY='1')
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        output = deploy_python('''import pathlib, subprocess, sys, tarfile
state, app = map(pathlib.Path, sys.argv[1:])
archives = list(state.glob('*/code-before.tar.gz'))
assert len(archives) == 1, archives
archive = archives[0]
assert archive.stat().st_mode & 0o777 == 0o600
with tarfile.open(archive) as saved:
    member = next(m for m in saved.getmembers() if m.name.removeprefix('./') == 'probe.py')
    assert member.mode == (app/'probe.py').stat().st_mode & 0o777
    assert saved.extractfile(member).read() == (app/'probe.py').read_bytes()
restore = state/'restore-inspection'
restore.mkdir(mode=0o700)
subprocess.run(['tar', '--extract', '--gzip', '--same-permissions', '--file', str(archive), '--directory', str(restore), member.name], check=True)
assert (restore/'probe.py').stat().st_mode & 0o777 == 0o664
assert (restore/'probe.py').read_bytes() == (app/'probe.py').read_bytes()
print(archive)
''', self.fixture['state'], self.fixture['app'])
        for user in ('web', 'staticweb', 'staticdenied'):
            self.assertNotEqual(command(['cat', output.stdout.strip()], user=user, check=False).returncode, 0)

    def test_candidate_creation_failure_never_switches(self):
        command(['chmod','555',self.fixture['stage']],user='deploy')
        result=self.run_deploy()
        self.assert_no_switch_or_restart(result)
        self.assertIn('mktemp',result.stderr)

    def test_private_record_is_private_and_has_exact_recovery_shas(self):
        result=self.run_deploy(DEPLOY_PREPARE_ONLY='1')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        output=deploy_python('''from pathlib import Path
import json,sys
root=Path(sys.argv[1]); records=list(root.glob('*/recovery.txt'))
assert len(records)==1
p=records[0]
print(json.dumps({'directory_mode':oct(p.parent.stat().st_mode&0o777),'file_mode':oct(p.stat().st_mode&0o777),'body':p.read_text(),'path':str(p)}))
''',self.fixture['state'])
        record=json.loads(output.stdout)
        self.assertEqual(record['directory_mode'],'0o700')
        self.assertEqual(record['file_mode'],'0o600')
        for key,field in [('tool','tool_sha'),('target','target'),('previous','before')]:
            self.assertIn(f'{key}={self.fixture[field]}',record['body'])
        for user in ('web', 'staticweb', 'staticdenied'):
            self.assertNotEqual(command(['cat',record['path']],user=user,check=False).returncode,0)

    def test_shared_group_umask0027_with_preprovisioned_directories(self):
        deploy_python('''import os, pathlib, grp, sys
gid=grp.getgrnam('sharedcode').gr_gid
root=pathlib.Path(sys.argv[1])
for path in [root, *root.rglob('*')]:
    if '.git' in path.parts: continue
    os.chown(path,-1,gid)
    path.chmod(0o2750 if path.is_dir() else 0o640)
stage=pathlib.Path(sys.argv[2]); os.chown(stage,-1,gid); stage.chmod(0o2750)
''',self.fixture['app'],self.fixture['stage'])
        result=self.run_deploy(CODE_UMASK='0027',DEPLOY_GROUP='sharedcode')
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        command(['/opt/permission-runtime/bin/python','-B','-c',WEB_CHECK,self.fixture['app']],user='web')
        command(['cat',self.fixture['app']+'/staticfiles/probe.css'],user='staticweb')
        mode=command(['stat','-c','%a',self.fixture['app']+'/new_probe.py'],user='deploy').stdout.strip()
        self.assertEqual(mode,'640')


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--counterexample-only', action='store_true')
    args = parser.parse_args()
    setup_container()
    suite = unittest.defaultTestLoader.loadTestsFromTestCase(OldCounterexample)
    if not args.counterexample_only:
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(RealApplicationAcceptance))
        suite.addTests(unittest.defaultTestLoader.loadTestsFromTestCase(DeploymentAcceptance))
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    raise SystemExit(not result.wasSuccessful())
