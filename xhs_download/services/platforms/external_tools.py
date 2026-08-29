import logging
from hashlib import sha256
import os
from pathlib import Path
import signal
import shlex
import shutil
import subprocess
import tempfile
import time

from django.conf import settings
from django.core.cache import cache


logger = logging.getLogger(__name__)


class ExternalCommandOutputTooLarge(RuntimeError):
    pass


EXTERNAL_PROCESS_POLL_INTERVAL_SECONDS = 0.02
EXTERNAL_PROCESS_TERM_GRACE_SECONDS = 0.5


def safe_subprocess_env():
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": os.environ.get("HOME", ""),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "PYTHONNOUSERSITE": "1",
        "YTDLP_NO_PLUGINS": "1",
    }


def split_external_command(binary, default_command):
    return shlex.split(str(binary or "").strip()) or [default_command]


def resolve_external_command_for_cwd(args):
    args = list(args or ())
    if not args:
        return args
    executable = args[0]
    if os.path.isabs(executable):
        return args
    if os.sep in executable and os.path.exists(executable):
        args[0] = os.path.abspath(executable)
    return args


def external_command_output_max_bytes():
    value = getattr(
        settings,
        "THIRD_PARTY_EXTERNAL_OUTPUT_MAX_BYTES",
        8 * 1024 * 1024,
    )
    return max(
        1024,
        int(value or 0),
    )


def read_external_output(file_obj, *, max_bytes):
    file_obj.seek(0, os.SEEK_END)
    size = file_obj.tell()
    if size > max_bytes:
        raise ExternalCommandOutputTooLarge(
            f"external command output exceeded {max_bytes} bytes"
        )
    file_obj.seek(0)
    return file_obj.read(max_bytes).decode("utf-8", errors="replace")


def directory_size(path):
    total = 0
    try:
        for item in Path(path).rglob("*"):
            if item.is_file():
                try:
                    total += item.stat().st_size
                except OSError:
                    continue
    except OSError:
        return 0
    return total


def emit_external_progress(progress_callback, progress_path, estimated_size):
    if not progress_callback or not progress_path or not estimated_size:
        return
    current_size = directory_size(progress_path)
    if current_size <= 0:
        return
    progress_callback(current_size, estimated_size)


def _external_output_size(file_obj):
    return os.fstat(file_obj.fileno()).st_size


def _sanitized_external_executable_name(args):
    name = os.path.basename(str(args[0])) if args else ""
    sanitized = "".join(
        character
        for character in name
        if character.isalnum() or character in {".", "_", "-"}
    )
    return sanitized[:80] or "external-tool"


def _enforce_external_output_limit(
    stdout_file,
    stderr_file,
    *,
    max_stdout_bytes,
    max_stderr_bytes,
):
    if (
        _external_output_size(stdout_file) > max_stdout_bytes
        or _external_output_size(stderr_file) > max_stderr_bytes
    ):
        raise ExternalCommandOutputTooLarge(
            "external command output exceeded its configured byte limit"
        )


def _process_group_exists(process_group_id):
    if os.name != "posix" or not process_group_id:
        return False
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _wait_for_external_process_exit(process, process_group_id, timeout_seconds):
    deadline = time.monotonic() + max(0.0, float(timeout_seconds or 0.0))
    while True:
        process.poll()
        if os.name == "posix":
            if not _process_group_exists(process_group_id):
                return True
        elif process.returncode is not None:
            return True
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        time.sleep(min(EXTERNAL_PROCESS_POLL_INTERVAL_SECONDS, remaining))


def _signal_external_process(process, process_group_id, *, force=False):
    if os.name == "posix":
        try:
            os.killpg(
                process_group_id,
                signal.SIGKILL if force else signal.SIGTERM,
            )
        except ProcessLookupError:
            return
        return
    if process.poll() is not None:
        return
    if force:
        process.kill()
    else:
        process.terminate()


def _reap_external_process(process, timeout_seconds):
    try:
        process.wait(timeout=max(0.0, float(timeout_seconds or 0.0)))
    except subprocess.TimeoutExpired:
        if process.poll() is None:
            process.kill()
            process.wait()


