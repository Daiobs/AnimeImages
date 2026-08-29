#!/usr/bin/env python3
"""Create or verify the isolated, exact-pin ParseHub provider environment."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


PARSEHUB_PIN = "144260f4349c5bb57415f7555a572fcd48c3b8ba"
PARSEHUB_GIT_URL = "https://github.com/z-mio/ParseHub.git"
WORKER = Path(__file__).with_name("parsehub_xhs_image_worker.py")


def _venv_python(venv: Path) -> Path:
    return venv / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def _run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def _verify(venv: Path) -> None:
    python = _venv_python(venv)
    if not python.is_file():
        raise SystemExit("PARSEHUB_PROVIDER_PYTHON_MISSING")
    completed = subprocess.run(
        [str(python), str(WORKER), "--check-only"],
        check=False,
        capture_output=True,
        text=True,
        timeout=20,
    )
    try:
        payload = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise SystemExit("PARSEHUB_PROVIDER_CHECK_INVALID") from error
    expected_version = f"parsehub@{PARSEHUB_PIN}"
    if (
        completed.returncode != 0
        or payload.get("status") != "ok"
        or payload.get("provider_version") != expected_version
    ):
        raise SystemExit("PARSEHUB_PROVIDER_PIN_MISMATCH")
    print(f"PARSEHUB_PROVIDER_OK python={python} pin={PARSEHUB_PIN}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("venv", type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    venv = args.venv.expanduser().resolve()
    if not args.check_only:
        if venv.exists():
            raise SystemExit("PARSEHUB_PROVIDER_VENV_ALREADY_EXISTS")
        try:
            _run([args.python, "-m", "venv", str(venv)])
            python = _venv_python(venv)
            _run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
            _run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    f"git+{PARSEHUB_GIT_URL}@{PARSEHUB_PIN}",
                ]
            )
        except Exception:
            shutil.rmtree(venv, ignore_errors=True)
            raise
    _verify(venv)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
