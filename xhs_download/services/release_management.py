import os
import re
import shlex
import subprocess
from dataclasses import dataclass

from django.conf import settings
from django.db import IntegrityError, transaction
from django.utils import timezone

from ..models import (
    ReleaseDeployJob,
    ReleaseDeployJobStatus,
    ReleaseDeployTargetType,
)


SAFE_TAG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/@+-]{0,159}$")
SAFE_SHA_RE = re.compile(r"^[0-9a-fA-F]{7,64}$")
FULL_SHA_RE = re.compile(r"^[0-9a-fA-F]{40,64}$")
OUTPUT_TAIL_CHARS = 12000
ACTIVE_JOB_STATUSES = (
    ReleaseDeployJobStatus.PENDING,
    ReleaseDeployJobStatus.RUNNING,
)
DUBIOUS_OWNERSHIP_MARKER = "detected dubious ownership"
PRODUCTION_SAFE_DIRECTORY_COMMAND = (
    "sudo git config --system --add safe.directory /srv/animeimages"
)
READ_ONLY_GIT_COMMANDS = frozenset({"merge-base", "rev-list", "rev-parse", "show", "tag"})
RELEASE_EXECUTOR_COMMAND_TEMPLATE = (
    "/usr/bin/sudo",
    "-n",
    "/usr/local/sbin/animeimages-release-submit",
    "{job_id}",
)
EXECUTOR_ERROR_MAX_CHARS = 300
EXECUTOR_ERROR_INPUT_MAX_CHARS = 4000
ENV_ASSIGNMENT_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*=[^\s]+")
CONTROL_CHARACTER_RE = re.compile(r"[\x00-\x1f\x7f]+")


@dataclass(frozen=True)
class GitResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    returncode: int = 0


def env_float(name, default=0.0):
    value = os.environ.get(name)
    if value is None or value == "":
        return float(default)
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def env_str(name, default=""):
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip()


def release_git_timeout_seconds():
    return env_float("RELEASE_MANAGEMENT_GIT_TIMEOUT_SECONDS", 6.0)


def release_trigger_timeout_seconds():
    return env_float("RELEASE_MANAGEMENT_TRIGGER_TIMEOUT_SECONDS", 5.0)


def release_deploy_timeout_seconds():
    return env_float("RELEASE_MANAGEMENT_DEPLOY_TIMEOUT_SECONDS", 900.0)


def release_deploy_script():
    configured = env_str("RELEASE_MANAGEMENT_DEPLOY_SCRIPT", "")
    return configured or f"{release_base_dir()}/scripts/deploy.sh"


def release_base_dir():
    return str(getattr(settings, "BASE_DIR"))


def run_git(args, *, timeout=None):
    if not args or args[0] not in READ_ONLY_GIT_COMMANDS:
        return GitResult(
            ok=False,
            stderr="Release Management Git access is read-only; command is not allowed.",
            returncode=2,
        )
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


def git_read_error_message(result):
    stderr = str(getattr(result, "stderr", "") or "").strip()
    if DUBIOUS_OWNERSHIP_MARKER in stderr.lower():
        return (
            "Git 无法读取本地仓库：检测到 dubious ownership。"
            f"请由服务器管理员执行 `{PRODUCTION_SAFE_DIRECTORY_COMMAND}` 后重试。"
        )
    if stderr:
        return f"Git 无法读取本地仓库：{stderr[:500]}"
    return f"Git 无法读取本地仓库：命令退出码 {getattr(result, 'returncode', '不可用')}。"


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


def resolve_local_origin_main_sha():
    result = run_git(["rev-parse", "origin/main"])
    if not result.ok:
        raise ValueError(git_read_error_message(result))
    target_sha = result.stdout
    if not FULL_SHA_RE.fullmatch(target_sha):
        raise ValueError("无法从本地 origin/main 解析精确 commit SHA。")
    reachable = run_git(["merge-base", "--is-ancestor", target_sha, "origin/main"])
    if not reachable.ok:
        if DUBIOUS_OWNERSHIP_MARKER in (reachable.stderr or "").lower():
            raise ValueError(git_read_error_message(reachable))
        raise ValueError("本地目标 commit 不是 origin/main 的可达提交。")
    return target_sha.lower()


