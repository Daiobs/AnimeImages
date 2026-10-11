"""Unprivileged filesystem tests; only root identity and chown are simulated."""

import importlib.util
import json
import os
import signal
import stat
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


MODULE_PATH = Path(__file__).absolute().parents[1] / "install.py"
SPEC = importlib.util.spec_from_file_location("managed_release_installer", MODULE_PATH)
installer = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = installer
SPEC.loader.exec_module(installer)

CONTENTS = {
    "install.py": b"# Independently reviewed installer fixture.\n",
    "animeimages-consumer-probe": b"#!/bin/bash -p\nexit 0\n",
    "check_deploy_readability.py": b"# Trusted gate fixture; never executed by the installer.\n",
    "animeimages-managed-release.sudoers": b"# Reviewed sudoers fixture.\n",
}


class InstallerTests(unittest.TestCase):
    def setUp(self):
        # Avoid /tmp (world-writable) and macOS's /var symlink in path-validation tests.
        temporary = tempfile.TemporaryDirectory(dir=Path.home())
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bundle = self.root / "bundle"
        for name in ("bundle", "usr/local/sbin", "etc/sudoers.d", "root", "usr/bin", "usr/sbin"):
            (self.root / name).mkdir(parents=True, exist_ok=True, mode=0o755)
        (self.root / "root").chmod(0o700)
        for name, content in CONTENTS.items():
            (self.bundle / name).write_bytes(content)
            (self.bundle / name).chmod(0o644)
        for name in ("usr/bin/python3", "usr/sbin/visudo"):
            (self.root / name).write_bytes(b"#!/bin/sh\nexit 99\n")
            (self.root / name).chmod(0o755)
        self.paths = installer.Paths(
            bundle=self.bundle,
            probe=self.root / "usr/local/sbin/animeimages-consumer-probe",
            gate=self.root / "usr/local/libexec/animeimages-release/check_deploy_readability.py",
            sudoers=self.root / "etc/sudoers.d/animeimages-managed-release",
            sudoers_main=self.root / "etc/sudoers",
            backups=self.root / "root/sudoers-backups",
            lock=self.root / "root/install.lock",
            interpreter=self.root / "usr/bin/python3",
            visudo=self.root / "usr/sbin/visudo",
        )
        self.paths.sudoers_main.write_bytes(
            b"Defaults env_reset\n" + installer._sudoers_include(self.paths.sudoers.parent).replace(
                b"#include ", b"#includedir ", 1)
        )
        self.paths.sudoers_main.chmod(0o440)
        self.calls = []
        self.owners, self.groups, self.modes, self.chowned = {}, {}, {}, {}
        self.real_stat, self.real_fstat = os.stat, os.fstat

        def metadata(info):
            values = {name: getattr(info, name) for name in dir(info) if name.startswith("st_")}
            key = info.st_dev, info.st_ino
            values["st_uid"] = self.owners.get(key, 0)
            values["st_gid"] = self.groups.get(key, 0)
            values["st_mode"] = self.modes.get(key, info.st_mode)
            return SimpleNamespace(**values)

        def chown(fd, uid, gid):
            info = self.real_fstat(fd)
            key = info.st_dev, info.st_ino
            self.owners[key], self.groups[key] = uid, gid
            self.chowned[key] = uid, gid

        for patch in (
            mock.patch.object(installer.os, "geteuid", return_value=0),
            mock.patch.object(installer.os, "fchown", side_effect=chown),
            mock.patch.object(installer.os, "stat", side_effect=lambda *a, **kw: metadata(self.real_stat(*a, **kw))),
            mock.patch.object(installer.os, "fstat", side_effect=lambda fd: metadata(self.real_fstat(fd))),
        ):
            patch.start()
            self.addCleanup(patch.stop)

    def mark_owner(self, path, uid=1000):
        info = self.real_stat(path, follow_symlinks=False)
        self.owners[info.st_dev, info.st_ino] = uid

    def execute(self, command):
        self.calls.append(command)
        self.assertEqual(command[0], str(self.paths.visudo))
        if command[1] == "-cf":
            staged = Path(command[2])
            if staged.name == "combined.sudoers":
                self.assertEqual(staged.parent.parent, self.paths.backups)
                self.assertEqual(stat.S_IMODE(staged.parent.stat().st_mode), 0o700)
                self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o600)
                self.assertEqual((os.stat(staged).st_uid, os.stat(staged).st_gid), (0, 0))
                self.assertEqual(staged.read_bytes(), installer._sudoers_include(self.paths.sudoers_main)
                                 + installer._sudoers_include(self.staged_sudoers))
                self.assertTrue(self.staged_sudoers.exists())
                self.assertFalse(self.paths.sudoers.exists())
            else:
                self.staged_sudoers = staged
                self.assertEqual(staged.parent, self.paths.sudoers.parent)
                self.assertTrue(staged.name.startswith("."))
                self.assertEqual(staged.read_bytes(), (self.bundle / "animeimages-managed-release.sudoers").read_bytes())
                self.assertEqual(stat.S_IMODE(staged.stat().st_mode), 0o440)

    def run_install(self, executor=None):
        return installer.install(self.paths, executor or self.execute)

    def targets(self):
        return [getattr(self.paths, field) for _, field, _ in installer.ASSETS]

    def existing(self, field):
        name, _, mode = next(asset for asset in installer.ASSETS if asset[1] == field)
        target = getattr(self.paths, field)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(CONTENTS[name])
        target.chmod(mode)
        return target

    def assert_no_targets_or_stages(self):
        for target in self.targets():
            self.assertFalse(target.exists(), str(target))
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_root_required(self):
        with mock.patch.object(installer.os, "geteuid", return_value=1000):
            with self.assertRaisesRegex(installer.InstallError, "root"):
                self.run_install()
        self.assertEqual(self.calls, [])

    def test_first_install_modes_backup_absence_and_visudo_order(self):
        backup = self.run_install()
        self.assertEqual([call[1] for call in self.calls], ["-c", "-cf", "-cf", "-c"])
        for name, field, mode in installer.ASSETS:
            target = getattr(self.paths, field)
            self.assertEqual(target.read_bytes(), CONTENTS[name])
            self.assertEqual(stat.S_IMODE(target.stat().st_mode), mode)
            self.assertEqual(os.stat(target).st_uid, 0)
            self.assertEqual(os.stat(target).st_gid, 0)
            self.assertEqual(self.chowned[installer._identity(os.stat(target))], (0, 0))
            self.assertEqual(target.stat().st_nlink, 1)
        for directory in (self.paths.gate.parent.parent, self.paths.gate.parent):
            self.assertEqual(stat.S_IMODE(directory.stat().st_mode), 0o755)
        self.assertEqual(backup.parent, self.paths.backups)
        self.assertRegex(backup.name, r"^managed-release-\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{16}$")
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o700)
        state = json.loads((backup / "state.json").read_text())
        self.assertEqual(state, {str(target): {"existed": False, "mode": None} for target in self.targets()})
        self.assertEqual(stat.S_IMODE((backup / "state.json").stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.paths.lock.stat().st_mode), 0o600)
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_exact_rerun_does_not_replace_existing_files_or_lock(self):
        first = self.run_install()
        inodes = {p: p.stat().st_ino for p in self.targets() + [self.paths.lock]}
        second = self.run_install()
        self.assertEqual([call[1] for call in self.calls[4:]], ["-c", "-cf", "-c"])
        self.assertFalse((second / "combined.sudoers").exists())
        self.assertNotEqual(first, second)
        self.assertEqual(inodes, {p: p.stat().st_ino for p in inodes})
        for target in self.targets():
            self.assertEqual((os.stat(target).st_uid, os.stat(target).st_gid), (0, 0))
        state = json.loads((second / "state.json").read_text())
        self.assertTrue(all(item["existed"] for item in state.values()))
        for name, _, _ in installer.ASSETS:
            self.assertEqual((second / name).read_bytes(), CONTENTS[name])
            self.assertEqual(stat.S_IMODE((second / name).stat().st_mode), 0o600)

    def test_exact_rerun_rejects_nonroot_group_without_changing_targets(self):
        self.run_install()
        inodes = {target: target.stat().st_ino for target in self.targets()}
        for name, field, _ in installer.ASSETS:
            target = getattr(self.paths, field)
            with self.subTest(target=target):
                key = installer._identity(os.stat(target))
                self.groups[key] = 1234
                with self.assertRaisesRegex(installer.InstallError, "refusing to replace existing target"):
                    self.run_install()
                self.assertEqual(os.stat(target).st_gid, 1234)
                self.assertEqual(target.read_bytes(), CONTENTS[name])
                self.assertEqual(inodes, {path: path.stat().st_ino for path in inodes})
                self.groups[key] = 0

    def test_source_owners_including_ancestors_and_installer_are_checked(self):
        for path in [self.root, self.bundle, *[self.bundle / name for name in CONTENTS]]:
            with self.subTest(path=path):
                self.mark_owner(path)
                with self.assertRaises(installer.InstallError):
                    self.run_install()
                self.owners.clear()
        self.assertEqual(self.calls, [])

    def test_source_writable_files_directories_and_extra_entries_are_rejected(self):
        extra = self.bundle / "extra"
        extra.mkdir(mode=0o755)
        (extra / "unrelated").write_bytes(b"not executed")
        for path in [self.bundle, self.bundle / "install.py", extra, extra / "unrelated"]:
            original = stat.S_IMODE(path.stat().st_mode)
            for bit in (0o020, 0o002):
                with self.subTest(path=path, bit=bit):
                    path.chmod(original | bit)
                    with self.assertRaises(installer.InstallError):
                        self.run_install()
                    path.chmod(original)
        self.assertEqual(self.calls, [])

    def test_source_symlink_file_and_directory_and_special_file_are_rejected(self):
        for name, destination in (("file-link", self.bundle / "install.py"), ("dir-link", self.root)):
            link = self.bundle / name
            link.symlink_to(destination)
            with self.assertRaises(installer.InstallError):
                self.run_install()
            link.unlink()
        fifo = self.bundle / "fifo"
        os.mkfifo(fifo, 0o600)
        with self.assertRaises(installer.InstallError):
            self.run_install()
        self.assertEqual(self.calls, [])

    def test_source_bundle_and_ancestor_symlinks_are_rejected(self):
        alias = self.root / "alias"
        alias.symlink_to(self.bundle)
        for bundle in (alias, alias / "child"):
            with self.assertRaises(OSError):
                installer.install(replace(self.paths, bundle=bundle), self.execute)
        self.assertEqual(self.calls, [])

    def test_missing_asset_or_installer_is_rejected_before_visudo(self):
        for name in CONTENTS:
            path = self.bundle / name
            path.unlink()
            with self.assertRaises(FileNotFoundError):
                self.run_install()
            path.write_bytes(CONTENTS[name])
            path.chmod(0o644)
        self.assertEqual(self.calls, [])

    def test_root_component_itself_is_validated(self):
        self.mark_owner(Path("/"))
        with self.assertRaises(installer.InstallError):
            self.run_install()
        self.owners.clear()
        info = self.real_stat("/")
        self.modes[info.st_dev, info.st_ino] = info.st_mode | 0o002
        with self.assertRaises(installer.InstallError):
            self.run_install()
        self.assertFalse(self.paths.lock.exists())

    def test_untrusted_interpreter_or_visudo_is_not_executed(self):
        for path in (self.paths.interpreter, self.paths.visudo):
            for kind in ("owner", "writable", "not-executable"):
                with self.subTest(path=path, kind=kind):
                    self.mark_owner(path, 1000 if kind == "owner" else 0)
                    path.chmod({"owner": 0o755, "writable": 0o777, "not-executable": 0o644}[kind])
                    with self.assertRaises(installer.InstallError):
                        self.run_install()
                    path.chmod(0o755)
                    self.owners.clear()
        self.assertEqual(self.calls, [])
        self.assertFalse(self.paths.lock.exists())

    def test_root_controlled_system_interpreter_symlink_is_allowed(self):
        executable = self.paths.interpreter.with_name("python3.real")
        self.paths.interpreter.rename(executable)
        self.paths.interpreter.symlink_to(executable.name)
        self.run_install()
        self.mark_owner(self.paths.interpreter)
        with self.assertRaisesRegex(installer.InstallError, "executable link"):
            self.run_install()

    def test_destination_ancestors_require_safe_owner_and_modes(self):
        self.paths.gate.parent.mkdir(parents=True)
        for path in (self.paths.probe.parent, self.paths.probe.parent.parent,
                     self.paths.gate.parent.parent, self.paths.gate.parent,
                     self.paths.sudoers.parent, self.paths.sudoers.parent.parent):
            with self.subTest(path=path):
                self.mark_owner(path)
                with self.assertRaises(installer.InstallError):
                    self.run_install()
                self.owners.clear()
                path.chmod(0o777)
                with self.assertRaises(installer.InstallError):
                    self.run_install()
                path.chmod(0o755)
        self.assert_no_targets_or_stages()

    def test_destination_symlink_directory_is_rejected_and_never_altered(self):
        real = self.root / "real-sbin"
        self.paths.probe.parent.rename(real)
        self.paths.probe.parent.symlink_to(real)
        with self.assertRaises(OSError):
            self.run_install()
        self.assertEqual(list(real.iterdir()), [])
        self.assertTrue(self.paths.probe.parent.is_symlink())

    def test_existing_directory_modes_are_never_changed(self):
        self.paths.gate.parent.mkdir(parents=True)
        self.paths.gate.parent.parent.chmod(0o711)
        self.paths.gate.parent.chmod(0o750)
        for directory in (self.root, self.paths.probe.parent, self.paths.gate.parent.parent,
                          self.paths.gate.parent, self.paths.sudoers.parent):
            self.groups[installer._identity(os.stat(directory))] = 1234
        self.run_install()
        self.assertEqual(stat.S_IMODE(self.paths.gate.parent.parent.stat().st_mode), 0o711)
        self.assertEqual(stat.S_IMODE(self.paths.gate.parent.stat().st_mode), 0o750)

    def test_different_bytes_or_mode_or_owner_refuses_existing_target(self):
        for field in ("probe", "gate", "sudoers"):
            target = self.existing(field)
            original, original_mode = target.read_bytes(), stat.S_IMODE(target.stat().st_mode)
            for kind in ("bytes", "mode", "owner"):
                with self.subTest(field=field, kind=kind):
                    target.chmod(0o600)
                    target.write_bytes(b"conflicting" if kind == "bytes" else original)
                    target.chmod(0o600 if kind == "mode" else original_mode)
                    self.mark_owner(target, 1000 if kind == "owner" else 0)
                    inode = target.stat().st_ino
                    with self.assertRaises(installer.InstallError):
                        self.run_install()
                    self.assertEqual(target.stat().st_ino, inode)
                    self.assertEqual(target.read_bytes(), b"conflicting" if kind == "bytes" else original)
                    self.owners.clear()
            target.unlink()

    def test_unexpected_symlink_directory_or_hardlink_target_is_not_replaced(self):
        target = self.paths.probe
        target.symlink_to(self.bundle / "animeimages-consumer-probe")
        with self.assertRaises(installer.InstallError):
            self.run_install()
        self.assertTrue(target.is_symlink())
        target.unlink()
        target.mkdir()
        with self.assertRaises(installer.InstallError):
            self.run_install()
        self.assertTrue(target.is_dir())
        target.rmdir()
        other = self.root / "hardlink"
        other.write_bytes(CONTENTS["animeimages-consumer-probe"])
        other.chmod(0o755)
        os.link(other, target)
        with self.assertRaises(installer.InstallError):
            self.run_install()
        self.assertEqual(target.stat().st_nlink, 2)

    def test_lock_rejects_symlink_wrong_mode_owner_and_hardlinks(self):
        lock = self.paths.lock
        lock.symlink_to(self.bundle / "install.py")
        with self.assertRaises(installer.InstallError):
            self.run_install()
        lock.unlink()
        for kind in ("mode", "owner", "hardlink"):
            with self.subTest(kind=kind):
                lock.write_bytes(b"lock contents must not be truncated")
                lock.chmod(0o644 if kind == "mode" else 0o600)
                self.mark_owner(lock, 1000 if kind == "owner" else 0)
                linked = self.root / "other-lock"
                if kind == "hardlink":
                    os.link(lock, linked)
                with self.assertRaises(installer.InstallError):
                    self.run_install()
                self.assertEqual(lock.read_bytes(), b"lock contents must not be truncated")
                lock.unlink()
                if linked.exists():
                    linked.unlink()
                self.owners.clear()
        self.assertEqual(self.calls, [])

    def test_lock_contention_refuses_concurrent_install(self):
        with installer._lock(self.paths.lock):
            with self.assertRaisesRegex(installer.InstallError, "holds the lock"):
                self.run_install()
        self.assertEqual(self.calls, [])

    def test_backup_and_lock_parents_are_root_controlled(self):
        self.paths.backups.mkdir()
        for path in (self.paths.backups, self.paths.lock.parent):
            self.mark_owner(path)
            with self.assertRaises(installer.InstallError):
                self.run_install()
            self.owners.clear()
            previous = stat.S_IMODE(path.stat().st_mode)
            path.chmod(previous | 0o002)
            with self.assertRaises(installer.InstallError):
                self.run_install()
            path.chmod(previous)
        self.assert_no_targets_or_stages()

    def test_preflight_visudo_failure_changes_no_targets_or_backup(self):
        def fail(command):
            self.execute(command)
            raise subprocess.CalledProcessError(1, command)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_install(fail)
        self.assertEqual([call[1] for call in self.calls], ["-c"])
        self.assertFalse(self.paths.backups.exists())
        self.assertFalse(self.paths.gate.parent.parent.exists())
        self.assert_no_targets_or_stages()

    def test_staged_visudo_failure_rolls_back_and_rechecks_full_policy(self):
        def fail(command):
            self.execute(command)
            if command[1] == "-cf":
                raise subprocess.CalledProcessError(1, command)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_install(fail)
        self.assertEqual([call[1] for call in self.calls], ["-c", "-cf", "-c"])
        self.assertFalse(self.paths.gate.parent.parent.exists())
        self.assert_no_targets_or_stages()
        self.assertEqual(len(list(self.paths.backups.iterdir())), 1)

    def test_combined_duplicate_alias_is_rejected_before_any_publication(self):
        alias = b"Cmnd_Alias DUPLICATE = /usr/bin/true\n"
        self.paths.sudoers_main.chmod(0o600)
        self.paths.sudoers_main.write_bytes(alias + self.paths.sudoers_main.read_bytes())
        self.paths.sudoers_main.chmod(0o440)
        (self.bundle / "animeimages-managed-release.sudoers").write_bytes(alias)

        def reject_duplicate(command):
            self.execute(command)
            if command[1] == "-cf" and Path(command[2]).name == "combined.sudoers":
                self.assertIn(alias, self.paths.sudoers_main.read_bytes())
                self.assertIn(alias, self.staged_sudoers.read_bytes())
                self.assertTrue(all(not target.exists() for target in self.targets()))
                raise subprocess.CalledProcessError(1, command, stderr="Alias DUPLICATE already defined")

        with mock.patch.object(installer.os, "link") as publish:
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_install(reject_duplicate)
        publish.assert_not_called()
        self.assertEqual([call[1] for call in self.calls], ["-c", "-cf", "-cf", "-c"])
        self.assert_no_targets_or_stages()
        self.assertFalse(self.paths.gate.parent.parent.exists())

    def test_combined_parse_requires_root_controlled_main_sudoers(self):
        main = self.paths.sudoers_main
        for kind in ("owner", "mode", "symlink"):
            with self.subTest(kind=kind):
                self.mark_owner(main, 1000 if kind == "owner" else 0)
                main.chmod(0o666 if kind == "mode" else 0o440)
                if kind == "symlink":
                    main.unlink()
                    main.symlink_to(self.bundle / "animeimages-managed-release.sudoers")
                with mock.patch.object(installer.os, "link") as publish:
                    with self.assertRaises(installer.InstallError):
                        self.run_install()
                publish.assert_not_called()
                self.assert_no_targets_or_stages()
                if kind == "symlink":
                    main.unlink()
                self.owners.clear()

    def test_post_install_failure_removes_only_new_targets(self):
        existing = self.existing("probe")
        inode = existing.stat().st_ino
        def fail(command):
            self.execute(command)
            if len(self.calls) == 4:
                raise subprocess.CalledProcessError(1, command)
        with self.assertRaises(subprocess.CalledProcessError):
            self.run_install(fail)
        self.assertEqual([call[1] for call in self.calls], ["-c", "-cf", "-cf", "-c", "-c"])
        self.assertEqual(existing.stat().st_ino, inode)
        self.assertEqual(existing.read_bytes(), CONTENTS[existing.name])
        self.assertFalse(self.paths.gate.exists())
        self.assertFalse(self.paths.sudoers.exists())
        self.assertFalse(self.paths.gate.parent.parent.exists())
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_partial_publish_failure_rolls_back_files_and_stages(self):
        real_link = os.link
        count = 0
        def fail(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 2:
                raise OSError("injected publish failure")
            return real_link(*args, **kwargs)
        with mock.patch.object(installer.os, "link", side_effect=fail):
            with self.assertRaisesRegex(OSError, "publish failure"):
                self.run_install()
        self.assert_no_targets_or_stages()
        self.assertEqual([call[1] for call in self.calls], ["-c", "-cf", "-cf", "-c"])

    def test_publish_race_never_overwrites_or_rolls_back_unowned_file(self):
        def race(command):
            self.execute(command)
            if command[1] == "-cf" and Path(command[2]).parent == self.paths.sudoers.parent:
                self.paths.probe.write_bytes(b"created by someone else")
        with self.assertRaises(FileExistsError):
            self.run_install(race)
        self.assertEqual(self.paths.probe.read_bytes(), b"created by someone else")
        self.assertFalse(self.paths.gate.exists())
        self.assertFalse(self.paths.sudoers.exists())
        self.assertEqual(list(self.root.rglob("*.tmp")), [])

    def test_rollback_does_not_delete_replacement_inode(self):
        def replace_and_fail(command):
            self.execute(command)
            if len(self.calls) == 4:
                replacement = self.paths.probe.with_name("replacement")
                replacement.write_bytes(b"another root writer")
                replacement.replace(self.paths.probe)
                raise OSError("injected final check failure")
        with self.assertRaisesRegex(installer.InstallError, "rollback incomplete.*changed file"):
            self.run_install(replace_and_fail)
        self.assertEqual(self.paths.probe.read_bytes(), b"another root writer")
        self.assertFalse(self.paths.gate.exists())
        self.assertFalse(self.paths.sudoers.exists())
        self.assertEqual(self.calls[-1][1], "-c")

    def test_rollback_check_failure_is_reported(self):
        def fail(command):
            self.execute(command)
            if len(self.calls) >= 4:
                raise subprocess.CalledProcessError(1, command)
        with self.assertRaisesRegex(installer.InstallError, "rollback visudo failed"):
            self.run_install(fail)
        self.assert_no_targets_or_stages()

    def test_interruption_rolls_back_new_targets(self):
        def interrupt(command):
            self.execute(command)
            if len(self.calls) == 4:
                raise KeyboardInterrupt()
        with self.assertRaises(KeyboardInterrupt):
            self.run_install(interrupt)
        self.assert_no_targets_or_stages()
        self.assertEqual(self.calls[-1][1], "-c")

    def test_signals_between_link_and_registration_roll_back_published_file(self):
        previous = {number: signal.getsignal(number) for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP)}
        real_link = os.link
        for number in previous:
            with self.subTest(signal=number):
                def link_then_interrupt(*args, **kwargs):
                    result = real_link(*args, **kwargs)
                    signal.raise_signal(number)
                    return result

                with mock.patch.object(installer.os, "link", side_effect=link_then_interrupt):
                    with self.assertRaisesRegex(installer.InstallError, signal.Signals(number).name):
                        self.run_install()
                self.assert_no_targets_or_stages()
                self.assertEqual(self.calls[-1][1], "-c")
                self.assertEqual(previous, {item: signal.getsignal(item) for item in previous})

    def test_term_and_hup_after_sudoers_publication_trigger_rollback(self):
        for number in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signal=number):
                self.calls.clear()

                def interrupt(command):
                    self.execute(command)
                    if len(self.calls) == 4:
                        self.assertTrue(all(target.exists() for target in self.targets()))
                        signal.raise_signal(number)

                with self.assertRaisesRegex(installer.InstallError, signal.Signals(number).name):
                    self.run_install(interrupt)
                self.assert_no_targets_or_stages()
                self.assertEqual([call[1] for call in self.calls], ["-c", "-cf", "-cf", "-c", "-c"])

    def test_further_signals_cannot_interrupt_rollback(self):
        real_link, real_unlink = os.link, os.unlink
        interrupted_cleanup = []

        def link_then_interrupt(*args, **kwargs):
            result = real_link(*args, **kwargs)
            signal.raise_signal(signal.SIGINT)
            return result

        def unlink_then_interrupt(*args, **kwargs):
            result = real_unlink(*args, **kwargs)
            if not interrupted_cleanup:
                interrupted_cleanup.append(True)
                for number in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
                    signal.raise_signal(number)
            return result

        with mock.patch.object(installer.os, "link", side_effect=link_then_interrupt), \
                mock.patch.object(installer.os, "unlink", side_effect=unlink_then_interrupt):
            with self.assertRaisesRegex(installer.InstallError, "SIGINT"):
                self.run_install()
        self.assertTrue(interrupted_cleanup)
        self.assert_no_targets_or_stages()
        self.assertEqual(self.calls[-1][1], "-c")

    def test_staging_write_failure_cleans_partial_file(self):
        real_fsync = os.fsync
        def fail(fd):
            info = self.real_fstat(fd)
            if stat.S_IMODE(info.st_mode) == 0o755:
                raise OSError("injected staging fsync failure")
            return real_fsync(fd)
        with mock.patch.object(installer.os, "fsync", side_effect=fail):
            with self.assertRaisesRegex(OSError, "staging fsync failure"):
                self.run_install()
        self.assert_no_targets_or_stages()

    def test_cli_rejects_path_overrides_nonisolated_python_and_virtualenv(self):
        with mock.patch.object(installer, "install") as install:
            with self.assertRaisesRegex(installer.InstallError, "no arguments"):
                installer.main(["--bundle", str(self.bundle)])
            with mock.patch.object(installer.sys, "flags", SimpleNamespace(isolated=0)):
                with self.assertRaisesRegex(installer.InstallError, "python3 -I"):
                    installer.main([])
            with mock.patch.object(installer.sys, "flags", SimpleNamespace(isolated=1)), \
                    mock.patch.object(installer.sys, "prefix", "/mutable/app/.venv"):
                with self.assertRaisesRegex(installer.InstallError, "virtualenv"):
                    installer.main([])
            install.assert_not_called()

    def test_executor_uses_only_explicit_command_and_clean_environment(self):
        with mock.patch.object(installer.subprocess, "run") as run:
            installer.execute(["/usr/sbin/visudo", "-c"])
        run.assert_called_once_with(
            ["/usr/sbin/visudo", "-c"], check=True, stdin=subprocess.DEVNULL, cwd="/",
            env={"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"},
        )


if __name__ == "__main__":
    unittest.main()
