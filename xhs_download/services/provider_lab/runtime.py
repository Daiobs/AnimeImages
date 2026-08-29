from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import tempfile
from typing import Any

from xhs_download.services.platforms.external_tools import (
    ExternalCommandOutputTooLarge,
    _execute_external_process,
    read_external_output,
)

from .contracts import (
    MAX_ASSETS,
    MAX_SOURCE_LENGTH,
    ProviderLabError,
    SCHEMA_VERSION,
)


MAX_WORKER_STDOUT_BYTES = 1024 * 1024
MAX_WORKER_STDERR_BYTES = 128 * 1024
ALLOWED_PARENT_ENV = (
    "PATH",
    "LANG",
    "LC_ALL",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)
FORBIDDEN_ENV_MARKERS = (
    "PROXY",
    "COOKIE",
    "AUTHORIZATION",
    "TOKEN",
    "PASSWORD",
    "SECRET",
    "BROWSER",
    "DOWNLOAD",
)


def clean_worker_environment(*, home: str, config_home: str, cache_home: str) -> dict[str, str]:
    env = {
        key: value
        for key in ALLOWED_PARENT_ENV
        if (value := os.environ.get(key))
    }
    env.update(
        {
            "HOME": home,
            "XDG_CONFIG_HOME": config_home,
            "XDG_CACHE_HOME": cache_home,
            "PYTHONNOUSERSITE": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        }
    )
    return env


def environment_is_clean(env: dict[str, str]) -> bool:
    for key in env:
        upper = str(key).upper()
        if key in {"XDG_CONFIG_HOME", "XDG_CACHE_HOME"}:
            continue
        if any(marker in upper for marker in FORBIDDEN_ENV_MARKERS):
            return False
    return True


def parse_worker_stdout(stdout: str) -> dict[str, Any]:
    try:
        payload = json.loads(stdout or "")
    except (TypeError, ValueError) as error:
        raise ProviderLabError("PROVIDER_INVALID_JSON") from error
    if not isinstance(payload, dict) or payload.get("schema_version") != SCHEMA_VERSION:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    error = payload.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        if code in {
            "PROVIDER_NOT_CONFIGURED",
            "PROVIDER_UNSUPPORTED",
            "PROVIDER_TIMEOUT",
            "PROVIDER_RESULT_EMPTY",
            "PROVIDER_LICENSE_RESTRICTED",
        }:
            raise ProviderLabError(code)
        raise ProviderLabError("PROVIDER_PROCESS_FAILED")
    result = payload.get("result")
    resources = payload.get("resources")
    if not isinstance(result, dict) or not isinstance(resources, list):
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    if len(resources) > MAX_ASSETS:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    return payload


def execute_provider_worker(
    *,
    python: str,
    worker: str | Path,
    request_id: str,
    source: str,
    timeout_seconds: float,
) -> dict[str, Any]:
    source = str(source or "")
    if not source or len(source) > MAX_SOURCE_LENGTH:
        raise ProviderLabError("PROVIDER_SCHEMA_INVALID")
    python_path = Path(str(python or "")).expanduser()
    worker_path = Path(worker).resolve()
    if not python_path.is_file() or not worker_path.is_file():
        raise ProviderLabError("PROVIDER_NOT_CONFIGURED")

    request_payload = json.dumps(
        {
            "schema_version": SCHEMA_VERSION,
            "request_id": request_id,
            "source": source,
            "max_assets": MAX_ASSETS,
            "max_output_bytes": MAX_WORKER_STDOUT_BYTES,
        },
        ensure_ascii=True,
        separators=(",", ":"),
    )
    timeout_seconds = min(60.0, max(0.2, float(timeout_seconds or 20.0)))

    try:
        with (
            tempfile.TemporaryDirectory(prefix="animeimages_provider_lab_") as root,
            tempfile.TemporaryFile() as stdin_file,
            tempfile.TemporaryFile() as stdout_file,
            tempfile.TemporaryFile() as stderr_file,
        ):
            root_path = Path(root)
            home = root_path / "home"
            config_home = root_path / "xdg-config"
            cache_home = root_path / "xdg-cache"
            cwd = root_path / "work"
            for directory in (home, config_home, cache_home, cwd):
                directory.mkdir(mode=0o700)
            env = clean_worker_environment(
                home=str(home),
                config_home=str(config_home),
                cache_home=str(cache_home),
            )
            if not environment_is_clean(env):
                raise ProviderLabError("PROVIDER_PROCESS_FAILED")
            stdin_file.write(request_payload.encode("utf-8"))
            stdin_file.seek(0)
            completed = _execute_external_process(
                [str(python_path.absolute()), str(worker_path)],
                stdout=stdout_file,
                stderr=stderr_file,
                stdin=stdin_file,
                cwd=str(cwd),
                env=env,
                timeout_seconds=timeout_seconds,
                max_stdout_bytes=MAX_WORKER_STDOUT_BYTES,
                max_stderr_bytes=MAX_WORKER_STDERR_BYTES,
            )
            stdout = read_external_output(
                stdout_file,
                max_bytes=MAX_WORKER_STDOUT_BYTES,
            )
            read_external_output(
                stderr_file,
                max_bytes=MAX_WORKER_STDERR_BYTES,
            )
            payload = parse_worker_stdout(stdout)
            if completed.returncode != 0:
                raise ProviderLabError("PROVIDER_PROCESS_FAILED")
            return payload
    except subprocess.TimeoutExpired as error:
        raise ProviderLabError("PROVIDER_TIMEOUT") from error
    except ExternalCommandOutputTooLarge as error:
        raise ProviderLabError("PROVIDER_OUTPUT_TOO_LARGE") from error
    except FileNotFoundError as error:
        raise ProviderLabError("PROVIDER_NOT_CONFIGURED") from error
