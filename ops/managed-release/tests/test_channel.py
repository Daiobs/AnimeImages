"""Real Linux sudo/consumer acceptance; all mutable fixtures are container-local.

Run with the cached ops/deploy/tests/Dockerfile image (never on a host):
docker --context colima run --rm --network none --user 0:0 \
    -v "$PWD:/source:ro" --entrypoint python \
    animeimages-deploy-permission-tests -B \
    /source/ops/managed-release/tests/test_channel.py

Bootstrap runs the actual installer from a private root-owned bundle, with the
three pre-existing service grants installed separately beforehand. Installer
failure cases have separate coverage; this suite exercises its normal CLI path.
Only synthetic container repositories receive fixture commits. systemctl and
curl are replaced inside the container; no service or network request is real.
"""

import fcntl
import json
import os
from pathlib import Path
import pwd
import grp
import shutil
import signal
import stat
import subprocess
import sys
import time
import unittest


SOURCE = Path(__file__).resolve().parents[3]
WORK = Path('/managed-release-channel-tests')
APP = Path('/srv/animeimages')
STAGING = Path('/srv/animeimages-deploy-staging')
CANDIDATE = STAGING / 'animeimages-candidate.aB12cD34'
HELPER = Path('/usr/local/sbin/animeimages-consumer-probe')
GATE = Path('/usr/local/libexec/animeimages-release/check_deploy_readability.py')
POLICY = Path('/etc/sudoers.d/animeimages-managed-release')
BUNDLE = Path('/root/managed-release-channel-bundle')
SYSTEMCTL = Path('/usr/bin/systemctl')
EVENTS = WORK / 'events.jsonl'
CONTROL = WORK / 'control.json'
TOOL = WORK / 'tooling'
STATE = WORK / 'private/state'
WRAPPER_STATE = Path('/home/ubuntu/.local/state/animeimages-managed-release')
TRACKED = ('djangoProject/__init__.py', 'djangoProject/settings.py',
           'djangoProject/urls.py', 'manage.py', 'requirements.txt',
           'templates/space and\nnewline.html', 'static/probe.css')
MANIFEST = ''.join(name + '\0' for name in TRACKED)
SECRETS = {'FIXTURE_SECRET': 'synthetic-secret',
           'DJANGO_SECRET_KEY': 'synthetic-django-secret',
           'DATABASE_URL': 'synthetic-database-secret',
           'AWS_SECRET_ACCESS_KEY': 'synthetic-cloud-secret'}


def command(args, *, user=None, group=None, check=True, input=None, env=None):
    args = list(map(str, args))
    if user:
        args = ['/usr/sbin/runuser', '-u', user,
                *(['-g', group] if group else []), '--', *args]
    result = subprocess.run(args, text=True, input=input, capture_output=True,
                            env=env, timeout=180)
    if check and result.returncode:
        raise AssertionError(f'{args!r}\n{result.stdout}\n{result.stderr}')
    return result


def owned(path, mode=0o755, *, user='ubuntu', directory=False, content=''):
    path = Path(path)
    identity = pwd.getpwnam(user)
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    for parent in reversed(missing):
        parent.mkdir(mode=0o755)
        os.chown(parent, identity.pw_uid, identity.pw_gid)
    if directory:
        path.mkdir(parents=True, exist_ok=True)
    else:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    os.chown(path, identity.pw_uid, identity.pw_gid)
    path.chmod(mode)
    return path


def copy_root_owned(source, destination, mode):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, destination)
    os.chown(destination, 0, 0)
    destination.chmod(mode)


def control(**overrides):
    values = dict(state='inactive', status_code=0, local_code=200, public_code=200)
    values.update(overrides)
    CONTROL.write_text(json.dumps(values))


def events():
    return [json.loads(line) for line in EVENTS.read_text().splitlines()]


def service_actions():
    return [event['args'] for event in events()
            if event['kind'] == 'systemctl' and event['args'][0] != 'show']


def sudo(args, *, target='www-data', group='www-data', input=None, env=None):
    return command(['/usr/bin/sudo', '-n', '-k', '-u', target, '-g', group,
                    '--', *args], user='ubuntu', input=input, env=env, check=False)


def probe(mode, root, *, input=MANIFEST, **kwargs):
    return sudo([HELPER, mode, root], input=input, **kwargs)


PROJECT_INIT = '''import os, pathlib, pwd, sys
if os.geteuid() == pwd.getpwnam('www-data').pw_uid:
    assert os.getegid() == pwd.getpwnam('www-data').pw_gid, 'wrong consumer gid'
    assert sys.flags.isolated and sys.dont_write_bytecode, 'Python not isolated'
    assert sys.executable == '/srv/animeimages/.venv/bin/python', 'wrong interpreter'
    for key in ('FIXTURE_SECRET', 'DJANGO_SECRET_KEY', 'DATABASE_URL',
                'AWS_SECRET_ACCESS_KEY', 'BASH_ENV', 'PYTHONPATH', 'PYTHONHOME'):
        assert key not in os.environ, 'caller environment leaked: ' + key
    for name in ('/srv/animeimages/.env.production',
                 '/managed-release-channel-tests/private/backup.sql'):
        try:
            pathlib.Path(name).read_bytes()
        except PermissionError:
            pass
        else:
            raise AssertionError('consumer read private secret: ' + name)
'''

