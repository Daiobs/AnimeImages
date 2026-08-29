import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from django.core.cache import cache
from django.test import SimpleTestCase, override_settings

from .services.platforms import external_tools
from .services.platforms.external_tools import (
    ExternalCommandOutputTooLarge,
    probe_external_command_version,
    run_external_command,
)


FAKE_EXTERNAL_TOOL = r"""
import argparse
import os
from pathlib import Path
import signal
import subprocess
import sys
import time


parser = argparse.ArgumentParser(add_help=False)
parser.add_argument("--mode", default="success")
parser.add_argument("--pid-file", default="")
parser.add_argument("--child-pid-file", default="")
parser.add_argument("--grandchild-pid-file", default="")
parser.add_argument("--ready-file", default="")
parser.add_argument("--term-file", default="")
parser.add_argument("--cwd-file", default="")
parser.add_argument("--home-file", default="")
parser.add_argument("--progress-file", default="")
parser.add_argument("--child-mode", default="sleep")
parser.add_argument("--byte-count", type=int, default=4096)
parser.add_argument("--stdout-text", default="")
parser.add_argument("--stderr-text", default="")
parser.add_argument("--exit-code", type=int, default=7)
args, _unknown = parser.parse_known_args()


def write(path, value="ready"):
    if path:
        Path(path).write_text(str(value), encoding="utf-8")


def wait_for(path):
    deadline = time.monotonic() + 5.0
    while path and not Path(path).exists():
        if time.monotonic() >= deadline:
            raise SystemExit(70)
        time.sleep(0.01)


write(args.pid_file, os.getpid())
write(args.cwd_file, os.getcwd())
write(args.home_file, os.environ.get("HOME", ""))


def handle_term(_signum, _frame):
    write(args.term_file, "term")
    if args.mode != "ignore-term":
        raise SystemExit(0)


signal.signal(signal.SIGTERM, handle_term)


def child_command(mode, *, pid_file="", child_pid_file=""):
    command = [
        sys.executable,
        str(Path(__file__).resolve()),
        "--mode",
        mode,
    ]
    if pid_file:
        command.extend(["--pid-file", pid_file])
    if child_pid_file:
        command.extend(["--child-pid-file", child_pid_file])
    if mode in {"write-stdout", "write-stderr"}:
        command.extend(["--byte-count", str(args.byte_count)])
    return command


if args.mode == "success":
    if args.stdout_text:
        sys.stdout.write(args.stdout_text)
    if args.stderr_text:
        sys.stderr.write(args.stderr_text)
    write(args.progress_file, "progress")
    write(args.ready_file)
    raise SystemExit(0)

if args.mode == "nonzero":
    sys.stdout.write(args.stdout_text)
    sys.stderr.write(args.stderr_text)
    write(args.ready_file)
    raise SystemExit(args.exit_code)

if args.mode in {"write-stdout", "write-stderr"}:
    stream = sys.stdout.buffer if args.mode == "write-stdout" else sys.stderr.buffer
    stream.write(b"x" * args.byte_count)
    stream.flush()
    write(args.ready_file)
    while True:
        time.sleep(1)

if args.mode == "spawn-child":
    subprocess.Popen(
        child_command(args.child_mode, pid_file=args.child_pid_file),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    wait_for(args.child_pid_file)
    write(args.progress_file, "progress")
    write(args.ready_file)
    while True:
        time.sleep(1)

if args.mode == "spawn-grandchild":
    command = child_command(
        "spawn-child",
        pid_file=args.child_pid_file,
        child_pid_file=args.grandchild_pid_file,
    )
    command.extend(["--child-mode", "ignore-term"])
    subprocess.Popen(command, stdout=sys.stdout, stderr=sys.stderr)
    wait_for(args.child_pid_file)
    wait_for(args.grandchild_pid_file)
    write(args.ready_file)
    while True:
        time.sleep(1)

if args.mode == "parent-exits-child-lives":
    subprocess.Popen(
        child_command("sleep", pid_file=args.child_pid_file),
        stdout=sys.stdout,
        stderr=sys.stderr,
    )
    wait_for(args.child_pid_file)
    write(args.ready_file)
    raise SystemExit(0)

write(args.ready_file)
while True:
    time.sleep(1)
"""


