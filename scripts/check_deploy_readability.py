#!/usr/bin/env python3
"""Read-only deployment probe, executed as the actual non-root consumer."""
import argparse
import importlib
import json
import os
from pathlib import Path
import stat
import sys


def parents_accessible(path):
    for parent in (path, *path.parents):
        if not os.access(parent, os.X_OK):
            raise PermissionError(f"directory not traversable: {parent}")


def artifact_protected(path):
    # Follow every link hop; sticky /tmp protects only other owners' entries.
    pending = [path.absolute()]
    checked = set()
    while pending:
        candidate = pending.pop()
        if candidate in checked:
            continue
        checked.add(candidate)
        resolved = candidate.resolve(strict=True)
        if resolved not in checked:
            pending.append(resolved)
        if os.access(candidate, os.W_OK):
            raise PermissionError(f"consumer can write application artifact: {candidate}")
        for entry in (candidate, *candidate.parents):
            metadata = entry.lstat()
            if metadata.st_uid == os.geteuid():
                raise PermissionError(f"consumer owns application artifact: {entry}")
            if stat.S_ISLNK(metadata.st_mode):
                target = entry.readlink()
                pending.append(target if target.is_absolute() else entry.parent / target)
            parent = entry.parent
            if parent == entry:
                break
            parent_metadata = parent.stat()
            if os.access(parent, os.W_OK | os.X_OK) and not (
                parent_metadata.st_mode & stat.S_ISVTX
                and os.geteuid() not in (metadata.st_uid, parent_metadata.st_uid)
            ):
                raise PermissionError(f"consumer can replace application artifacts via: {parent}")


def readable_file(path):
    parents_accessible(path.parent)
    artifact_protected(path)
    if not path.is_file():
        raise PermissionError(f"not a regular readable file: {path}")
    with path.open("rb") as stream:
        stream.read(1)
    if path.stat().st_mode & stat.S_IXUSR and not os.access(path, os.X_OK):
        raise PermissionError(f"artifact not executable: {path}")


def readable_tree(root, *, allow_external_files=False):
    parents_accessible(root)
    artifact_protected(root)
    count = 0
    def failed(error):
        raise error
    for current, dirs, files in os.walk(root, onerror=failed):
        for name in dirs:
            directory = Path(current) / name
            parents_accessible(directory)
            artifact_protected(directory)
            if directory.is_symlink() and not directory.resolve().is_relative_to(root):
                raise PermissionError(f"directory symlink needs explicit review: {directory}")
        for name in files:
            path = Path(current) / name
            if not allow_external_files and not path.resolve(strict=True).is_relative_to(root):
                raise PermissionError(f"artifact link escapes public root: {path}")
            readable_file(path)
            count += 1
    return count


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--expected-uid", type=int, required=True)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--runtime-root", type=Path)
    parser.add_argument("--paths-stdin", action="store_true")
    parser.add_argument("--static", action="store_true")
    args = parser.parse_args()
    if os.geteuid() == 0 or os.geteuid() != args.expected_uid:
        raise PermissionError("probe must run as the expected non-root identity")
    artifact_protected(args.root)
    root = args.root.resolve(strict=True)
    parents_accessible(root)
    if args.static:
        count = readable_tree(root)
        if not count:
            raise PermissionError("static output is empty")
    else:
        if not args.paths_stdin or not args.runtime_root:
            raise ValueError("code probe requires tracked paths and runtime root")
        paths = [os.fsdecode(p) for p in sys.stdin.buffer.read().split(b"\0") if p]
        if not paths:
            raise ValueError("empty tracked-path manifest")
        for name in paths:
            path = root / name
            if not path.resolve(strict=True).is_relative_to(root):
                raise PermissionError("tracked path escapes candidate root")
            readable_file(path)
        artifact_protected(args.runtime_root)
        runtime = args.runtime_root.resolve(strict=True)
        if runtime != Path(sys.prefix).resolve() or sys.prefix == sys.base_prefix:
            raise ValueError("probe interpreter must use the selected virtualenv")
        readable_tree(runtime, allow_external_files=True)
        for module in ("django", "gunicorn", "cryptography", "psycopg"):
            importlib.import_module(module)
        sys.path.insert(0, str(root))
        project = importlib.import_module("djangoProject")
        if not Path(project.__file__).resolve().is_relative_to(root):
            raise ValueError("application import came from another checkout")
        count = len(paths)
    print(json.dumps({"status": "PASS", "uid": os.geteuid(), "gid": os.getegid(),
                      "root": str(root), "files": count,
                      "scope": "static" if args.static else "code-and-runtime"}))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, ImportError) as exc:
        print(f"[readability] FAIL: {exc}", file=sys.stderr)
        raise SystemExit(1)