SETTINGS = '''import os
from pathlib import Path
BASE_DIR = Path(__file__).resolve().parent.parent
SECRET_KEY = 'synthetic-offline-only'
INSTALLED_APPS = ['django.contrib.staticfiles']
DATABASES = {'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}}
STATIC_URL = '/static/'
STATIC_ROOT = Path(os.environ.get('FIXTURE_STATIC_ROOT', str(BASE_DIR/'staticfiles')))
STATICFILES_DIRS = [BASE_DIR/'static']
TEMPLATES = [{'BACKEND': 'django.template.backends.django.DjangoTemplates',
              'DIRS': [BASE_DIR/'templates']}]
ROOT_URLCONF = 'djangoProject.urls'
'''

MANAGE = '''import json, os, pathlib, sys
os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'djangoProject.settings')
with open(os.environ['FIXTURE_EVENTS'], 'a') as stream:
    stream.write(json.dumps({'kind': 'manage', 'uid': os.geteuid(),
                             'args': sys.argv[1:]}) + '\\n')
if sys.argv[1] == 'migrate' and os.environ.get('FIXTURE_PAUSE_MIGRATE') == '1':
    import signal, subprocess, time
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    descendant = subprocess.Popen([sys.executable, '-c', ''' + repr('''
import os, pathlib, signal, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
while not pathlib.Path(os.environ['FIXTURE_RELEASE']).exists():
    time.sleep(0.05)
pathlib.Path(os.environ['FIXTURE_CONTINUED']).write_text('descendant continued')
''') + '''])
    pathlib.Path(os.environ['FIXTURE_BLOCK_PID']).write_text(str(os.getpid()) + ' ' + str(descendant.pid))
    while not pathlib.Path(os.environ['FIXTURE_RELEASE']).exists():
        time.sleep(0.05)
from django.core.management import execute_from_command_line
execute_from_command_line(sys.argv)
if sys.argv[1] == 'collectstatic':
    from django.conf import settings
    if os.environ.get('FIXTURE_BREAK_STATIC') == '1':
        (settings.STATIC_ROOT/'probe.css').chmod(0o600)
    if os.environ.get('FIXTURE_BREAK_CODE') == '1':
        pathlib.Path(__file__).parent.joinpath('djangoProject/__init__.py').chmod(0o600)
'''


def application(root):
    owned(root, directory=True)
    files = dict(zip(TRACKED, (PROJECT_INIT, SETTINGS, 'urlpatterns = []\n', MANAGE,
                              '# Dependencies already installed; no network.\n',
                              'hello {{ name }}', 'body { color: black; }')))
    for name, content in files.items():
        owned(root / name, mode=0o644, content=content)


STUB = '''#!/usr/local/bin/python
import json, os, pathlib, sys
work = pathlib.Path('/managed-release-channel-tests')
control = json.loads((work/'control.json').read_text())
kind = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with (work/'events.jsonl').open('a') as stream:
    stream.write(json.dumps({'kind': kind, 'uid': os.geteuid(),
                             'gid': os.getegid(), 'args': args}) + '\\n')
if kind == 'curl':
    url = args[-1]
    if url not in ('http://127.0.0.1:8000/health/ready/',
                    'https://download.cosflow.icu/health/ready/'):
        raise SystemExit(90)
    print(control['local_code' if url.startswith('http://127.') else 'public_code'], end='')
    raise SystemExit(0)
if args[:2] == ['show', 'animeimages'] and len(args) == 5 and args[2] == '-p' and args[4] == '--value':
    active = control['state'] == 'active'
    values = {'User': 'www-data', 'Group': 'www-data', 'ActiveState': control['state'],
              'SubState': 'running' if active else 'dead', 'MainPID': '123' if active else '0'}
    print(values[args[3]])
elif args == ['show', 'animeimages', '-p', 'ActiveState', '-p', 'SubState', '-p', 'MainPID']:
    active = control['state'] == 'active'
    print('ActiveState=' + control['state'])
    print('SubState=' + ('running' if active else 'dead'))
    print('MainPID=' + ('123' if active else '0'))
elif args in (['stop', 'animeimages'], ['restart', 'animeimages']):
    assert os.geteuid() == 0, 'service mutation was not sudo root'
    control['state'] = 'inactive' if args[0] == 'stop' else 'active'
    (work/'control.json').write_text(json.dumps(control))
elif args == ['daemon-reload']:
    assert os.geteuid() == 0
elif args == ['status', 'animeimages', '--no-pager', '-l']:
    assert os.geteuid() == 0
    raise SystemExit(control['status_code'])
else:
    print('unexpected systemctl arguments', file=sys.stderr)
    raise SystemExit(90)
'''


