import os
import re
import shlex
import subprocess
from dataclasses import dataclass

from django.conf import settings
from django.utils import timezone

from ..models import (
    ReleaseDeployJob,
    ReleaseDeployJobStatus,
    ReleaseDeployTargetType,
)


SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,159}$")
SAFE_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
OUTPUT_TAIL_CHARS = 12000


@dataclass(frozen=True)
class GitResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def release_git_timeout_seconds():
    return float(getattr(settings, "RELEASE_MANAGEMENT_GIT_TIMEOUT_SECONDS", 6.0))


def release_base_dir():
    return str(getattr(settings, "BASE_DIR"))


def run_git(args, *, timeout=None):
    timeout = release_git_timeout_seconds() if timeout is None else timeout
    try:
        completed = subprocess.run(
            ["git", *args],
            cwd=release_base_dir(),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return GitResult(
            ok=False,
            stderr=f"{type(error).__name__}: {error}",
            returncode=124,
        )
    return GitResult(
        ok=completed.returncode == 0,
        stdout=(completed.stdout or "").strip(),
        stderr=(completed.stderr or "").strip(),
        returncode=completed.returncode,
    )


def git_stdout(args, default=""):
    result = run_git(args)
    if not result.ok:
        return default
    return result.stdout


def split_count_pair(value):
    parts = str(value or "").split()
    if len(parts) != 2:
        return 0, 0
    try:
        return int(parts[0]), int(parts[1])
    except ValueError:
        return 0, 0


def parse_tag_rows(value):
    tags = []
    for line in str(value or "").splitlines():
        name, sha, created_at = (line.split("|", 2) + ["", "", ""])[:3]
        if not name:
            continue
        tags.append({
            "name": name,
            "sha": sha,
            "created_at": created_at,
        })
    return tags


def validate_target_ref(target_type, target_ref):
    target_ref = str(target_ref or "").strip()
    if target_type == ReleaseDeployTargetType.MAIN:
        return "main"
    if target_type == ReleaseDeployTargetType.TAG:
        if not SAFE_TAG_RE.match(target_ref):
            raise ValueError("版本 tag 格式不安全。")
        return target_ref
    if target_type == ReleaseDeployTargetType.COMMIT:
        if not SAFE_SHA_RE.match(target_ref):
            raise ValueError("commit SHA 格式不安全。")
        return target_ref
    raise ValueError("未知部署目标类型。")


def resolve_target_sha(target_type, target_ref):
    if target_type == ReleaseDeployTargetType.MAIN:
        result = run_git(["rev-parse", "origin/main"])
    elif target_type == ReleaseDeployTargetType.TAG:
        result = run_git(["rev-parse", f"refs/tags/{target_ref}^{{commit}}"])
    elif target_type == ReleaseDeployTargetType.COMMIT:
        result = run_git(["rev-parse", f"{target_ref}^{{commit}}"])
    else:
        return ""
    return result.stdout if result.ok else ""


def build_release_state(*, fetch_remote=False):
    fetch_result = None
    if fetch_remote:
        fetch_result = run_git(["fetch", "--tags", "origin"])

    commit_sha = git_stdout(["rev-parse", "HEAD"])
    short_sha = git_stdout(["rev-parse", "--short", "HEAD"])
    branch = git_stdout(["rev-parse", "--abbrev-ref", "HEAD"], default="HEAD")
    current_tags = git_stdout(["tag", "--points-at", "HEAD"]).splitlines()
    current_tag = current_tags[0] if current_tags else ""
    commit_time = git_stdout(["show", "-s", "--format=%cI", "HEAD"])
    origin_main_sha = git_stdout(["rev-parse", "origin/main"])
    origin_main_short_sha = git_stdout(["rev-parse", "--short", "origin/main"])
    ahead, behind = split_count_pair(git_stdout(["rev-list", "--left-right", "--count", "HEAD...origin/main"]))
    tag_rows = parse_tag_rows(
        git_stdout([
            "tag",
            "--sort=-creatordate",
            "--format=%(refname:short)|%(objectname:short)|%(creatordate:iso8601-strict)",
        ])
    )
    latest_success = (
        ReleaseDeployJob.objects.filter(status=ReleaseDeployJobStatus.SUCCEEDED)
        .order_by("-finished_at", "-created_at")
        .first()
    )

    return {
        "branch": branch,
        "commit_sha": commit_sha,
        "short_sha": short_sha,
        "current_tag": current_tag,
        "current_tags": current_tags,
        "commit_time": commit_time,
        "origin_main_sha": origin_main_sha,
        "origin_main_short_sha": origin_main_short_sha,
        "ahead_count": ahead,
        "behind_count": behind,
        "has_update": bool(origin_main_sha and commit_sha and origin_main_sha != commit_sha),
        "tags": tag_rows[:30],
        "latest_tag": tag_rows[0] if tag_rows else {},
        "latest_successful_job": latest_success,
        "fetch": {
            "attempted": bool(fetch_remote),
            "ok": None if fetch_result is None else fetch_result.ok,
            "error": "" if fetch_result is None or fetch_result.ok else fetch_result.stderr,
        },
    }


def release_executor_command():
    command = str(getattr(settings, "RELEASE_MANAGEMENT_EXECUTOR_COMMAND", "") or "").strip()
    if not command:
        return []
    return shlex.split(command)


def build_command_plan(job):
    executor = release_executor_command()
    if executor:
        trigger_command = [part.format(job_id=job.pk) for part in executor]
    else:
        trigger_command = []
    deploy_environment = {
        "DEPLOY_BRANCH": "main" if job.target_type == ReleaseDeployTargetType.MAIN else "",
        "DEPLOY_TARGET_TYPE": job.target_type,
        "DEPLOY_TARGET_REF": job.target_ref,
        "DEPLOY_TARGET_SHA": job.target_sha,
    }
    return {
        "executor_configured": bool(trigger_command),
        "trigger_command": trigger_command,
        "management_command": f"python manage.py run_release_deploy_job --job-id {job.pk}",
        "deploy_environment": deploy_environment,
        "notes": [
            "Django admin only records and triggers a deployment job.",
            "The actual deploy/restart must run outside the request thread.",
            "Tag/commit switching requires deploy script support before execution is enabled.",
        ],
    }


def create_release_deploy_job(*, request, target_type, target_ref, notes=""):
    target_type = str(target_type or "").strip()
    target_ref = validate_target_ref(target_type, target_ref)
    target_sha = resolve_target_sha(target_type, target_ref)
    if not target_sha:
        raise ValueError("无法解析目标版本，请先检查远程更新或 tag 是否存在。")
    active_job = ReleaseDeployJob.objects.filter(
        status__in=(ReleaseDeployJobStatus.PENDING, ReleaseDeployJobStatus.RUNNING),
    ).first()
    if active_job is not None:
        raise ValueError(f"已有未完成部署任务 #{active_job.pk}。")

    user = getattr(request, "user", None)
    job = ReleaseDeployJob.objects.create(
        status=ReleaseDeployJobStatus.PENDING,
        target_type=target_type,
        target_ref=target_ref,
        target_sha=target_sha,
        request_id=str(getattr(request, "request_id", "") or ""),
        actor_id=getattr(user, "pk", None),
        actor_username=str(getattr(user, "get_username", lambda: "")() or ""),
        actor_is_superuser=bool(getattr(user, "is_superuser", False)),
        notes=notes,
    )
    job.command_plan = build_command_plan(job)
    job.save(update_fields=("command_plan", "updated_at"))
    return job


def trigger_release_executor(job):
    command = (job.command_plan or {}).get("trigger_command") or []
    if not command:
        return GitResult(ok=False, stderr="release executor is not configured", returncode=127)
    try:
        completed = subprocess.run(
            command,
            cwd=release_base_dir(),
            check=False,
            capture_output=True,
            text=True,
            timeout=float(getattr(settings, "RELEASE_MANAGEMENT_TRIGGER_TIMEOUT_SECONDS", 5.0)),
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        return GitResult(ok=False, stderr=f"{type(error).__name__}: {error}", returncode=124)
    return GitResult(
        ok=completed.returncode == 0,
        stdout=(completed.stdout or "").strip(),
        stderr=(completed.stderr or "").strip(),
        returncode=completed.returncode,
    )


def execute_release_deploy_job(job):
    job.status = ReleaseDeployJobStatus.RUNNING
    job.started_at = timezone.now()
    job.save(update_fields=("status", "started_at", "updated_at"))

    if job.target_type != ReleaseDeployTargetType.MAIN:
        job.status = ReleaseDeployJobStatus.FAILED
        job.finished_at = timezone.now()
        job.error_message = "tag/commit deploy execution is not enabled until deploy.sh supports target refs"
        job.save(update_fields=("status", "finished_at", "error_message", "updated_at"))
        return job

    script_path = getattr(settings, "RELEASE_MANAGEMENT_DEPLOY_SCRIPT", "")
    if not script_path:
        script_path = f"{release_base_dir()}/scripts/deploy.sh"
    try:
        completed = subprocess.run(
            [script_path],
            cwd=release_base_dir(),
            check=False,
            capture_output=True,
            text=True,
            timeout=float(getattr(settings, "RELEASE_MANAGEMENT_DEPLOY_TIMEOUT_SECONDS", 900.0)),
            env={
                **os.environ,
                "APP_DIR": release_base_dir(),
                "DEPLOY_BRANCH": "main",
            },
        )
        job.stdout_tail = (completed.stdout or "")[-OUTPUT_TAIL_CHARS:]
        job.stderr_tail = (completed.stderr or "")[-OUTPUT_TAIL_CHARS:]
        job.status = (
            ReleaseDeployJobStatus.SUCCEEDED
            if completed.returncode == 0
            else ReleaseDeployJobStatus.FAILED
        )
        if completed.returncode != 0:
            job.error_message = f"deploy script exited with {completed.returncode}"
    except (OSError, subprocess.TimeoutExpired) as error:
        job.status = ReleaseDeployJobStatus.FAILED
        job.error_message = f"{type(error).__name__}: {error}"[:300]
    job.finished_at = timezone.now()
    job.save(
        update_fields=(
            "status",
            "finished_at",
            "stdout_tail",
            "stderr_tail",
            "error_message",
            "updated_at",
        )
    )
    return job
