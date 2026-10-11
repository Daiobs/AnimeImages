#!/usr/bin/python3 -I
"""One-time root installer; run only from an independently reviewed, frozen bundle."""

import fcntl
import json
import os
import secrets
import signal
import stat
import subprocess
import sys
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class Paths:
    bundle: Path = Path(__file__).absolute().parent
    probe: Path = Path("/usr/local/sbin/animeimages-consumer-probe")
    gate: Path = Path("/usr/local/libexec/animeimages-release/check_deploy_readability.py")
    sudoers: Path = Path("/etc/sudoers.d/animeimages-managed-release")
    sudoers_main: Path = Path("/etc/sudoers")
    backups: Path = Path("/root/sudoers-backups")
    lock: Path = Path("/root/.animeimages-managed-release-install.lock")
    interpreter: Path = Path(sys.executable)
    visudo: Path = Path("/usr/sbin/visudo")


DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
FILE_FLAGS = os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
ASSETS = (
    ("animeimages-consumer-probe", "probe", 0o755),
    ("check_deploy_readability.py", "gate", 0o644),
    ("animeimages-managed-release.sudoers", "sudoers", 0o440),
)


class _SignalGuard:
    def __init__(self):
        self.pending, self.depth, self.aborting = None, 0, False
        self.previous = {}

    def __enter__(self):
        try:
            for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                self.previous[number] = signal.signal(number, self._handle)
        except BaseException:
            self.__exit__(None, None, None)
            raise
        return self

    def __exit__(self, *unused):
        self.aborting = True
        for number, handler in self.previous.items():
            signal.signal(number, handler)

    def _handle(self, number, frame):
        self.pending = self.pending or number
        self._check()

    def _check(self):
        if self.pending and not self.depth and not self.aborting:
            # Further handled signals cannot interrupt the ensuing rollback.
            self.aborting = True
            raise InstallError(f"interrupted by {signal.Signals(self.pending).name}")

    @contextmanager
    def defer(self):
        self.depth += 1
        try:
            yield
        finally:
            self.depth -= 1
            self._check()


def _sudoers_include(path):
    value = str(_absolute(path))
    if any(character in value for character in '\n\r"\\'):
        raise InstallError(f"unsupported sudoers include path: {path}")
    return f'#include "{value}"\n'.encode()