def setup_container():
    # All destructive fixture setup stays behind these checks, including stubs.
    if sys.platform != 'linux' or os.geteuid() != 0 or not Path('/.dockerenv').exists():
        raise SystemExit('Run only as root in a disposable Linux Docker container.')
    if set(os.listdir('/sys/class/net')) != {'lo'}:
        raise SystemExit('Use docker run --network none.')
    if not os.statvfs(SOURCE).f_flag & os.ST_RDONLY:
        raise SystemExit('Mount the repository read-only at /source.')
    if Path('/var/run/docker.sock').exists():
        raise SystemExit('Do not mount a Docker socket.')
    for path in (WORK, APP, STAGING, HELPER, GATE, POLICY, BUNDLE):
        if path.exists() or path.is_symlink():
            raise SystemExit(f'Container fixture path already exists: {path}')
    for path in (SOURCE/'ops/managed-release/install.py',
                 SOURCE/'ops/managed-release/animeimages-consumer-probe',
                 SOURCE/'ops/managed-release/animeimages-managed-release.sudoers',
                 SOURCE/'scripts/check_deploy_readability.py',
                 Path('/opt/permission-runtime/bin/python'),
                 Path('/usr/local/bin/python')):
        if not path.is_file():
            raise SystemExit(f'Required fixture source missing: {path}')
    os.umask(0o022)
    hostname = os.uname().nodename
    with Path('/etc/hosts').open('a') as hosts:
        hosts.write('\n127.0.0.1 ' + hostname + '\n')
    command(['useradd', '--create-home', '--shell', '/bin/bash', '--groups', 'sudo', 'ubuntu'])
    command(['passwd', '--lock', 'ubuntu'])
    owned(WORK, user='root', directory=True)
    owned(WORK/'private', 0o700, directory=True)
    owned(STATE, 0o700, directory=True)
    owned(WORK/'private/backup.sql', 0o600, content='synthetic-private-backup')
    owned(APP, directory=True)
    owned(STAGING, directory=True)
    shutil.copytree('/opt/permission-runtime', APP/'.venv', symlinks=True)
    command(['chown', '-R', 'ubuntu:ubuntu', APP/'.venv'])
    application(APP)
    application(CANDIDATE)
    owned(APP/'staticfiles/probe.css', 0o644, content='published fixture')
    owned(APP/'.env.production', 0o600, content='FIXTURE_SECRET=synthetic-secret\n')
    EVENTS.write_text('')
    EVENTS.chmod(0o666)
    control()
    owned(SYSTEMCTL, 0o755, user='root', content=STUB)
    owned('/usr/local/bin/curl', 0o755, user='root', content=STUB)
    owned('/etc/sudoers.d/00-channel-password-group', 0o440, user='root',
          content='%sudo ALL=(ALL:ALL) PASSWD: ALL\n')
    owned('/etc/sudoers.d/50-animeimages-deploy-existing', 0o440, user='root', content=(
        'ubuntu ALL=(root) NOPASSWD: NOSETENV: /usr/bin/systemctl daemon-reload, '
        '/usr/bin/systemctl restart animeimages, '
        '/usr/bin/systemctl status animeimages --no-pager -l\n'))
    owned(BUNDLE, 0o700, user='root', directory=True)
    for source in (SOURCE/'ops/managed-release/install.py',
                   SOURCE/'ops/managed-release/animeimages-consumer-probe',
                   SOURCE/'scripts/check_deploy_readability.py',
                   SOURCE/'ops/managed-release/animeimages-managed-release.sudoers'):
        copy_root_owned(source, BUNDLE/source.name, 0o600)
    command(['/usr/local/bin/python', '-I', '-B', BUNDLE/'install.py'],
            env={'PATH': '/usr/sbin:/usr/bin:/sbin:/bin', 'LC_ALL': 'C'})


