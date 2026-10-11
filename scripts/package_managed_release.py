#!/usr/bin/env python3
"""Freeze the reviewed, clean Git artifacts for a one-time administrator install."""

import argparse
import hashlib
import io
from pathlib import Path
import subprocess
import tarfile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parent.parent

    def git(*arguments):
        return subprocess.check_output(["git", "--no-replace-objects", "-C", str(root), *arguments])

    if git("status", "--porcelain").strip():
        parser.error("package only a clean, reviewed tooling checkout")
    sha = git("rev-parse", "HEAD").decode().strip()
    artifacts = {
        "install.py": "ops/managed-release/install.py",
        "animeimages-consumer-probe": "ops/managed-release/animeimages-consumer-probe",
        "animeimages-managed-release.sudoers": "ops/managed-release/animeimages-managed-release.sudoers",
        "check_deploy_readability.py": "scripts/check_deploy_readability.py",
    }
    # Read committed blobs, never a mutable worktree file after its clean check.
    blobs = {name: git("show", f"{sha}:{path}") for name, path in artifacts.items()}
    blobs["TOOL_SHA"] = (sha + "\n").encode("ascii")
    with args.output.open("xb") as output:
        with tarfile.open(fileobj=output, mode="w") as archive:
            for name, data in blobs.items():
                info = tarfile.TarInfo(name)
                info.size = len(data)
                info.mode = 0o600
                archive.addfile(info, io.BytesIO(data))
    print(f"tool={sha}")
    print(f"sha256={hashlib.sha256(args.output.read_bytes()).hexdigest()}")
    print(f"bundle={args.output}")


if __name__ == "__main__":
    main()