def build_release_state():
    git_errors = []

    def read_local_git(args, default=""):
        result = run_git(args)
        if not result.ok:
            if not git_errors:
                git_errors.append(git_read_error_message(result))
            return default
        return result.stdout

    commit_sha = read_local_git(["rev-parse", "HEAD"])
    short_sha = read_local_git(["rev-parse", "--short", "HEAD"])
    branch = read_local_git(["rev-parse", "--abbrev-ref", "HEAD"])
    current_tags = read_local_git(["tag", "--points-at", "HEAD"]).splitlines()
    current_tag = current_tags[0] if current_tags else ""
    commit_time = read_local_git(["show", "-s", "--format=%cI", "HEAD"])
    origin_main_sha = read_local_git(["rev-parse", "origin/main"])
    origin_main_short_sha = read_local_git(["rev-parse", "--short", "origin/main"])
    ahead, behind = split_count_pair(
        read_local_git(["rev-list", "--left-right", "--count", "HEAD...origin/main"])
    )
    tag_rows = parse_tag_rows(
        read_local_git([
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
        "git": {
            "ok": not git_errors,
            "error": git_errors[0] if git_errors else "",
        },
    }


def release_executor_command():
    command = env_str("RELEASE_MANAGEMENT_EXECUTOR_COMMAND", "")
    if not command:
        return []
    try:
        parsed = shlex.split(command)
    except ValueError as error:
        raise ValueError(
            "RELEASE_MANAGEMENT_EXECUTOR_COMMAND 配置无效：命令引号未闭合。"
        ) from error
    if tuple(parsed) != RELEASE_EXECUTOR_COMMAND_TEMPLATE:
        raise ValueError(
            "RELEASE_MANAGEMENT_EXECUTOR_COMMAND 配置无效：只允许固定的 "
            "sudo -n release submit helper 与 {job_id} 占位符。"
        )
    return parsed


def approved_release_executor_command(job_id):
    try:
        normalized_job_id = int(job_id)
    except (TypeError, ValueError) as error:
        raise ValueError("release executor job ID must be a positive integer") from error
    if normalized_job_id <= 0 or str(normalized_job_id) != str(job_id):
        raise ValueError("release executor job ID must be a positive integer")
    return [*RELEASE_EXECUTOR_COMMAND_TEMPLATE[:3], str(normalized_job_id)]


def release_executor_trigger_command_is_approved(command, job_id):
    try:
        expected = approved_release_executor_command(job_id)
    except ValueError:
        return False
    return isinstance(command, list) and command == expected


def sanitize_executor_error(error_message, fallback="release executor submission failed"):
    message = str(error_message or "")[:EXECUTOR_ERROR_INPUT_MAX_CHARS]
    message = CONTROL_CHARACTER_RE.sub(" ", message)
    message = ENV_ASSIGNMENT_RE.sub("[redacted]", message)
    message = " ".join(message.split())
    return (message or fallback)[:EXECUTOR_ERROR_MAX_CHARS]


def build_command_plan(job):
    executor = release_executor_command()
    trigger_command = approved_release_executor_command(job.pk) if executor else []
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
            "Release Management v1 only supports a pinned origin/main commit.",
        ],
    }