class ProgressCallbackError(Exception):
    pass


class ProgressCallbackAbort(BaseException):
    pass


class LifecycleMonitorError(Exception):
    pass


@override_settings(THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES=1024)
class ExternalProcessLifecycleTests(SimpleTestCase):
    timeout_seconds = 0.8

    def setUp(self):
        self.temp_dir_context = tempfile.TemporaryDirectory(
            prefix="animeimages_external_lifecycle_test_"
        )
        self.temp_dir = Path(self.temp_dir_context.name)
        self.script_path = self.temp_dir / "fake_external_tool.py"
        self.script_path.write_text(FAKE_EXTERNAL_TOOL, encoding="utf-8")
        self.grace_patch = patch.object(
            external_tools,
            "EXTERNAL_PROCESS_TERM_GRACE_SECONDS",
            0.15,
        )
        self.poll_patch = patch.object(
            external_tools,
            "EXTERNAL_PROCESS_POLL_INTERVAL_SECONDS",
            0.01,
        )
        self.grace_patch.start()
        self.poll_patch.start()

    def tearDown(self):
        self.grace_patch.stop()
        self.poll_patch.stop()
        leaked = self._owned_live_pids()
        for pid in leaked:
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
        for pid in leaked:
            self._wait_for_pid_exit(pid, timeout_seconds=1.0)
        self.temp_dir_context.cleanup()
        self.assertEqual(leaked, [], f"owned subprocesses leaked: {leaked}")

    def marker(self, name):
        return self.temp_dir / name

    def command(self, mode, **markers):
        args = [sys.executable, str(self.script_path), "--mode", mode]
        for name, value in markers.items():
            args.extend([f"--{name.replace('_', '-')}", str(value)])
        return args

    def run_tool(self, mode="success", **kwargs):
        return run_external_command(
            self.command(mode, **kwargs.pop("markers", {})),
            timeout_seconds=kwargs.pop("timeout_seconds", self.timeout_seconds),
            **kwargs,
        )

    def _owned_live_pids(self):
        live = []
        for marker in self.temp_dir.glob("*.pid"):
            try:
                pid = int(marker.read_text(encoding="utf-8").strip())
            except (OSError, ValueError):
                continue
            if self._pid_exists(pid):
                live.append(pid)
        return sorted(set(live))

    @staticmethod
    def _pid_exists(pid):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        return True

    @classmethod
    def _wait_for_pid_exit(cls, pid, timeout_seconds=2.0):
        deadline = time.monotonic() + timeout_seconds
        while cls._pid_exists(pid):
            if time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def assert_marker_pid_stopped(self, marker):
        self.assertTrue(marker.exists(), f"missing PID marker: {marker.name}")
        pid = int(marker.read_text(encoding="utf-8").strip())
        self.assertTrue(self._wait_for_pid_exit(pid), f"PID remained alive: {pid}")

    def test_normal_success(self):
        result = self.run_tool(markers={"stdout_text": "ok"})
        self.assertEqual(result.returncode, 0)
        self.assertEqual(result.stdout, "ok")

    def test_nonzero_exit_is_preserved(self):
        result = self.run_tool(
            "nonzero",
            markers={"exit_code": 9, "stderr_text": "failed"},
        )
        self.assertEqual(result.returncode, 9)
        self.assertEqual(result.stderr, "failed")

    def test_stdout_is_preserved(self):
        result = self.run_tool(markers={"stdout_text": "stdout value"})
        self.assertEqual(result.stdout, "stdout value")

    def test_stderr_is_preserved(self):
        result = self.run_tool(markers={"stderr_text": "stderr value"})
        self.assertEqual(result.stderr, "stderr value")

    def test_stdin_text_is_supplied_without_command_line_exposure(self):
        secret_marker = "opaque-worker-request"
        result = run_external_command(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.write(sys.stdin.read())",
            ],
            timeout_seconds=self.timeout_seconds,
            input_text=secret_marker,
        )

        self.assertEqual(result.stdout, secret_marker)
        self.assertNotIn(secret_marker, " ".join(result.args))

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_timeout_kills_direct_child(self):
        pid_file = self.marker("direct.pid")
        ready_file = self.marker("direct.ready")
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_tool(
                "sleep",
                markers={"pid_file": pid_file, "ready_file": ready_file},
            )
        self.assertTrue(ready_file.exists())
        self.assert_marker_pid_stopped(pid_file)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_timeout_kills_child_and_grandchild(self):
        parent_pid = self.marker("grand-parent.pid")
        child_pid = self.marker("grand-child.pid")
        grandchild_pid = self.marker("grandchild.pid")
        ready_file = self.marker("grand.ready")
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_tool(
                "spawn-grandchild",
                markers={
                    "pid_file": parent_pid,
                    "child_pid_file": child_pid,
                    "grandchild_pid_file": grandchild_pid,
                    "ready_file": ready_file,
                },
            )
        self.assertTrue(ready_file.exists())
        for marker in (parent_pid, child_pid, grandchild_pid):
            self.assert_marker_pid_stopped(marker)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_term_compliant_group_exits_without_kill_escalation(self):
        term_file = self.marker("compliant.term")
        with patch.object(external_tools.os, "killpg", wraps=os.killpg) as killpg:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_tool(
                    "sleep",
                    markers={
                        "pid_file": self.marker("compliant.pid"),
                        "ready_file": self.marker("compliant.ready"),
                        "term_file": term_file,
                    },
                )
        signals = [call.args[1] for call in killpg.call_args_list]
        self.assertTrue(term_file.exists())
        self.assertIn(signal.SIGTERM, signals)
        self.assertNotIn(signal.SIGKILL, signals)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_term_ignoring_group_receives_kill(self):
        term_file = self.marker("ignored.term")
        with patch.object(external_tools.os, "killpg", wraps=os.killpg) as killpg:
            with self.assertRaises(subprocess.TimeoutExpired):
                self.run_tool(
                    "ignore-term",
                    markers={
                        "pid_file": self.marker("ignored.pid"),
                        "ready_file": self.marker("ignored.ready"),
                        "term_file": term_file,
                    },
                )
        signals = [call.args[1] for call in killpg.call_args_list]
        self.assertTrue(term_file.exists())
        self.assertIn(signal.SIGTERM, signals)
        self.assertIn(signal.SIGKILL, signals)

    def test_progress_path_uses_popen_lifecycle(self):
        progress_dir = self.temp_dir / "progress"
        progress_dir.mkdir()
        progress_file = progress_dir / "payload.part"
        callbacks = []
        with patch.object(
            external_tools.subprocess,
            "Popen",
            wraps=subprocess.Popen,
        ) as popen:
            result = self.run_tool(
                markers={"progress_file": progress_file},
                progress_callback=lambda current, total: callbacks.append((current, total)),
                progress_path=progress_dir,
                estimated_size=100,
            )
        self.assertEqual(result.returncode, 0)
        self.assertTrue(callbacks)
        self.assertTrue(popen.called)

    def test_nonprogress_path_uses_popen_lifecycle(self):
        with patch.object(
            external_tools.subprocess,
            "Popen",
            wraps=subprocess.Popen,
        ) as popen:
            result = self.run_tool()
        self.assertEqual(result.returncode, 0)
        self.assertTrue(popen.called)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_progress_callback_error_kills_group(self):
        progress_dir = self.temp_dir / "callback-progress"
        progress_dir.mkdir()
        child_pid = self.marker("callback-child.pid")

        def fail_progress(_current, _total):
            raise ProgressCallbackError("callback failed")

        with self.assertRaisesRegex(ProgressCallbackError, "callback failed"):
            self.run_tool(
                "spawn-child",
                markers={
                    "pid_file": self.marker("callback-parent.pid"),
                    "child_pid_file": child_pid,
                    "ready_file": self.marker("callback.ready"),
                    "progress_file": progress_dir / "payload.part",
                },
                progress_callback=fail_progress,
                progress_path=progress_dir,
                estimated_size=100,
            )
        self.assert_marker_pid_stopped(child_pid)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_progress_callback_base_exception_kills_group(self):
        progress_dir = self.temp_dir / "callback-abort-progress"
        progress_dir.mkdir()
        child_pid = self.marker("callback-abort-child.pid")

        def abort_progress(_current, _total):
            raise ProgressCallbackAbort("callback aborted")

        with self.assertRaisesRegex(ProgressCallbackAbort, "callback aborted"):
            self.run_tool(
                "spawn-child",
                markers={
                    "pid_file": self.marker("callback-abort-parent.pid"),
                    "child_pid_file": child_pid,
                    "ready_file": self.marker("callback-abort.ready"),
                    "progress_file": progress_dir / "payload.part",
                },
                progress_callback=abort_progress,
                progress_path=progress_dir,
                estimated_size=100,
            )
        self.assert_marker_pid_stopped(child_pid)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_monitor_exception_kills_group(self):
        child_pid = self.marker("monitor-child.pid")
        original_enforcer = external_tools._enforce_external_output_limit

        def fail_after_child_starts(*args, **kwargs):
            if child_pid.exists():
                raise LifecycleMonitorError("monitor failed")
            return original_enforcer(*args, **kwargs)

        with patch.object(
            external_tools,
            "_enforce_external_output_limit",
            side_effect=fail_after_child_starts,
        ):
            with self.assertRaisesRegex(LifecycleMonitorError, "monitor failed"):
                self.run_tool(
                    "spawn-child",
                    markers={
                        "pid_file": self.marker("monitor-parent.pid"),
                        "child_pid_file": child_pid,
                        "ready_file": self.marker("monitor.ready"),
                    },
                )
        self.assert_marker_pid_stopped(child_pid)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_cleanup_error_does_not_replace_timeout(self):
        pid_file = self.marker("cleanup-error.pid")
        original_signal = external_tools._signal_external_process

        def fail_term_once(process, process_group_id, *, force=False):
            if not force:
                raise OSError("term failed")
            return original_signal(
                process,
                process_group_id,
                force=True,
            )

        with patch.object(
            external_tools,
            "_signal_external_process",
            side_effect=fail_term_once,
        ):
            with self.assertLogs(external_tools.logger, level="WARNING"):
                with self.assertRaises(subprocess.TimeoutExpired):
                    self.run_tool(
                        "sleep",
                        markers={
                            "pid_file": pid_file,
                            "ready_file": self.marker("cleanup-error.ready"),
                        },
                    )
        self.assert_marker_pid_stopped(pid_file)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_stdout_overflow_kills_group(self):
        child_pid = self.marker("stdout-child.pid")
        with self.assertRaises(ExternalCommandOutputTooLarge):
            self.run_tool(
                "spawn-child",
                markers={
                    "pid_file": self.marker("stdout-parent.pid"),
                    "child_pid_file": child_pid,
                    "child_mode": "write-stdout",
                    "byte_count": 4096,
                    "ready_file": self.marker("stdout.ready"),
                },
            )
        self.assert_marker_pid_stopped(child_pid)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_stderr_overflow_kills_group(self):
        child_pid = self.marker("stderr-child.pid")
        with self.assertRaises(ExternalCommandOutputTooLarge):
            self.run_tool(
                "spawn-child",
                markers={
                    "pid_file": self.marker("stderr-parent.pid"),
                    "child_pid_file": child_pid,
                    "child_mode": "write-stderr",
                    "byte_count": 4096,
                    "ready_file": self.marker("stderr.ready"),
                },
            )
        self.assert_marker_pid_stopped(child_pid)

    @unittest.skipUnless(os.name == "posix", "process groups require POSIX")
    def test_normal_parent_exit_cleans_remaining_child(self):
        child_pid = self.marker("orphan-child.pid")
        result = self.run_tool(
            "parent-exits-child-lives",
            markers={
                "pid_file": self.marker("orphan-parent.pid"),
                "child_pid_file": child_pid,
                "ready_file": self.marker("orphan.ready"),
            },
        )
        self.assertEqual(result.returncode, 0)
        self.assert_marker_pid_stopped(child_pid)

    def test_temporary_home_and_cwd_are_removed_after_success(self):
        cwd_file = self.marker("success.cwd")
        home_file = self.marker("success.home")
        self.run_tool(markers={"cwd_file": cwd_file, "home_file": home_file})
        cwd = Path(cwd_file.read_text(encoding="utf-8"))
        home = Path(home_file.read_text(encoding="utf-8"))
        self.assertEqual(cwd.resolve(strict=False), home.resolve(strict=False))
        self.assertFalse(cwd.exists())
        self.assertFalse(home.exists())

    def test_temporary_home_and_cwd_are_removed_after_timeout(self):
        cwd_file = self.marker("timeout.cwd")
        home_file = self.marker("timeout.home")
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_tool(
                "sleep",
                markers={
                    "cwd_file": cwd_file,
                    "home_file": home_file,
                    "ready_file": self.marker("timeout.ready"),
                },
            )
        cwd = Path(cwd_file.read_text(encoding="utf-8"))
        home = Path(home_file.read_text(encoding="utf-8"))
        self.assertEqual(cwd.resolve(strict=False), home.resolve(strict=False))
        self.assertFalse(cwd.exists())
        self.assertFalse(home.exists())

    def test_temporary_streams_close_after_output_overflow(self):
        real_temporary_file = tempfile.TemporaryFile
        streams = []

        def capture_stream(*args, **kwargs):
            stream = real_temporary_file(*args, **kwargs)
            streams.append(stream)
            return stream

        with patch.object(
            external_tools.tempfile,
            "TemporaryFile",
            side_effect=capture_stream,
        ):
            with self.assertRaises(ExternalCommandOutputTooLarge):
                self.run_tool("write-stdout", markers={"byte_count": 4096})
        self.assertEqual(len(streams), 3)
        self.assertTrue(all(stream.closed for stream in streams))

    def test_version_probe_still_works(self):
        cache.clear()
        result = probe_external_command_version(
            [
                sys.executable,
                str(self.script_path),
                "--mode",
                "success",
                "--stdout-text",
                "tool 1.0\n",
            ],
            cache_seconds=0,
        )
        self.assertTrue(result["version_probe_ok"])
        self.assertEqual(result["version_output"], "tool 1.0")

    def test_missing_executable_behavior_is_preserved(self):
        with self.assertRaises(FileNotFoundError):
            run_external_command(
                [str(self.temp_dir / "missing-executable")],
                timeout_seconds=1,
            )

    def test_completed_process_contract_is_preserved(self):
        result = self.run_tool()
        self.assertIsInstance(result, subprocess.CompletedProcess)
        self.assertIsInstance(result.stdout, str)
        self.assertIsInstance(result.stderr, str)

    def test_argument_list_does_not_invoke_shell(self):
        unexpected_file = self.marker("shell-was-invoked")
        shell_expression = f"$(touch {unexpected_file})"
        result = self.run_tool(markers={"stdout_text": shell_expression})
        self.assertEqual(result.stdout, shell_expression)
        self.assertFalse(unexpected_file.exists())

    def test_cleanup_log_executable_name_is_sanitized(self):
        self.assertEqual(
            external_tools._sanitized_external_executable_name(
                ["/tmp/tool-name\nwith?unsafe%characters"]
            ),
            "tool-namewithunsafecharacters",
        )

    def test_completed_runs_leave_no_owned_processes(self):
        for index in range(3):
            self.run_tool(markers={"pid_file": self.marker(f"complete-{index}.pid")})
        self.assertEqual(self._owned_live_pids(), [])