def _secure(info, label, *, directory=False):
    kind = stat.S_ISDIR if directory else stat.S_ISREG
    if not kind(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
        raise InstallError(f"unsafe root-controlled {'directory' if directory else 'file'}: {label}")
    if not directory and (info.st_nlink != 1 or info.st_mode & 0o7000):
        raise InstallError(f"unexpected linked or special-mode file: {label}")


def _identity(info):
    return info.st_dev, info.st_ino


def _absolute(path):
    path = Path(path)
    if not path.is_absolute() or ".." in path.parts:
        raise InstallError(f"expected an absolute path without '..': {path}")
    return path


@contextmanager
def _directory(path, *, create=(), created=None, mode=0o755):
    path = _absolute(path)
    fd = os.open("/", DIR_FLAGS)
    try:
        _secure(os.fstat(fd), "/", directory=True)
        current = Path("/")
        for name in path.parts[1:]:
            current /= name
            try:
                child = os.open(name, DIR_FLAGS, dir_fd=fd)
            except FileNotFoundError:
                if current not in create:
                    raise
                os.mkdir(name, mode, dir_fd=fd)
                child = os.open(name, DIR_FLAGS, dir_fd=fd)
                if created is not None:
                    created.append((os.dup(fd), name, _identity(os.fstat(child))))
                try:
                    os.fchown(child, 0, 0)
                    os.fchmod(child, mode)
                except BaseException:
                    os.close(child)
                    raise
            try:
                _secure(os.fstat(child), current, directory=True)
            except BaseException:
                os.close(child)
                raise
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


def _read(parent, name, *, missing=False):
    try:
        before = os.stat(name, dir_fd=parent, follow_symlinks=False)
    except FileNotFoundError:
        if missing:
            return None
        raise
    _secure(before, name)
    fd = os.open(name, os.O_RDONLY | FILE_FLAGS, dir_fd=parent)
    with os.fdopen(fd, "rb") as stream:
        info = os.fstat(stream.fileno())
        _secure(info, name)
        if _identity(before) != _identity(info):
            raise InstallError(f"file changed while opening: {name}")
        data = stream.read()
        after = os.fstat(stream.fileno())
        if (info.st_size, info.st_mtime_ns, info.st_ctime_ns) != (
            after.st_size, after.st_mtime_ns, after.st_ctime_ns
        ):
            raise InstallError(f"file changed while reading: {name}")
        return data, info


def _bundle(fd):
    # Reject unsafe extra entries too: the whole bootstrap bundle must be frozen.
    with os.scandir(fd) as entries:
        for entry in entries:
            info = os.stat(entry.name, dir_fd=fd, follow_symlinks=False)
            if stat.S_ISDIR(info.st_mode):
                child = os.open(entry.name, DIR_FLAGS, dir_fd=fd)
                try:
                    _secure(os.fstat(child), entry.name, directory=True)
                    _bundle(child)
                finally:
                    os.close(child)
            else:
                _read(fd, entry.name)


def _executable(path):
    # System python3 commonly is a symlink; validate each link and its parent.
    path = _absolute(path)
    for _ in range(40):
        with _directory(path.parent) as parent:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            if stat.S_ISLNK(info.st_mode):
                if info.st_uid != 0:
                    raise InstallError(f"untrusted executable link: {path}")
                link = Path(os.readlink(path.name, dir_fd=parent))
                path = _absolute(link if link.is_absolute() else path.parent / link)
                continue
            _, info = _read(parent, path.name)
            if not info.st_mode & 0o111:
                raise InstallError(f"not executable: {path}")
            return path
    raise InstallError("too many executable symlinks")


@contextmanager
def _lock(path):
    path = _absolute(path)
    with _directory(path.parent) as parent:
        try:
            fd = os.open(path.name, os.O_RDWR | os.O_CREAT | os.O_EXCL | FILE_FLAGS,
                         0o600, dir_fd=parent)
        except FileExistsError:
            info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
            _secure(info, path)
            fd = os.open(path.name, os.O_RDWR | FILE_FLAGS, dir_fd=parent)
        try:
            # Creation is already private under the installer's restrictive umask.
            info = os.fstat(fd)
            _secure(info, path)
            if stat.S_IMODE(info.st_mode) != 0o600:
                raise InstallError(f"lock must have mode 0600: {path}")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as error:
                raise InstallError("another managed-release installer holds the lock") from error
            if _identity(info) != _identity(os.stat(path.name, dir_fd=parent, follow_symlinks=False)):
                raise InstallError("lock path changed while acquiring it")
            yield
        finally:
            # Never unlink the lock: a second inode would allow concurrent owners.
            os.close(fd)


def _write(parent, name, data, mode):
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | FILE_FLAGS,
                 0o600, dir_fd=parent)
    with os.fdopen(fd, "wb") as stream:
        identity = _identity(os.fstat(stream.fileno()))
        try:
            os.fchown(stream.fileno(), 0, 0)
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), mode)
            os.fsync(stream.fileno())
            return identity
        except BaseException:
            _remove(parent, name, identity)
            raise


def _remove(parent, name, identity, *, directory=False):
    info = os.stat(name, dir_fd=parent, follow_symlinks=False)
    if _identity(info) != identity:
        raise InstallError(f"refusing to remove changed {'directory' if directory else 'file'}: {name}")
    (os.rmdir if directory else os.unlink)(name, dir_fd=parent)


