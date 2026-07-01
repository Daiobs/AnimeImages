import logging
from hashlib import sha256
import os
from pathlib import Path
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


def run_external_command(
    args,
    *,
    timeout_seconds,
    progress_callback=None,
    progress_path=None,
    estimated_size=0,
    progress_interval_seconds=0.25,
):
    args = resolve_external_command_for_cwd(args)
    with (
        tempfile.TemporaryDirectory(prefix="xhs_external_tool_") as work_dir,
        tempfile.TemporaryFile() as stdout_file,
        tempfile.TemporaryFile() as stderr_file,
    ):
        env = safe_subprocess_env()
        env["HOME"] = work_dir
        timeout_seconds = max(0.1, float(timeout_seconds or 15.0))
        if progress_callback and progress_path and estimated_size:
            process = subprocess.Popen(
                args,
                stdout=stdout_file,
                stderr=stderr_file,
                cwd=work_dir,
                env=env,
            )
            started_at = time.monotonic()
            while process.poll() is None:
                if time.monotonic() - started_at > timeout_seconds:
                    process.kill()
                    process.wait()
                    raise subprocess.TimeoutExpired(args, timeout_seconds)
                emit_external_progress(progress_callback, progress_path, estimated_size)
                time.sleep(max(0.1, float(progress_interval_seconds or 0.25)))
            emit_external_progress(progress_callback, progress_path, estimated_size)
            completed = subprocess.CompletedProcess(args, process.returncode)
        else:
            completed = subprocess.run(
                args,
                stdout=stdout_file,
                stderr=stderr_file,
                check=False,
                cwd=work_dir,
                env=env,
                timeout=timeout_seconds,
            )
        max_bytes = external_command_output_max_bytes()
        completed.stdout = read_external_output(stdout_file, max_bytes=max_bytes)
        completed.stderr = read_external_output(stderr_file, max_bytes=max_bytes)
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
