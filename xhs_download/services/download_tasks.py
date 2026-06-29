import threading
import time
import uuid
from pathlib import Path

from download_console.models import DownloadRuntimeSetting
from .downloads import build_download_archive_file
from .rate_limits import consume_daily, release_concurrency


TASK_TTL_SECONDS = 30 * 60
_tasks = {}
_lock = threading.Lock()


def _now():
    return time.time()


def cleanup_download_tasks():
    cutoff = _now() - TASK_TTL_SECONDS
    expired = []

    with _lock:
        for task_id, task in _tasks.items():
            if task.get("created_at", 0) < cutoff:
                expired.append((task_id, task))
        for task_id, _ in expired:
            _tasks.pop(task_id, None)

    for _, task in expired:
        archive_file = task.get("file")
        if archive_file:
            try:
                archive_file.close()
            except Exception:
                pass


def serialize_task(task):
    return {
        "task_id": task["task_id"],
        "status": task["status"],
        "progress": int(task.get("progress", 0)),
        "message": task.get("message", ""),
        "filename": task.get("filename", ""),
        "saved_count": task.get("saved_count", 0),
        "failed_count": task.get("failed_count", 0),
    }


def get_download_task(task_id):
    cleanup_download_tasks()
    with _lock:
        task = _tasks.get(task_id)
        return dict(task) if task else None


def pop_download_task(task_id):
    with _lock:
        return _tasks.pop(task_id, None)


def update_task(task_id, **changes):
    with _lock:
        task = _tasks.get(task_id)
        if not task:
            return
        task.update(changes)
        task["updated_at"] = _now()


def _download_limit(payload, key, fallback):
    limits = payload.get("download_runtime_limits") or {}
    value = limits.get(key, fallback)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _download_bool(payload, key, fallback):
    limits = payload.get("download_runtime_limits") or {}
    value = limits.get(key, fallback)
    return bool(value)


def run_download_task(task_id, payload, owner_identity):
    archive = None

    def progress_callback(completed, total):
        if total <= 0:
            return
        completed = max(0, min(float(completed), float(total)))
        progress = 5 + round((completed / total) * 90)
        completed_count = min(int(completed), total)
        update_task(
            task_id,
            progress=min(progress, 95),
            message=f"正在打包 {completed_count}/{total}",
        )

    try:
        update_task(task_id, status="running", progress=5, message="正在准备资源")
        archive = build_download_archive_file(
            media_items=payload.get("media_items", []),
            base_filename=payload.get("base_filename", "xhs"),
            save_format=payload.get("save_format", "original"),
            quality=payload.get("quality", 95),
            video_save_mode=payload.get("video_save_mode", "video"),
            progress_callback=progress_callback,
        )
        archive_size = Path(archive["file"].name).stat().st_size
        runtime_setting = None
        if not payload.get("download_runtime_limits"):
            runtime_setting = DownloadRuntimeSetting.load()
        single_archive_bytes_limit = _download_limit(
            payload,
            "single_archive_bytes_limit",
            runtime_setting.single_archive_bytes_limit if runtime_setting else 0,
        )
        staff_large_download_exempt = _download_bool(
            payload,
            "staff_large_download_exempt",
            runtime_setting.staff_large_download_exempt if runtime_setting else False,
        )
        if (
            single_archive_bytes_limit > 0
            and archive_size > single_archive_bytes_limit
            and not staff_large_download_exempt
        ):
            archive["file"].close()
            archive = None
            raise RuntimeError("打包后的文件超过单次 ZIP 下载限制")
        daily_bytes_limit_per_ip = _download_limit(
            payload,
            "daily_bytes_limit_per_ip",
            runtime_setting.daily_bytes_limit_per_ip if runtime_setting else 0,
        )
        global_daily_bytes_limit = _download_limit(
            payload,
            "global_daily_bytes_limit",
            runtime_setting.global_daily_bytes_limit if runtime_setting else 0,
        )
        per_ip_bytes = consume_daily(
            "download-bytes-ip",
            owner_identity,
            daily_bytes_limit_per_ip,
            amount=archive_size,
        )
        if not per_ip_bytes.allowed:
            archive["file"].close()
            archive = None
            raise RuntimeError("今日下载流量已达到限制，请稍后再试")

        global_bytes = consume_daily(
            "download-bytes-global",
            "all",
            global_daily_bytes_limit,
            amount=archive_size,
        )
        if not global_bytes.allowed:
            archive["file"].close()
            archive = None
            raise RuntimeError("今日下载流量已达到限制，请稍后再试")
        update_task(
            task_id,
            status="completed",
            progress=100,
            message="打包完成",
            filename=archive["filename"],
            file=archive["file"],
            saved_count=archive["saved_count"],
            failed_count=archive["failed_count"],
        )
        archive = None
    except Exception as error:
        if archive and archive.get("file"):
            archive["file"].close()
        update_task(
            task_id,
            status="failed",
            progress=100,
            message=str(error),
        )
    finally:
        release_concurrency("download-concurrent", owner_identity)


def create_download_task(payload, owner_identity):
    cleanup_download_tasks()
    task_id = uuid.uuid4().hex
    task = {
        "task_id": task_id,
        "status": "queued",
        "progress": 0,
        "message": "等待开始",
        "filename": "",
        "saved_count": 0,
        "failed_count": 0,
        "created_at": _now(),
        "updated_at": _now(),
    }

    with _lock:
        _tasks[task_id] = task

    worker = threading.Thread(
        target=run_download_task,
        args=(task_id, payload, owner_identity),
        daemon=True,
    )
    worker.start()
    return task