def execute(command):
    subprocess.run(command, check=True, stdin=subprocess.DEVNULL, cwd="/",
                   env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"})


def install(paths=None, executor=execute):
    """Python API permits fixture paths/executor; the CLI accepts no overrides."""
    if os.geteuid() != 0:
        raise InstallError("installer must run as root")
    paths = paths or Paths()
    _executable(paths.interpreter)
    visudo = str(_executable(paths.visudo))
    old_umask = os.umask(0o077)
    directories, created, stages = [], [], []
    try:
        with ExitStack() as stack:
            signals = stack.enter_context(_SignalGuard())
            stack.enter_context(_lock(paths.lock))
            source = stack.enter_context(_directory(paths.bundle))
            _bundle(source)
            _read(source, "install.py")
            assets = [(name, _absolute(getattr(paths, field)), mode, _read(source, name)[0])
                      for name, field, mode in ASSETS]
            executor([visudo, "-c"])
            try:
                targets = []
                for name, target, mode, data in assets:
                    allowed = (paths.gate.parent.parent, paths.gate.parent) if target == paths.gate else ()
                    parent = stack.enter_context(_directory(target.parent, create=allowed, created=directories))
                    previous = _read(parent, target.name, missing=True)
                    if previous is not None and (
                        previous[0] != data or stat.S_IMODE(previous[1].st_mode) != mode
                        or previous[1].st_gid != 0
                    ):
                        raise InstallError(f"refusing to replace existing target: {target}")
                    targets.append((name, target, mode, data, parent, previous))

                backup_base = stack.enter_context(_directory(paths.backups, create=(paths.backups,), mode=0o700))
                stamp = datetime.now(timezone.utc).strftime("managed-release-%Y%m%dT%H%M%S.%fZ-")
                backup = paths.backups / (stamp + secrets.token_hex(8))
                os.mkdir(backup.name, 0o700, dir_fd=backup_base)
                backup_fd = stack.enter_context(_directory(backup))
                os.fchown(backup_fd, 0, 0)
                os.fchmod(backup_fd, 0o700)
                state = {}
                for name, target, mode, data, parent, previous in targets:
                    state[str(target)] = {"existed": previous is not None,
                                          "mode": stat.S_IMODE(previous[1].st_mode) if previous else None}
                    if previous is not None:
                        _write(backup_fd, name, previous[0], 0o600)
                _write(backup_fd, "state.json", (json.dumps(state, indent=2) + "\n").encode(), 0o600)

                for name, target, mode, data, parent, previous in targets:
                    temporary = "." + target.name + "." + secrets.token_hex(8) + ".tmp"
                    with signals.defer():
                        identity = _write(parent, temporary, data, mode)
                        stages.append((parent, temporary, identity))
                    if name == "animeimages-managed-release.sudoers":
                        staged_sudoers = target.parent / temporary
                        sudoers_missing = previous is None
                executor([visudo, "-cf", str(staged_sudoers)])
                if sudoers_missing:
                    main = _absolute(paths.sudoers_main)
                    with _directory(main.parent) as parent:
                        _read(parent, main.name)
                    # Includedir ignores the dotted staging name; include it once explicitly.
                    combined = _sudoers_include(main) + _sudoers_include(staged_sudoers)
                    _write(backup_fd, "combined.sudoers", combined, 0o600)
                    executor([visudo, "-cf", str(backup / "combined.sudoers")])
                for target_info, stage in zip(targets, stages):
                    _, target, _, _, parent, previous = target_info
                    if previous is None:
                        with signals.defer():
                            os.link(stage[1], target.name, src_dir_fd=parent, dst_dir_fd=parent,
                                    follow_symlinks=False)
                            created.append((parent, target.name, stage[2]))
                        os.fsync(parent)
                while stages:
                    with signals.defer():
                        _remove(*stages[-1])
                        stages.pop()
                executor([visudo, "-c"])
                return backup
            except BaseException as error:
                signals.aborting = True
                failures = []
                for item in reversed(created + stages):
                    try:
                        _remove(*item)
                    except Exception as cleanup_error:
                        failures.append(str(cleanup_error))
                for parent, name, identity in reversed(directories):
                    try:
                        _remove(parent, name, identity, directory=True)
                    except Exception as cleanup_error:
                        failures.append(str(cleanup_error))
                try:
                    executor([visudo, "-c"])
                except Exception as check_error:
                    failures.append(f"rollback visudo failed: {check_error}")
                if failures:
                    raise InstallError(f"{error}; rollback incomplete: {'; '.join(failures)}") from error
                raise
    finally:
        for parent, _, _ in directories:
            os.close(parent)
        os.umask(old_umask)


def main(argv=None):
    if (sys.argv[1:] if argv is None else argv):
        raise InstallError("usage: /usr/bin/python3 -I /root/reviewed-bundle/install.py (no arguments)")
    if not sys.flags.isolated or sys.prefix != sys.base_prefix:
        raise InstallError("use system python3 -I, not an application virtualenv")
    print(f"Managed-release files installed and sudoers validated. Backup: {install()}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (InstallError, OSError, subprocess.CalledProcessError) as error:
        print(f"managed-release installer: {error}", file=sys.stderr)
        raise SystemExit(1)