class ChannelAcceptance(unittest.TestCase):
    def setUp(self):
        EVENTS.write_text('')
        control()

    def assert_denied(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn('"status": "PASS"', result.stdout)

    def assert_pass(self, result, root, scope):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        record = json.loads(result.stdout)
        self.assertEqual(record['status'], 'PASS')
        self.assertEqual(record['uid'], pwd.getpwnam('www-data').pw_uid)
        self.assertEqual(record['gid'], grp.getgrnam('www-data').gr_gid)
        self.assertNotEqual(record['uid'], 0)
        self.assertEqual(record['root'], str(root))
        self.assertEqual(record['scope'], scope)
        self.assertGreater(record['files'], 0)
        return record

    def test_real_locked_user_and_root_owned_fixed_installation(self):
        identity = pwd.getpwnam('ubuntu')
        self.assertNotEqual(identity.pw_uid, 0)
        self.assertIn(grp.getgrnam('sudo').gr_gid, os.getgrouplist('ubuntu', identity.pw_gid))
        self.assertEqual(command(['passwd', '-S', 'ubuntu']).stdout.split()[1], 'L')
        for path, mode, source in (
            (HELPER, 0o755, SOURCE/'ops/managed-release/animeimages-consumer-probe'),
            (GATE, 0o644, SOURCE/'scripts/check_deploy_readability.py'),
            (POLICY, 0o440,
             SOURCE/'ops/managed-release/animeimages-managed-release.sudoers'),
        ):
            with self.subTest(path=path):
                self.assertEqual((path.stat().st_uid, path.stat().st_gid), (0, 0))
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                self.assertEqual(path.read_bytes(), source.read_bytes())

    def test_real_sudo_accepts_live_and_candidate_code_static(self):
        for root in (APP, CANDIDATE):
            with self.subTest(mode='code', root=root):
                record = self.assert_pass(probe('code', root), root, 'code-and-runtime')
                self.assertEqual(record['files'], len(TRACKED))
        for root in (APP/'staticfiles', CANDIDATE/'static'):
            with self.subTest(mode='static', root=root):
                self.assert_pass(probe('static', root, input=''), root, 'static')

    def test_no_general_commands_interpreter_shell_or_runas_grants(self):
        for target, group, args in (
            ('root', 'root', ['/usr/bin/true']),
            ('root', 'root', ['/usr/bin/id']),
            ('www-data', 'www-data', ['/usr/bin/id']),
            ('www-data', 'www-data', ['/bin/bash', '-c', 'true']),
            ('www-data', 'www-data', [APP/'.venv/bin/python', '-c', 'print(1)']),
            ('root', 'root', [HELPER, 'code', APP]),
            ('www-data', 'root', [HELPER, 'code', APP]),
            ('nobody', 'nogroup', [HELPER, 'code', APP]),
        ):
            with self.subTest(target=target, group=group, args=args):
                result = sudo(args, target=target, group=group, input=MANIFEST)
                self.assert_denied(result)
                self.assertIn('password is required', result.stderr)

    def test_exact_service_rules_only_and_denials_never_reach_stub(self):
        accepted = (['stop', 'animeimages'], ['daemon-reload'], ['restart', 'animeimages'],
                    ['status', 'animeimages', '--no-pager', '-l'])
        for args in accepted:
            result = sudo([SYSTEMCTL, *args], target='root', group='root')
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(service_actions(), list(accepted))
        self.assertTrue(all(event['uid'] == event['gid'] == 0 for event in events()))
        baseline = EVENTS.read_bytes()
        denied = (['stop', 'ssh'], ['restart', 'ssh'], ['status', 'ssh'],
                  ['status', 'animeimages'], ['stop', 'animeimages', '--no-block'],
                  ['stop', 'animeimages', 'ssh'], ['show', 'animeimages', '-p', 'User', '--value'])
        for args in denied:
            with self.subTest(args=args):
                result = sudo([SYSTEMCTL, *args], target='root', group='root')
                self.assert_denied(result)
                self.assertIn('password is required', result.stderr)
                self.assertEqual(EVENTS.read_bytes(), baseline)

    def test_malformed_modes_arguments_and_noncanonical_roots(self):
        cases = ([], ['code'], ['code', APP, 'extra'], ['exec', APP],
                 ['--static', APP/'staticfiles'], ['static', APP],
                 ['code', APP/'staticfiles'], ['code', '/etc'],
                 ['static', WORK], ['code', str(APP)+'/'],
                 ['code', str(APP)+'/../animeimages'], ['code', str(APP)+'/.'],
                 ['code', str(CANDIDATE)+'/static'], ['static', CANDIDATE],
                 ['code', str(STAGING)+'/animeimages-candidate.short'],
                 ['code', str(STAGING)+'/animeimages-candidate.aB12cD345'],
                 ['code', str(STAGING)+'/animeimages-candidate.aB12cD3_'],
                 ['static', str(CANDIDATE)+'/../animeimages-candidate.aB12cD34/static'],
                 ['code', str(APP)+'; /usr/bin/id'])
        for args in cases:
            with self.subTest(args=args):
                self.assert_denied(sudo([HELPER, *args], input=MANIFEST))

    def test_root_and_wrong_consumer_identity_rejected_by_helper_itself(self):
        for user, group in ((None, None), ('ubuntu', 'ubuntu'), ('www-data', 'root')):
            with self.subTest(user=user, group=group):
                result = command([HELPER, 'code', APP], user=user, group=group,
                                 input=MANIFEST, check=False)
                self.assert_denied(result)
                self.assertIn('must run as www-data:www-data', result.stderr)

    def test_symlink_candidate_root_and_symlink_static_root_rejected(self):
        alias = STAGING/'animeimages-candidate.Z9Y8X7W6'
        alias.symlink_to(CANDIDATE, target_is_directory=True)
        self.addCleanup(alias.unlink)
        self.assert_denied(probe('code', alias))
        self.assert_denied(probe('static', alias/'static', input=''))
        root = STAGING/'animeimages-candidate.X1Y2Z3W4'
        owned(root, directory=True)
        (root/'static').symlink_to(CANDIDATE/'static', target_is_directory=True)
        self.addCleanup(shutil.rmtree, root)
        self.assert_denied(probe('static', root/'static', input=''))

    def test_empty_and_escaping_tracked_manifests_rejected(self):
        for manifest in ('', '\0', '../animeimages/.env.production\0',
                         '/etc/passwd\0', 'missing.py\0'):
            with self.subTest(manifest=manifest):
                self.assert_denied(probe('code', APP, input=manifest))

    def test_unreadable_code_static_and_private_secrets(self):
        for mode, root, path in (('code', APP, APP/'djangoProject/__init__.py'),
                                 ('static', APP/'staticfiles', APP/'staticfiles/probe.css')):
            with self.subTest(mode=mode):
                path.chmod(0o600)
                try:
                    result = probe(mode, root)
                    self.assert_denied(result)
                    self.assertIn('Permission denied', result.stderr)
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
                finally:
                    path.chmod(0o644)
        for path in (APP/'.env.production', WORK/'private/backup.sql'):
            self.assertNotEqual(command(['/bin/cat', path], user='www-data', check=False).returncode, 0)
        self.assert_denied(probe('code', APP, input='.env.production\0'))

    def test_environment_cannot_inject_or_expose_secrets(self):
        injection = owned(WORK/'injection', directory=True)
        marker = Path('/tmp/managed-release-injected')
        bash_env = owned(injection/'bashenv', 0o644,
                         content=f'/usr/bin/touch {marker}\nexit 89\n')
        payload = f"from pathlib import Path\nPath({str(marker)!r}).touch()\nraise RuntimeError('injected')\n"
        owned(injection/'sitecustomize.py', 0o644, content=payload)
        owned(injection/'djangoProject/__init__.py', 0o644, content=payload)
        hostile = dict(os.environ, **SECRETS, BASH_ENV=str(bash_env),
                       PYTHONPATH=str(injection), PYTHONHOME=str(injection),
                       PATH=str(injection), ENV=str(bash_env))
        for use_sudo in (True, False):
            with self.subTest(use_sudo=use_sudo):
                if use_sudo:
                    result = probe('code', APP, env=hostile)
                else:
                    result = command([HELPER, 'code', APP], user='www-data', group='www-data',
                                     input=MANIFEST, env=hostile, check=False)
                self.assert_pass(result, APP, 'code-and-runtime')
                self.assertFalse(marker.exists())
        result = sudo(['BASH_ENV='+str(bash_env), HELPER, 'code', APP], input=MANIFEST)
        self.assert_denied(result)
        self.assertFalse(marker.exists())

    def test_gate_trust_rejects_nonroot_writable_and_symlink_installations(self):
        metadata = GATE.stat()
        try:
            for owner, mode in ((pwd.getpwnam('ubuntu').pw_uid, 0o644), (0, 0o666)):
                os.chown(GATE, owner, 0)
                GATE.chmod(mode)
                result = probe('static', APP/'staticfiles', input='')
                self.assert_denied(result)
                self.assertIn('installed gate', result.stderr)
        finally:
            os.chown(GATE, metadata.st_uid, metadata.st_gid)
            GATE.chmod(stat.S_IMODE(metadata.st_mode))
        saved = GATE.with_suffix('.saved')
        GATE.rename(saved)
        try:
            GATE.symlink_to(saved)
            result = probe('static', APP/'staticfiles', input='')
            self.assert_denied(result)
            self.assertIn('symlink', result.stderr)
        finally:
            GATE.unlink()
            saved.rename(GATE)


def git(repo, *args):
    return command(['git', '-C', repo, *args], user='ubuntu').stdout.strip()


def init_repo(repo):
    git(repo, 'init', '-b', 'main')
    git(repo, 'config', 'user.email', 'fixture@example.invalid')
    git(repo, 'config', 'user.name', 'Managed release fixture')


class DeployFixture(unittest.TestCase):
    def setUp(self):
        for path in APP.iterdir():
            if path.name != '.venv':
                if path.is_dir() and not path.is_symlink():
                    shutil.rmtree(path)
                else:
                    path.unlink()
        for path in (TOOL, WORK/'origin.git', STATE, WRAPPER_STATE):
            if path.exists():
                shutil.rmtree(path)
        for path in STAGING.iterdir():
            if path.is_symlink():
                path.unlink()
            elif path.is_dir():
                shutil.rmtree(path)
        application(APP)
        owned(APP/'staticfiles/probe.css', 0o644, content='previous public static')
        owned(APP/'.gitignore', 0o644, content='.venv/\n.env.production\nstaticfiles/\n__pycache__/\n')
        init_repo(APP)
        git(APP, 'add', '.')
        git(APP, 'commit', '-m', 'synthetic before')
        self.before = git(APP, 'rev-parse', 'HEAD')
        owned(APP/'new_probe.py', 0o644, content='VALUE = 43\n')
        git(APP, 'add', '.')
        git(APP, 'commit', '-m', 'synthetic target')
        self.target = git(APP, 'rev-parse', 'HEAD')
        owned(WORK/'origin.git', directory=True)
        command(['git', 'clone', '--bare', APP, WORK/'origin.git'], user='ubuntu',
                env=dict(os.environ, GIT_CONFIG_COUNT='1',
                         GIT_CONFIG_KEY_0='safe.directory', GIT_CONFIG_VALUE_0=str(APP)))
        git(APP, 'remote', 'add', 'origin', WORK/'origin.git')
        git(APP, 'reset', '--hard', self.before)
        owned(TOOL, directory=True)
        for name in ('deploy.sh', 'pre_deploy_check.sh', 'check_deploy_readability.py',
                     'restart.sh', 'health_check.sh', 'managed_release.sh'):
            owned(TOOL/'scripts'/name, 0o644, content=(SOURCE/'scripts'/name).read_text())
        for name in ('animeimages-consumer-probe', 'animeimages-managed-release.sudoers'):
            owned(TOOL/'ops/managed-release'/name, 0o644,
                  content=(SOURCE/'ops/managed-release'/name).read_text())
        init_repo(TOOL)
        git(TOOL, 'add', '.')
        git(TOOL, 'commit', '-m', 'reviewed synthetic tooling')
        self.tool_sha = git(TOOL, 'rev-parse', 'HEAD')
        owned(STATE, 0o700, directory=True)
        self.write_env()
        EVENTS.write_text('')
        control()

    def write_env(self, **extra):
        values = dict(SECRETS, FIXTURE_EVENTS=str(EVENTS), PIP_NO_INDEX='1',
                      PIP_DISABLE_PIP_VERSION_CHECK='1', RUN_HEALTH_CHECK='0')
        values.update(extra)
        owned(APP/'.env.production', 0o600,
              content=''.join(f'{key}={value}\n' for key, value in values.items()))

    def run_deploy(self, **overrides):
        values = dict(PATH='/usr/local/bin:/usr/bin:/bin', HOME='/home/ubuntu',
                      APP_DIR=str(APP), ENV_FILE=str(APP/'.env.production'),
                      PYTHON_BIN=str(APP/'.venv/bin/python'), SERVICE_NAME='animeimages',
                      DEPLOY_USER='ubuntu', DEPLOY_GROUP='ubuntu', DEPLOY_BRANCH='main',
                      WEB_USER='www-data', STATIC_USER='www-data', STATIC_GROUP='www-data',
                      CODE_UMASK='0022', DEPLOY_STATE_DIR=str(STATE),
                      DEPLOY_STAGE_PARENT=str(STAGING), DEPLOY_CONSUMER_PROBE_MODE='managed',
                      DEPLOY_TOOL_SHA=self.tool_sha, DEPLOY_TARGET_SHA=self.target,
                      RUN_HEALTH_CHECK='0', PYTHONDONTWRITEBYTECODE='1')
        values.update(overrides)
        return command(['/bin/bash', '-c', 'umask 077; exec /bin/bash "$1"',
                        'fixture', TOOL/'scripts/deploy.sh'], user='ubuntu', env=values, check=False)

    def run_wrapper(self, *args):
        return command(['/bin/bash', TOOL/'scripts/managed_release.sh', *args],
                       user='ubuntu', env={'PATH': '/usr/local/bin:/usr/bin:/bin',
                                           'HOME': '/home/ubuntu'}, check=False)

    def assert_success(self, result):
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def assert_stopped_before_switch(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(git(APP, 'rev-parse', 'HEAD'), self.before)
        self.assertEqual(service_actions(), [])
        self.assertFalse(any(event['kind'] == 'manage' for event in events()))

    def assert_no_restart(self, result):
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertFalse(any(args[0] == 'restart' for args in service_actions()))

    def assert_real_probes(self, result):
        records = [json.loads(line) for line in result.stdout.splitlines() if line.startswith('{"status"')]
        self.assertTrue(records, result.stdout + result.stderr)
        for record in records:
            self.assertEqual(record['status'], 'PASS')
            self.assertEqual(record['uid'], pwd.getpwnam('www-data').pw_uid)
            self.assertEqual(record['gid'], grp.getgrnam('www-data').gr_gid)
        return records


class ManagedDeployAcceptance(DeployFixture):
    def test_prepare_only_real_candidate_leaves_live_and_service_untouched(self):
        control(state='active')
        result = self.run_deploy(DEPLOY_PREPARE_ONLY='1')
        self.assert_success(result)
        records = self.assert_real_probes(result)
        self.assertEqual([record['scope'] for record in records],
                         ['code-and-runtime', 'static', 'code-and-runtime'])
        self.assertTrue(records[0]['root'].startswith(str(STAGING)+'/animeimages-candidate.'))
        self.assertEqual(records[-1]['root'], str(APP))
        self.assertEqual(git(APP, 'rev-parse', 'HEAD'), self.before)
        self.assertEqual(service_actions(), [])
        self.assertFalse(any(event['kind'] == 'manage' for event in events()))
        self.assertEqual(list(STAGING.iterdir()), [])
        record, = STATE.glob('*/recovery.txt')
        self.assertEqual(stat.S_IMODE(record.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(record.parent.stat().st_mode), 0o700)

    def test_official_managed_deploy_with_real_migrations_static_and_sudo(self):
        result = self.run_deploy()
        self.assert_success(result)
        records = self.assert_real_probes(result)
        self.assertIn(str(APP/'staticfiles'), [record['root'] for record in records])
        self.assertEqual(git(APP, 'rev-parse', 'HEAD'), self.target)
        self.assertTrue((APP/'staticfiles/probe.css').is_file())
        self.assertEqual(service_actions(), [['daemon-reload'], ['restart', 'animeimages'],
                                             ['status', 'animeimages', '--no-pager', '-l']])
        manage = [event for event in events() if event['kind'] == 'manage']
        self.assertEqual([event['args'][0] for event in manage], ['migrate', 'collectstatic'])
        self.assertTrue(all(event['uid'] == pwd.getpwnam('ubuntu').pw_uid for event in manage))

    def test_managed_profile_rejects_overrides_before_switch(self):
        for overrides in ({'CODE_UMASK': '0027'}, {'DEPLOY_STAGE_PARENT': '/tmp'},
                          {'ENV_FILE': str(WORK/'other.env')}, {'DEPLOY_BRANCH': 'other'},
                          {'PYTHON_BIN': '/opt/permission-runtime/bin/python'},
                          {'STATIC_GROUP': 'root'}):
            with self.subTest(overrides=overrides):
                result = self.run_deploy(**overrides)
                self.assert_stopped_before_switch(result)
                self.assertIn('fixed production profile', result.stderr)

    def test_installed_helper_and_gate_byte_drift_rejected(self):
        for path in (HELPER, GATE):
            with self.subTest(path=path):
                original = path.read_bytes()
                try:
                    path.write_bytes(original + b'\n# installed drift\n')
                    result = self.run_deploy(DEPLOY_PREPARE_ONLY='1')
                    self.assert_stopped_before_switch(result)
                    self.assertIn('differs from reviewed tooling', result.stderr)
                finally:
                    path.write_bytes(original)

    def test_existing_unreadable_code_fails_before_switch(self):
        (APP/'djangoProject/__init__.py').chmod(0o600)
        result = self.run_deploy()
        self.assert_stopped_before_switch(result)
        self.assertIn('Permission denied', result.stderr)

    def test_unreadable_collected_static_never_restarts(self):
        self.write_env(FIXTURE_BREAK_STATIC='1')
        result = self.run_deploy()
        self.assert_no_restart(result)
        self.assertEqual(git(APP, 'rev-parse', 'HEAD'), self.target)
        self.assertIn('Permission denied', result.stderr)

    def test_nonallowlisted_static_root_never_restarts(self):
        owned(WORK/'unexpected-static', directory=True)
        self.write_env(FIXTURE_STATIC_ROOT=str(WORK/'unexpected-static'))
        result = self.run_deploy()
        self.assert_no_restart(result)
        self.assertIn('root or mode is not allowed', result.stderr)

    def test_loaded_environment_cannot_change_managed_mode(self):
        self.write_env(DEPLOY_CONSUMER_PROBE_MODE='direct')
        result = self.run_deploy()
        self.assert_no_restart(result)
        self.assertIn('readonly variable', result.stderr)

    def test_loaded_environment_cannot_override_pinned_target(self):
        self.write_env(DEPLOY_TARGET_SHA=self.before)
        result = self.run_deploy()
        self.assert_no_restart(result)
        self.assertEqual(git(APP, 'rev-parse', 'HEAD'), self.target)
        self.assertIn('DEPLOY_TARGET_SHA: readonly variable', result.stderr)
        self.assertFalse(any(event['kind'] == 'manage' for event in events()))


class ManagedWrapperAcceptance(DeployFixture):
    def test_apply_requires_exact_maintenance_flag_before_any_action(self):
        for args in (('apply', self.target), ('apply', self.target, '--yes'),
                     ('prepare', self.target, '--maintenance-approved'),
                     ('verify', self.target, '--maintenance-approved')):
            with self.subTest(args=args):
                self.assert_stopped_before_switch(self.run_wrapper(*args))
                self.assertEqual(events(), [])

    def test_prepare_never_stops_active_service(self):
        control(state='active')
        result = self.run_wrapper('prepare', self.target)
        self.assert_success(result)
        self.assert_real_probes(result)
        self.assertEqual(service_actions(), [])
        self.assertEqual(git(APP, 'rev-parse', 'HEAD'), self.before)

    def test_apply_failed_prepare_never_stops_service(self):
        control(state='active')
        (APP/'djangoProject/__init__.py').chmod(0o600)
        self.assert_stopped_before_switch(self.run_wrapper('apply', self.target, '--maintenance-approved'))

    def test_apply_exact_stop_then_official_deploy_then_verify(self):
        control(state='active')
        result = self.run_wrapper('apply', self.target, '--maintenance-approved')
        self.assert_success(result)
        self.assert_real_probes(result)
        self.assertEqual(git(APP, 'rev-parse', 'HEAD'), self.target)
        self.assertEqual(service_actions(), [['stop', 'animeimages'], ['daemon-reload'],
                                             ['restart', 'animeimages'],
                                             ['status', 'animeimages', '--no-pager', '-l'],
                                             ['status', 'animeimages', '--no-pager', '-l']])
        audit = events()
        stop = next(i for i, event in enumerate(audit)
                    if event['kind'] == 'systemctl' and event['args'][0] == 'stop')
        migrate = next(i for i, event in enumerate(audit) if event['kind'] == 'manage')
        self.assertLess(stop, migrate)
        self.assertEqual([event['args'][-1] for event in audit[:stop] if event['kind'] == 'curl'],
                         ['http://127.0.0.1:8000/health/ready/',
                          'https://download.cosflow.icu/health/ready/'])
        for event in audit:
            if event['kind'] == 'curl':
                self.assertEqual(event['args'][:7],
                                 ['-q', '--proxy', '', '--noproxy', '*', '--max-redirs', '0'])
        self.assertIn('[managed-release] verified target='+self.target, result.stdout)

    def test_verify_respects_channel_lock_before_service_or_readiness(self):
        owned(WRAPPER_STATE, 0o700, directory=True)
        lock = owned(WRAPPER_STATE/'channel.lock', 0o600)
        with lock.open('r+') as stream:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
            result = self.run_wrapper('verify', self.before)
        self.assert_stopped_before_switch(result)
        self.assertIn('another managed release is running', result.stderr)
        self.assertEqual(events(), [])

    def test_cancelled_apply_kills_term_resistant_deploy_and_descendant(self):
        control(state='active')
        pid_file = owned(WORK/'blocked-pids', 0o600)
        release = WORK/'release-blocked-deploy'
        continued = owned(WORK/'continued-after-cancel', 0o600)
        self.write_env(FIXTURE_PAUSE_MIGRATE='1', FIXTURE_BLOCK_PID=str(pid_file),
                       FIXTURE_RELEASE=str(release), FIXTURE_CONTINUED=str(continued))
        identity = pwd.getpwnam('ubuntu')
        process = subprocess.Popen(
            ['/bin/bash', str(TOOL/'scripts/managed_release.sh'), 'apply', self.target,
             '--maintenance-approved'], user=identity.pw_uid, group=identity.pw_gid,
            extra_groups=os.getgrouplist('ubuntu', identity.pw_gid),
            env={'PATH': '/usr/local/bin:/usr/bin:/bin', 'HOME': '/home/ubuntu'},
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
            start_new_session=True)
        child_pids = []
        try:
            deadline = time.monotonic() + 40
            while not pid_file.read_text() and process.poll() is None and time.monotonic() < deadline:
                time.sleep(0.05)
            if not pid_file.read_text():
                process.kill()
                stdout, stderr = process.communicate(timeout=10)
                self.fail('deploy did not reach blocked migrate:\n'+stdout+stderr)
            child_pids = list(map(int, pid_file.read_text().split()))
            self.assertEqual(git(APP, 'rev-parse', 'HEAD'), self.target)
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=15)
            self.assertEqual(process.returncode, 143, stdout + stderr)
            self.assertIn('interrupted; actual service state:', stderr)
            self.assertIn('ActiveState=inactive', stderr)
            release.touch()
            time.sleep(0.3)
            for pid in child_pids:
                status = Path(f'/proc/{pid}/stat')
                if status.exists():
                    self.assertEqual(status.read_text().split(') ', 1)[1].split()[0], 'Z',
                                     f'child {pid} survived cancellation')
            self.assertEqual(continued.read_text(), '')
            self.assertEqual(service_actions(), [['stop', 'animeimages']])
            self.assertEqual([event['args'][0] for event in events() if event['kind'] == 'manage'],
                             ['migrate'])
            self.assertNotIn('deployment finished', stdout)
        finally:
            if process.poll() is None:
                process.kill()
            for pid in child_pids:
                try:
                    os.kill(pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            process.communicate(timeout=10)

    def test_verify_nonzero_status_fails_before_readiness(self):
        control(state='active', status_code=4)
        result = self.run_wrapper('verify', self.before)
        self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(service_actions(), [['status', 'animeimages', '--no-pager', '-l']])
        self.assertFalse(any(event['kind'] == 'curl' for event in events()))

    def test_verify_local_and_public_readiness_require_exact_200(self):
        for label in ('local', 'public'):
            for code in (301, 503):
                with self.subTest(label=label, code=code):
                    EVENTS.write_text('')
                    control(state='active', **{label+'_code': code})
                    result = self.run_wrapper('verify', self.before)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(label+' readiness must return HTTP 200', result.stderr)
                    self.assertNotIn('[managed-release] verified', result.stdout)
                    self.assertFalse(any(args[0] in ('stop', 'restart') for args in service_actions()))

    def test_failed_public_preapply_readiness_never_stops(self):
        control(state='active', public_code=503)
        result = self.run_wrapper('apply', self.target, '--maintenance-approved')
        self.assert_stopped_before_switch(result)
        self.assertIn('public readiness must return HTTP 200', result.stderr)


def setUpModule():
    setup_container()


if __name__ == '__main__':
    unittest.main(verbosity=2)