def _cleanup_external_process(process, process_group_id, *, executable_name):
    cleanup_error_types = []

    def process_is_alive():
        process.poll()
        if os.name == "posix":
            return _process_group_exists(process_group_id)
        return process.returncode is None

    try:
        group_alive = process_is_alive()
    except Exception as error:
        cleanup_error_types.append(type(error).__name__)
        group_alive = True

    if group_alive:
        try:
            _signal_external_process(process, process_group_id)
        except Exception as error:
            cleanup_error_types.append(type(error).__name__)
        try:
            group_alive = not _wait_for_external_process_exit(
                process,
                process_group_id,
                EXTERNAL_PROCESS_TERM_GRACE_SECONDS,
            )
        except Exception as error:
            cleanup_error_types.append(type(error).__name__)
            group_alive = True

    if group_alive:
        try:
            _signal_external_process(process, process_group_id, force=True)
        except Exception as error:
            cleanup_error_types.append(type(error).__name__)
        try:
            _wait_for_external_process_exit(
                process,
                process_group_id,
                EXTERNAL_PROCESS_TERM_GRACE_SECONDS,
            )
        except Exception as error:
            cleanup_error_types.append(type(error).__name__)

    try:
        _reap_external_process(process, EXTERNAL_PROCESS_TERM_GRACE_SECONDS)
    except Exception as error:
        cleanup_error_types.append(type(error).__name__)

    try:
        group_alive = process_is_alive()
    except Exception as error:
        cleanup_error_types.append(type(error).__name__)
        group_alive = True
    if cleanup_error_types or group_alive:
        logger.warning(
            "external_command_cleanup_incomplete",
            extra={
                "executable": executable_name,
                "cleanup_error_type": cleanup_error_types[0]
                if cleanup_error_types
                else "",
                "process_group_alive": bool(group_alive),
            },
        )


