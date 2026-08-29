#!/usr/bin/env python3
"""Create or verify exact-pin Provider Lab virtual environments."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import shutil
import subprocess
import sys


PROVIDERS = {
    "parsehub": {
        "pin": "144260f4349c5bb57415f7555a572fcd48c3b8ba",
        "git": "https://github.com/z-mio/ParseHub.git",
        "provider_id": "parsehub_repo",
        "license": "MIT",
    },
    "videodl": {
        "pin": "6cd1de0fe1209380b183b654c65e98a629637873",
        "git": "https://github.com/CharlesPikachu/videodl.git",
        "provider_id": "videodl_repo",
        "license": "PolyForm Noncommercial 1.0.0",
    },
}
WORKER_ROOT = Path(__file__).resolve().parent / "workers"


def venv_python(target: Path) -> Path:
    return target / ("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def verify(provider: str, target: Path) -> None:
    config = PROVIDERS[provider]
    python = venv_python(target)
    worker = WORKER_ROOT / f"{provider}_worker.py"
    if not python.is_file():
        raise SystemExit("PROVIDER_LAB_PYTHON_MISSING")
    completed = subprocess.run(
        [str(python), str(worker), "--check-only"],
        check=False,
        capture_output=True,
        text=True,
        timeout=30,
    )
    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as error:
        raise SystemExit("PROVIDER_LAB_CHECK_INVALID") from error
    if (
        completed.returncode != 0
        or payload.get("status") != "ok"
        or payload.get("provider_id") != config["provider_id"]
        or config["pin"] not in str(payload.get("provider_version") or "")
    ):
        raise SystemExit("PROVIDER_LAB_PIN_MISMATCH")
    print(
        f"PROVIDER_LAB_OK provider={provider} python={python} "
        f"pin={config['pin']} license={config['license']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", required=True, choices=tuple(PROVIDERS))
    parser.add_argument("--target", required=True, type=Path)
    parser.add_argument("--python", default=sys.executable)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args()
    provider = args.provider
    target = args.target.expanduser().resolve()
    config = PROVIDERS[provider]
    if provider == "videodl":
        print(
            "VIDEODL_LICENSE_NOTICE: PolyForm Noncommercial 1.0.0; "
            "local Staff research only; public/production use is not authorized."
        )
    if not args.check_only:
        if target.exists():
            raise SystemExit("PROVIDER_LAB_TARGET_ALREADY_EXISTS")
        try:
            run([args.python, "-m", "venv", str(target)])
            python = venv_python(target)
            run([str(python), "-m", "pip", "install", "--upgrade", "pip"])
            run(
                [
                    str(python),
                    "-m",
                    "pip",
                    "install",
                    f"git+{config['git']}@{config['pin']}",
                ]
            )
        except Exception:
            shutil.rmtree(target, ignore_errors=True)
            raise
    verify(provider, target)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