def create_release_deploy_job(*, request, target_type, target_ref, notes=""):
    target_type = str(target_type or "").strip()
    if target_type != ReleaseDeployTargetType.MAIN:
        raise ValueError("Release Management v1 仅支持 main 部署，Tag 版本切换暂未开放。")
    target_ref = validate_target_ref(target_type, target_ref)
    active_job = ReleaseDeployJob.objects.filter(status__in=ACTIVE_JOB_STATUSES).first()
    if active_job is not None:
        raise ValueError(f"已有未完成部署任务 #{active_job.pk}。")
    target_sha = resolve_local_origin_main_sha()

    user = getattr(request, "user", None)
    try:
        with transaction.atomic():
            job = ReleaseDeployJob.objects.create(
                execution_slot=1,
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
    except IntegrityError as error:
        active_job = ReleaseDeployJob.objects.filter(status__in=ACTIVE_JOB_STATUSES).first()
        if active_job is not None:
            raise ValueError(f"已有未完成部署任务 #{active_job.pk}。") from error
        raise
    return job


def cancel_pending_release_deploy_job(job_id):
    now = timezone.now()
    return ReleaseDeployJob.objects.filter(
        pk=job_id,
        status=ReleaseDeployJobStatus.PENDING,
    ).update(
        status=ReleaseDeployJobStatus.CANCELLED,
        finished_at=now,
        error_message="cancelled by admin",
        updated_at=now,
    )


def claim_pending_release_deploy_job(job_id):
    now = timezone.now()
    claimed = ReleaseDeployJob.objects.filter(
        pk=job_id,
        status=ReleaseDeployJobStatus.PENDING,
    ).update(
        status=ReleaseDeployJobStatus.RUNNING,
        started_at=now,
        updated_at=now,
    )
    if not claimed:
        return None
    return ReleaseDeployJob.objects.get(pk=job_id)


def mark_executor_trigger_failed(job, error_message):
    now = timezone.now()
    message = sanitize_executor_error(error_message)
    ReleaseDeployJob.objects.filter(
        pk=job.pk,
        status=ReleaseDeployJobStatus.PENDING,
    ).update(
        status=ReleaseDeployJobStatus.FAILED,
        finished_at=now,
        error_message=message,
        updated_at=now,
    )
    job.refresh_from_db()


def trigger_release_executor(job):
    command = (job.command_plan or {}).get("trigger_command") or []
    if not command:
        result = GitResult(ok=False, stderr="release executor is not configured", returncode=127)
        mark_executor_trigger_failed(job, result.stderr)
        return result
    if not release_executor_trigger_command_is_approved(command, job.pk):
        result = GitResult(
            ok=False,
            stderr="release executor command does not match the approved sudo helper",
            returncode=2,
        )
        mark_executor_trigger_failed(job, result.stderr)
        return result
    try:
        completed = subprocess.run(
            command,
            cwd=release_base_dir(),
            check=False,
            capture_output=True,
            text=True,
            timeout=release_trigger_timeout_seconds(),
        )
    except subprocess.TimeoutExpired:
        result = GitResult(
            ok=False,
            stderr="release executor submission timed out",
            returncode=124,
        )
        mark_executor_trigger_failed(job, result.stderr)
        return result
    except OSError as error:
        result = GitResult(
            ok=False,
            stderr=f"release executor could not be started ({type(error).__name__})",
            returncode=127,
        )
        mark_executor_trigger_failed(job, result.stderr)
        return result
    result = GitResult(
        ok=completed.returncode == 0,
        stdout=(completed.stdout or "").strip(),
        stderr=(
            ""
            if completed.returncode == 0
            else sanitize_executor_error(
                completed.stderr,
                fallback=f"release executor exited with status {completed.returncode}",
            )
        ),
        returncode=completed.returncode,
    )
    if not result.ok:
        mark_executor_trigger_failed(job, result.stderr or f"release executor exited with {result.returncode}")
    return result


def execute_release_deploy_job(job):
    if job.status != ReleaseDeployJobStatus.RUNNING:
        raise ValueError("release deploy job must be claimed before execution")
    if job.target_type != ReleaseDeployTargetType.MAIN or not FULL_SHA_RE.fullmatch(job.target_sha):
        job.status = ReleaseDeployJobStatus.FAILED
        job.finished_at = timezone.now()
        job.error_message = "v1 requires a main job with an exact target SHA"
        job.save(update_fields=("status", "finished_at", "error_message", "updated_at"))
        return job

    script_path = release_deploy_script()
    try:
        completed = subprocess.run(
            [script_path],
            cwd=release_base_dir(),
            check=False,
            capture_output=True,
            text=True,
            timeout=release_deploy_timeout_seconds(),
            env={
                **os.environ,
                "APP_DIR": release_base_dir(),
                "DEPLOY_BRANCH": "main",
                "DEPLOY_TARGET_SHA": job.target_sha,
            },
        )
        job.stdout_tail = (completed.stdout or "")[-OUTPUT_TAIL_CHARS:]
        job.stderr_tail = (completed.stderr or "")[-OUTPUT_TAIL_CHARS:]
        if completed.returncode == 0:
            deployed_head = run_git(["rev-parse", "HEAD"])
            if not deployed_head.ok or deployed_head.stdout.lower() != job.target_sha.lower():
                job.status = ReleaseDeployJobStatus.FAILED
                job.error_message = "deployed HEAD does not match job.target_sha"
            else:
                job.status = ReleaseDeployJobStatus.SUCCEEDED
                job.health_check_status = "ready"
        else:
            job.status = ReleaseDeployJobStatus.FAILED
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
            "health_check_status",
            "updated_at",
        )
    )
    return job