def _execute_external_process(
    args,
    *,
    stdout,
    stderr,
    stdin,
    cwd,
    env,
    timeout_seconds,
    max_stdout_bytes,
    max_stderr_bytes,
    progress_callback=None,
    progress_path=None,
    estimated_size=0,
    progress_interval_seconds=0.25,
):
    popen_kwargs = {
        "stdout": stdout,
        "stderr": stderr,
        "stdin": stdin,
        "cwd": cwd,
        "env": env,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True

    process = None
    process_group_id = None
    executable_name = _sanitized_external_executable_name(args)
    try:
        process = subprocess.Popen(args, **popen_kwargs)
        if os.name == "posix":
            process_group_id = process.pid
        started_at = time.monotonic()
        next_progress_at = started_at
        progress_interval_seconds = max(
            0.1,
            float(progress_interval_seconds or 0.25),
        )

        while process.poll() is None:
            _enforce_external_output_limit(
                stdout,
                stderr,
                max_stdout_bytes=max_stdout_bytes,
                max_stderr_bytes=max_stderr_bytes,
            )
            now = time.monotonic()
            elapsed = now - started_at
            if elapsed >= timeout_seconds:
                raise subprocess.TimeoutExpired(args, timeout_seconds)
            if (
                progress_callback
                and progress_path
                and estimated_size
                and now >= next_progress_at
            ):
                emit_external_progress(
                    progress_callback,
                    progress_path,
                    estimated_size,
                )
                next_progress_at = now + progress_interval_seconds

            wait_seconds = min(
                EXTERNAL_PROCESS_POLL_INTERVAL_SECONDS,
                timeout_seconds - elapsed,
            )
            try:
                process.wait(timeout=max(0.001, wait_seconds))
            except subprocess.TimeoutExpired:
                pass

        _enforce_external_output_limit(
            stdout,
            stderr,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
        )
        if progress_callback and progress_path and estimated_size:
            emit_external_progress(progress_callback, progress_path, estimated_size)
        completed = subprocess.CompletedProcess(args, process.returncode)
    except BaseException:
        if process is not None:
            _cleanup_external_process(
                process,
                process_group_id,
                executable_name=executable_name,
            )
        raise

    _cleanup_external_process(
        process,
        process_group_id,
        executable_name=executable_name,
    )
    return completed


def run_external_command(
    args,
    *,
    timeout_seconds,
    input_text=None,
    max_stdout_bytes=None,
    max_stderr_bytes=None,
    progress_callback=None,
    progress_path=None,
    estimated_size=0,
    progress_interval_seconds=0.25,
):
    args = resolve_external_command_for_cwd(args)
    with (
        tempfile.TemporaryDirectory(prefix="xhs_external_tool_") as work_dir,
        tempfile.TemporaryFile() as stdin_file,
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        env = safe_subprocess_env()
        env["HOME"] = work_dir
        timeout_seconds = max(0.1, float(timeout_seconds or 15.0))
        default_max_bytes = external_command_output_max_bytes()
        max_stdout_bytes = max(
            1024,
            int(max_stdout_bytes or default_max_bytes),
        )
        max_stderr_bytes = max(
            1024,
            int(max_stderr_bytes or default_max_bytes),
        )
        if input_text is not None:
            stdin_file.write(str(input_text).encode("utf-8"))
            stdin_file.seek(0)
        completed = _execute_external_process(
            args,
            stdout=stdout_file,
            stderr=stderr_file,
            stdin=stdin_file,
            cwd=work_dir,
            env=env,
            timeout_seconds=timeout_seconds,
            max_stdout_bytes=max_stdout_bytes,
            max_stderr_bytes=max_stderr_bytes,
            progress_callback=progress_callback,
            progress_path=progress_path,
            estimated_size=estimated_size,
            progress_interval_seconds=progress_interval_seconds,
        )
        completed.stdout = read_external_output(
            stdout_file,
            max_bytes=max_stdout_bytes,
        )
        completed.stderr = read_external_output(
            stderr_file,
            max_bytes=max_stderr_bytes,
        )
        return completed


def effective_external_timeout_seconds(
    provider_timeout,
    *,
    frontend_setting="THIRD_PARTY_FRONTEND_TIMEOUT_SECONDS",
    default_provider_timeout=15.0,
):
    provider_timeout = max(
        0.1,
        float(
            provider_timeout
            if provider_timeout is not None
            else default_provider_timeout
        ),
    )
    frontend_timeout = max(
        0.1,
        float(getattr(settings, frontend_setting, 3.0) or 3.0),
    )
    return min(provider_timeout, frontend_timeout)


def external_command_health(binary, default_command):
    configured = bool(str(binary or "").strip())
    try:
        command = split_external_command(binary, default_command)
    except ValueError as error:
        return {
            "binary": str(binary or "").strip() or default_command,
            "binary_configured": configured,
            "command": (),
            "executable": "",
            "resolved_executable": "",
            "executable_found": False,
            "error": f"invalid command: {error}",
        }

    executable = command[0] if command else default_command
    if os.path.isabs(executable):
        resolved = (
            executable
            if os.path.exists(executable) and os.access(executable, os.X_OK)
            else ""
        )
    elif os.sep in executable:
        candidate = os.path.abspath(executable)
        resolved = (
            candidate
            if os.path.exists(candidate) and os.access(candidate, os.X_OK)
            else ""
        )
    else:
        resolved = shutil.which(executable) or ""

    return {
        "binary": str(binary or "").strip() or default_command,
        "binary_configured": configured,
        "command": tuple(command),
        "executable": executable,
        "resolved_executable": resolved,
        "executable_found": bool(resolved),
        "error": "" if resolved else f"executable not found: {executable}",
    }


def external_version_probe_cache_key(command, version_args=("--version",)):
    digest = sha256(
        repr((tuple(command or ()), tuple(version_args or ())))
        .encode("utf-8")
    ).hexdigest()[:24]
    return f"third-party:external-command:version:{digest}"


def probe_external_command_version(
    command,
    timeout_seconds=3.0,
    cache_seconds=60,
    version_args=("--version",),
):
    command = tuple(command or ())
    if not command:
        return {
            "version_probe_ok": False,
            "version_output": "",
            "error": "empty command",
        }
    version_args = tuple(version_args or ("--version",))
    cache_seconds = int(cache_seconds or 0)
    cache_key = external_version_probe_cache_key(command, version_args)
    if cache_seconds > 0:
        try:
            cached = cache.get(cache_key)
            if isinstance(cached, dict):
                return cached
        except Exception as error:
            logger.debug(
                "external_provider_version_cache_read_failed",
                extra={"command": command[:2], "error_type": type(error).__name__},
                exc_info=True,
            )

    try:
        completed = run_external_command(
            [*command, *version_args],
            timeout_seconds=max(0.5, float(timeout_seconds or 3.0)),
        )
    except subprocess.TimeoutExpired:
        return {
            "version_probe_ok": False,
            "version_output": "",
            "error": "version probe timed out",
        }
    except ExternalCommandOutputTooLarge as error:
        return {
            "version_probe_ok": False,
            "version_output": "",
            "error": str(error),
        }
    except OSError as error:
        return {
            "version_probe_ok": False,
            "version_output": "",
            "error": str(error),
        }

    output = (completed.stdout or completed.stderr or "").strip()
    output = output.splitlines()[0][:200] if output else ""
    result = {
        "version_probe_ok": completed.returncode == 0,
        "version_output": output,
        "error": "" if completed.returncode == 0 else f"version probe failed: {completed.returncode}",
    }
    if cache_seconds > 0:
        try:
            cache.set(cache_key, result, timeout=cache_seconds)
        except Exception as error:
            logger.debug(
                "external_provider_version_cache_write_failed",
                extra={"command": command[:2], "error_type": type(error).__name__},
                exc_info=True,
            )
    return result


def external_payload_cache_key(provider_id, source_key):
    return f"third-party:{provider_id}:payload:{source_key}"


def external_failure_cache_key(provider_id, source_key):
    return f"third-party:{provider_id}:failure:{source_key}"


def get_cached_external_payload(provider_id, source_key):
    try:
        payload = cache.get(external_payload_cache_key(provider_id, source_key))
    except Exception:
        logger.exception(
            "external_provider_payload_cache_read_failed",
            extra={"provider": provider_id, "source_key": source_key},
        )
        return None
    return payload if isinstance(payload, (dict, list, tuple)) else None


def cache_external_payload(provider_id, source_key, payload, ttl_seconds):
    ttl_seconds = int(ttl_seconds or 0)
    if ttl_seconds <= 0 or not isinstance(payload, (dict, list, tuple)):
        return
    try:
        cache.set(
            external_payload_cache_key(provider_id, source_key),
            payload,
            timeout=ttl_seconds,
        )
    except Exception:
        logger.exception(
            "external_provider_payload_cache_write_failed",
            extra={"provider": provider_id, "source_key": source_key},
        )


def clear_cached_external_payload(provider_id, source_key):
    try:
        cache.delete(external_payload_cache_key(provider_id, source_key))
    except Exception:
        logger.exception(
            "external_provider_payload_cache_delete_failed",
            extra={"provider": provider_id, "source_key": source_key},
        )


def get_cached_external_failure(provider_id, source_key):
    try:
        return str(cache.get(external_failure_cache_key(provider_id, source_key)) or "")
    except Exception:
        logger.exception(
            "external_provider_failure_cache_read_failed",
            extra={"provider": provider_id, "source_key": source_key},
        )
        return ""


def cache_external_failure(provider_id, source_key, reason, ttl_seconds):
    ttl_seconds = int(ttl_seconds or 0)
    if ttl_seconds <= 0:
        return
    try:
        cache.set(
            external_failure_cache_key(provider_id, source_key),
            str(reason or "failed")[:120],
            timeout=ttl_seconds,
        )
    except Exception:
        logger.exception(
            "external_provider_failure_cache_write_failed",
            extra={"provider": provider_id, "source_key": source_key},
        )


def clear_cached_external_failure(provider_id, source_key):
    try:
        cache.delete(external_failure_cache_key(provider_id, source_key))
    except Exception:
        logger.exception(
            "external_provider_failure_cache_delete_failed",
            extra={"provider": provider_id, "source_key": source_key},
        )
