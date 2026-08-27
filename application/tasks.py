"""Task orchestration and persistence helpers."""
from __future__ import annotations

import json
import threading
import time
import uuid
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from sqlmodel import Session, select

from core.account_graph import (
    load_account_graphs,
    patch_account_graph,
    recover_lifecycle_status_for_valid_account,
)
from core.base_platform import AccountStatus, RegisterConfig
from core.datetime_utils import format_local_clock, serialize_datetime
from core.db import AccountModel, TaskEventModel, TaskModel, engine, save_account
from core.platform_accounts import build_platform_account
from core.registry import get
from infrastructure.platform_runtime import PlatformRuntime
from application.manual_otp import ManualOtpHandle, manual_otp_registry


def create_mailbox(*args, **kwargs):
    """Delegate mailbox creation while keeping the task-level test seam."""
    from core.base_mailbox import create_mailbox as mailbox_factory

    return mailbox_factory(*args, **kwargs)

TASK_TYPE_REGISTER = "register"
TASK_TYPE_ACCOUNT_CHECK_ALL = "account_check_all"
TASK_TYPE_GCASH_CHECK_ALL = "gcash_check_all"
TASK_TYPE_TRIAL_CHECK_ALL = "trial_check_all"
TASK_TYPE_PLATFORM_ACTION = "platform_action"

TASK_STATUS_PENDING = "pending"
TASK_STATUS_CLAIMED = "claimed"
TASK_STATUS_RUNNING = "running"
TASK_STATUS_SUCCEEDED = "succeeded"
TASK_STATUS_FAILED = "failed"
TASK_STATUS_INTERRUPTED = "interrupted"
TASK_STATUS_CANCEL_REQUESTED = "cancel_requested"
TASK_STATUS_CANCELLED = "cancelled"

# A single registration task may run many workers.  Twenty is the UI and
# backend contract; anything larger is deliberately bounded to avoid an
# accidental unbounded thread pool.
MAX_REGISTER_CONCURRENCY = 20

TERMINAL_TASK_STATUSES = {
    TASK_STATUS_SUCCEEDED,
    TASK_STATUS_FAILED,
    TASK_STATUS_INTERRUPTED,
    TASK_STATUS_CANCELLED,
}
ACTIVE_TASK_STATUSES = {
    TASK_STATUS_CLAIMED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_CANCEL_REQUESTED,
}

_task_locks: dict[str, threading.Lock] = {}
_task_locks_guard = threading.Lock()

# Cancellation is deliberately kept in memory as well as in the task row.
# The API must be able to signal a running worker without waiting for its next
# database poll, and active protocol/browser workers need a callback so their
# network session or browser can be closed immediately.
_task_cancel_events: dict[str, threading.Event] = {}
_task_cancel_callbacks: dict[str, set[Callable[[], None]]] = {}
_task_cancel_guard = threading.RLock()


def _task_cancel_event(task_id: str) -> threading.Event:
    with _task_cancel_guard:
        event = _task_cancel_events.get(task_id)
        if event is None:
            event = threading.Event()
            _task_cancel_events[task_id] = event
        return event


def register_task_cancel_callback(task_id: str, callback: Callable[[], None] | None) -> None:
    if not task_id or not callable(callback):
        return
    should_trigger = False
    with _task_cancel_guard:
        _task_cancel_callbacks.setdefault(task_id, set()).add(callback)
        event = _task_cancel_events.get(task_id)
        should_trigger = bool(event and event.is_set())
    if should_trigger:
        _invoke_cancel_callback(callback)


def unregister_task_cancel_callback(task_id: str, callback: Callable[[], None] | None) -> None:
    if not task_id or not callable(callback):
        return
    with _task_cancel_guard:
        callbacks = _task_cancel_callbacks.get(task_id)
        if not callbacks:
            return
        callbacks.discard(callback)
        if not callbacks:
            _task_cancel_callbacks.pop(task_id, None)


def clear_task_cancel_callbacks(task_id: str) -> None:
    if not task_id:
        return
    with _task_cancel_guard:
        _task_cancel_callbacks.pop(task_id, None)
        _task_cancel_events.pop(task_id, None)


def _invoke_cancel_callback(callback: Callable[[], None]) -> None:
    # Closing a browser or a network session should never block the HTTP
    # cancel endpoint.  A callback is therefore isolated in a tiny daemon
    # thread and exceptions are intentionally ignored.
    def _run() -> None:
        try:
            callback()
        except Exception:
            pass

    threading.Thread(target=_run, name="task-cancel-callback", daemon=True).start()


def _trigger_task_cancel(task_id: str) -> None:
    with _task_cancel_guard:
        _task_cancel_event(task_id).set()
        callbacks = list(_task_cancel_callbacks.get(task_id, set()))
    for callback in callbacks:
        _invoke_cancel_callback(callback)

# Sub2API Admin keys are intentionally volatile.  The browser submits them
# when creating a task, but they are never put in task payloads, the SQLite
# database, result data, or task logs.  The shared lock also makes attaching
# the config atomic with task creation, before a worker may claim the task.
_register_sub2api_upload_configs: dict[str, dict[str, str]] = {}
_register_sub2api_upload_configs_guard = threading.RLock()


def register_sub2api_upload_configs_guard() -> threading.RLock:
    return _register_sub2api_upload_configs_guard


def set_register_sub2api_upload_config(
    task_id: str,
    *,
    sub2api_url: str,
    api_key: str,
) -> None:
    with _register_sub2api_upload_configs_guard:
        _register_sub2api_upload_configs[task_id] = {
            "sub2api_url": str(sub2api_url or "").strip(),
            "api_key": str(api_key or "").strip(),
        }


def get_register_sub2api_upload_config(task_id: str) -> dict[str, str] | None:
    with _register_sub2api_upload_configs_guard:
        config = _register_sub2api_upload_configs.get(task_id)
        return dict(config) if config else None


def clear_register_sub2api_upload_config(task_id: str) -> None:
    with _register_sub2api_upload_configs_guard:
        _register_sub2api_upload_configs.pop(task_id, None)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _utcnow_iso() -> str:
    return _utcnow().isoformat().replace("+00:00", "Z")


def _serialize_datetime(value: datetime | None) -> str | None:
    return serialize_datetime(value)


def _json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return _serialize_datetime(value)
    raise TypeError(f"Object of type {value.__class__.__name__} is not JSON serializable")


def _dump_json(data: Any) -> str:
    return json.dumps(data or {}, ensure_ascii=False, default=_json_default)


def _task_lock(task_id: str) -> threading.Lock:
    with _task_locks_guard:
        lock = _task_locks.get(task_id)
        if lock is None:
            lock = threading.Lock()
            _task_locks[task_id] = lock
        return lock


def _mutate_task(task_id: str, fn: Callable[[TaskModel], None]) -> Optional[TaskModel]:
    with _task_lock(task_id):
        with Session(engine) as session:
            task = session.get(TaskModel, task_id)
            if not task:
                return None
            fn(task)
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            session.refresh(task)
            return task


def _task_result_seed(result: dict[str, Any] | None = None) -> dict[str, Any]:
    base = {"errors": [], "cashier_urls": [], "data": None}
    if result:
        base.update(result)
    return base


def _task_account_keys(task_type: str, payload: dict[str, Any]) -> list[str]:
    if task_type == TASK_TYPE_PLATFORM_ACTION:
        account_id = int(payload.get("account_id", 0) or 0)
        if account_id > 0:
            return [f"account:{account_id}"]
    return []


def serialize_task(task: TaskModel) -> dict[str, Any]:
    result = task.get_result()
    progress_total = int(task.progress_total or 0)
    progress_current = int(task.progress_current or 0)
    return {
        "id": task.id,
        "task_id": task.id,
        "type": task.type,
        "platform": task.platform,
        "status": task.status,
        "terminal": task.status in TERMINAL_TASK_STATUSES,
        "cancellable": task.status in {TASK_STATUS_PENDING, TASK_STATUS_CLAIMED, TASK_STATUS_RUNNING, TASK_STATUS_CANCEL_REQUESTED},
        "progress": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        "progress_detail": {
            "current": progress_current,
            "total": progress_total,
            "label": f"{progress_current}/{progress_total}" if progress_total else "0/0",
        },
        "success": int(task.success_count or 0),
        "error_count": int(task.error_count or 0),
        "errors": list(result.get("errors", [])),
        "cashier_urls": list(result.get("cashier_urls", [])),
        "data": result.get("data"),
        "result": result,
        "error": task.error,
        "created_at": _serialize_datetime(task.created_at),
        "started_at": _serialize_datetime(task.started_at),
        "finished_at": _serialize_datetime(task.finished_at),
        "updated_at": _serialize_datetime(task.updated_at),
    }


def serialize_event(event: TaskEventModel) -> dict[str, Any]:
    return {
        "id": event.id,
        "task_id": event.task_id,
        "type": event.type,
        "level": event.level,
        "message": event.message,
        "line": f"[{format_local_clock(event.created_at)}] {event.message}",
        "detail": event.get_detail(),
        "created_at": _serialize_datetime(event.created_at),
    }


def create_task(
    *,
    task_type: str,
    platform: str,
    payload: dict[str, Any],
    progress_total: int = 1,
    result_seed: dict[str, Any] | None = None,
) -> dict[str, Any]:
    task_id = f"task_{int(time.time() * 1000)}_{uuid.uuid4().hex[:6]}"
    task = TaskModel(
        id=task_id,
        type=task_type,
        platform=platform,
        status=TASK_STATUS_PENDING,
        payload_json=_dump_json(payload),
        result_json=_dump_json(_task_result_seed(result_seed)),
        progress_current=0,
        progress_total=max(int(progress_total or 0), 0),
    )
    with Session(engine) as session:
        session.add(task)
        session.commit()
        session.refresh(task)
    append_task_event(task.id, f"任务已创建: {task_type}", event_type="state")
    return serialize_task(task)


def create_register_task(payload: dict[str, Any]) -> dict[str, Any]:
    count = max(int(payload.get("count", 1) or 1), 1)
    payload = {**payload, "platform": "chatgpt"}
    return create_task(
        task_type=TASK_TYPE_REGISTER,
        platform="chatgpt",
        payload=payload,
        progress_total=count,
    )


def create_account_check_all_task(
    platform: str = "",
    limit: int | None = None,
    account_ids: list[int] | None = None,
    proxy: str | None = None,
    check_chain: bool | None = None,
) -> dict[str, Any]:
    """Create a status/subscription check task.

    The old task accepted a ``limit`` and defaulted to the newest 50 rows.
    ``limit`` remains available for scheduled/legacy callers, while new calls
    scan the complete platform or the explicitly selected account IDs.
    """

    normalized_ids: list[int] = []
    for raw_id in account_ids or []:
        try:
            account_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if account_id > 0 and account_id not in normalized_ids:
            normalized_ids.append(account_id)

    payload: dict[str, Any] = {"platform": platform}
    if account_ids is not None:
        payload["ids"] = normalized_ids
    normalized_proxy = str(proxy or "").strip()
    if normalized_proxy:
        payload["proxy"] = normalized_proxy
    if check_chain is not None:
        payload["check_chain"] = bool(check_chain)
    progress_total = len(normalized_ids)
    if limit is not None:
        bounded_limit = max(int(limit or 1), 1)
        payload["limit"] = bounded_limit
        if not progress_total:
            progress_total = bounded_limit
    return create_task(
        task_type=TASK_TYPE_ACCOUNT_CHECK_ALL,
        platform=platform,
        payload=payload,
        progress_total=progress_total,
    )


def create_gcash_check_all_task(
    platform: str = "chatgpt",
    account_ids: list[int] | None = None,
    proxy: str | None = None,
) -> dict[str, Any]:
    normalized_ids: list[int] = []
    for raw_id in account_ids or []:
        try:
            account_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if account_id > 0 and account_id not in normalized_ids:
            normalized_ids.append(account_id)
    normalized_proxy = str(proxy or "").strip()
    payload = {
        "platform": str(platform or "chatgpt").strip() or "chatgpt",
        "ids": normalized_ids,
        "proxy": normalized_proxy,
    }
    return create_task(
        task_type=TASK_TYPE_GCASH_CHECK_ALL,
        platform=payload["platform"],
        payload=payload,
        progress_total=len(normalized_ids),
    )


def create_trial_check_all_task(
    platform: str = "chatgpt",
    account_ids: list[int] | None = None,
    proxy: str | None = None,
    country: str = "PH",
    check_chain: bool = False,
) -> dict[str, Any]:
    normalized_ids: list[int] = []
    for raw_id in account_ids or []:
        try:
            account_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if account_id > 0 and account_id not in normalized_ids:
            normalized_ids.append(account_id)
    payload = {
        "platform": str(platform or "chatgpt").strip() or "chatgpt",
        "ids": normalized_ids,
        "proxy": str(proxy or "").strip(),
        "country": str(country or "PH").strip().upper(),
        "check_chain": bool(check_chain),
    }
    return create_task(
        task_type=TASK_TYPE_TRIAL_CHECK_ALL,
        platform=payload["platform"],
        payload=payload,
        progress_total=len(normalized_ids),
    )


def create_platform_action_task(payload: dict[str, Any]) -> dict[str, Any]:
    return create_task(
        task_type=TASK_TYPE_PLATFORM_ACTION,
        platform=str(payload.get("platform", "")),
        payload=payload,
        progress_total=1,
    )


def get_task(task_id: str) -> Optional[dict[str, Any]]:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        return serialize_task(task) if task else None


def list_task_events(task_id: str, *, since: int = 0, limit: int = 200) -> list[dict[str, Any]]:
    limit = min(max(limit, 1), 500)
    with Session(engine) as session:
        q = (
            select(TaskEventModel)
            .where(TaskEventModel.task_id == task_id)
            .where(TaskEventModel.id > since)
            .order_by(TaskEventModel.id)
            .limit(limit)
        )
        items = session.exec(q).all()
    return [serialize_event(item) for item in items]


def append_task_event(task_id: str, message: str, *, event_type: str = "log", level: str = "info", detail: dict | None = None) -> dict[str, Any]:
    with Session(engine) as session:
        event = TaskEventModel(
            task_id=task_id,
            type=event_type,
            level=level,
            message=message,
            detail_json=_dump_json(detail or {}),
        )
        session.add(event)
        session.commit()
        session.refresh(event)
    return serialize_event(event)


def mark_incomplete_tasks_interrupted() -> None:
    interrupted_ids: list[str] = []
    with Session(engine) as session:
        non_terminal = [TASK_STATUS_PENDING] + list(ACTIVE_TASK_STATUSES)
        tasks = session.exec(
            select(TaskModel).where(TaskModel.status.in_(non_terminal))
        ).all()
        for task in tasks:
            task.status = TASK_STATUS_INTERRUPTED
            task.error = task.error or "任务在服务重启后被中断"
            task.finished_at = _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            interrupted_ids.append(task.id)
        session.commit()
    for task_id in interrupted_ids:
        append_task_event(
            task_id,
            "任务在服务重启后被标记为中断",
            event_type="state",
            level="warning",
        )


def request_cancel(task_id: str) -> Optional[dict[str, Any]]:
    # Signal in memory before touching SQLite so currently running workers
    # observe cancellation even if their next DB read has not happened yet.
    with Session(engine) as session:
        existing = session.get(TaskModel, task_id)
    if not existing:
        return None
    if existing.status in TERMINAL_TASK_STATUSES:
        return serialize_task(existing)
    _trigger_task_cancel(task_id)
    task = _mutate_task(
        task_id,
        lambda model: _request_cancel_mutation(model),
    )
    if not task:
        return None
    append_task_event(task_id, "已请求取消任务", event_type="state", level="warning")
    return serialize_task(task)


def _request_cancel_mutation(task: TaskModel) -> None:
    if task.status in TERMINAL_TASK_STATUSES:
        return
    # Cancellation is a user-stop command, not a deferred request.  Mark the
    # task terminal immediately; running workers are independently signalled
    # through the in-memory event/callback registry and will clean themselves
    # up without keeping the UI in a waiting state.
    task.status = TASK_STATUS_CANCELLED
    task.finished_at = task.finished_at or _utcnow()
    task.error = task.error or "任务已取消"


def claim_next_runnable_task(
    *,
    running_platform_counts: dict[str, int] | None = None,
    busy_account_keys: set[str] | None = None,
    max_parallel_per_platform: int = 1,
) -> Optional[dict[str, Any]]:
    running_platform_counts = dict(running_platform_counts or {})
    busy_account_keys = set(busy_account_keys or set())
    with Session(engine) as session:
        tasks = session.exec(
            select(TaskModel)
            .where(TaskModel.status == TASK_STATUS_PENDING)
            .order_by(TaskModel.created_at)
        ).all()
        for task in tasks:
            payload = task.get_payload()
            platform = task.platform or str(payload.get("platform", "") or "")
            account_keys = _task_account_keys(task.type, payload)
            if platform and running_platform_counts.get(platform, 0) >= max_parallel_per_platform:
                continue
            if account_keys and busy_account_keys.intersection(account_keys):
                continue
            task.status = TASK_STATUS_CLAIMED
            task.started_at = task.started_at or _utcnow()
            task.updated_at = _utcnow()
            session.add(task)
            session.commit()
            return {"id": task.id, "platform": platform, "account_keys": account_keys}
    return None


class TaskLogger:
    def __init__(self, task_id: str):
        self.task_id = task_id
        # 并发任务里每个 worker 通过 ``set_subtask`` 把自己的 subtask_id
        # 绑到 thread-local，之后 ``log()`` 自动把 ``subtask_id`` 注入
        # 事件 detail，前端按这个分组折叠展示。
        self._tlocal = threading.local()

    def set_subtask(self, subtask_id: str, label: str = "") -> None:
        """绑定当前线程的子任务标签。子任务结束后调 ``clear_subtask`` 解绑。

        ``subtask_id`` 是稳定标识（如 ``worker_1``）；``label`` 是给前端
        展示的人类可读标题（如"账号 #1"）。
        """
        self._tlocal.subtask_id = str(subtask_id or "")
        self._tlocal.subtask_label = str(label or "")

    def clear_subtask(self) -> None:
        try:
            del self._tlocal.subtask_id
        except AttributeError:
            pass
        try:
            del self._tlocal.subtask_label
        except AttributeError:
            pass

    def _current_subtask(self) -> tuple[str, str]:
        sid = getattr(self._tlocal, "subtask_id", "") or ""
        label = getattr(self._tlocal, "subtask_label", "") or ""
        return sid, label

    def log(self, message: str, *, level: str = "info", event_type: str = "log", detail: dict | None = None) -> None:
        # 自动给当前线程绑定的 subtask 加 detail，用于前端按 worker 分组折叠
        merged_detail = dict(detail or {})
        sid, slabel = self._current_subtask()
        if sid and "subtask_id" not in merged_detail:
            merged_detail["subtask_id"] = sid
        if slabel and "subtask_label" not in merged_detail:
            merged_detail["subtask_label"] = slabel
        append_task_event(
            self.task_id,
            message,
            event_type=event_type,
            level=level,
            detail=merged_detail or None,
        )
        prefix = f"[task:{self.task_id}]"
        if sid:
            prefix += f"[{sid}]"
        print(f"{prefix} {message}")

    def mark_running(self) -> None:
        def _update(task: TaskModel) -> None:
            if task.status in TERMINAL_TASK_STATUSES or task.status == TASK_STATUS_CANCEL_REQUESTED:
                return
            task.status = TASK_STATUS_RUNNING
            task.started_at = task.started_at or _utcnow()

        updated = _mutate_task(self.task_id, _update)
        if updated and updated.status == TASK_STATUS_RUNNING:
            self.log("任务已开始执行", event_type="state")

    def is_cancel_requested(self) -> bool:
        with _task_cancel_guard:
            event = _task_cancel_events.get(self.task_id)
            if event is not None and event.is_set():
                return True
        with Session(engine) as session:
            task = session.get(TaskModel, self.task_id)
            return bool(task and task.status in {TASK_STATUS_CANCEL_REQUESTED, TASK_STATUS_CANCELLED})

    def register_cancel_callback(self, callback: Callable[[], None] | None) -> None:
        register_task_cancel_callback(self.task_id, callback)

    def unregister_cancel_callback(self, callback: Callable[[], None] | None) -> None:
        unregister_task_cancel_callback(self.task_id, callback)

    def set_progress(self, current: int, total: Optional[int] = None) -> None:
        current = max(int(current), 0)

        def _update(task: TaskModel) -> None:
            task.progress_current = current
            if total is not None:
                task.progress_total = max(int(total), 0)

        _mutate_task(self.task_id, _update)

    def record_success(self) -> None:
        def _update(task: TaskModel) -> None:
            task.success_count += 1

        _mutate_task(self.task_id, _update)

    def record_error(self, error: str) -> None:
        def _update(task: TaskModel) -> None:
            task.error_count += 1
            result = task.get_result()
            errors = list(result.get("errors", []))
            errors.append(error)
            result["errors"] = errors
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def add_cashier_url(self, url: str) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            urls = list(result.get("cashier_urls", []))
            urls.append(url)
            result["cashier_urls"] = urls
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def set_result_data(self, data: Any) -> None:
        def _update(task: TaskModel) -> None:
            result = task.get_result()
            result["data"] = data
            task.set_result(result)

        _mutate_task(self.task_id, _update)

    def finish(self, status: str, *, error: str = "") -> None:
        def _update(task: TaskModel) -> None:
            # A worker that returns after the user has cancelled must not
            # overwrite the terminal cancelled state with succeeded/failed.
            if task.status == TASK_STATUS_CANCELLED and status != TASK_STATUS_CANCELLED:
                return
            task.status = status
            task.finished_at = _utcnow()
            if error:
                task.error = error

        _mutate_task(self.task_id, _update)
        event_level = "error" if status == TASK_STATUS_FAILED else ("warning" if status in {TASK_STATUS_INTERRUPTED, TASK_STATUS_CANCELLED} else "info")
        self.log(
            f"任务结束: {status}",
            level=event_level,
            event_type="state",
            detail={"status": status, "error": error},
        )


def _build_platform_instance(platform_name: str, payload: dict[str, Any], logger: TaskLogger, resolved_proxy: str | None = None, shared_mailbox=None):
    from core.base_identity import normalize_identity_provider

    executor_type = str(payload.get("executor_type", "headless") or "headless")
    captcha_solver = str(payload.get("captcha_solver", "auto") or "auto")
    extra = dict(payload.get("extra") or {})
    subtask_getter = getattr(logger, "_current_subtask", None)
    subtask_id, subtask_label = subtask_getter() if callable(subtask_getter) else ("", "")
    task_id = str(getattr(logger, "task_id", "") or "")
    if task_id:
        extra["_manual_otp_handle"] = ManualOtpHandle(
            task_id=task_id,
            worker_id=subtask_id or "main",
            worker_label=subtask_label or subtask_id or "Main",
        )
        # Platform adapters use this volatile hook to register the concrete
        # protocol session/browser close callback once the worker is built.
        register_cancel_callback = getattr(logger, "register_cancel_callback", None)
        if callable(register_cancel_callback):
            extra["_register_cancel_callback"] = register_cancel_callback
    config = RegisterConfig(
        executor_type=executor_type,
        captcha_solver=captcha_solver,
        proxy=resolved_proxy,
        extra=extra,
    )
    identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
    mailbox = shared_mailbox
    if mailbox is None and identity_provider == "mailbox":
        if not extra.get("mail_provider"):
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
        mailbox = create_mailbox(
            provider=extra.get("mail_provider", ""),
            extra=extra,
            proxy=resolved_proxy,
        )

    platform_cls = get(platform_name)
    platform = platform_cls(config=config, mailbox=mailbox)
    if hasattr(platform, "set_logger"):
        platform.set_logger(logger.log)
    else:
        platform._log_fn = logger.log
    # Browser registration can block while waiting for an OTP. Wire the task
    # cancellation checker into headed/headless platform instances so mailbox
    # polling and the browser state machine can stop without changing the
    # protocol registration path.
    if executor_type in {"headless", "headed"} and hasattr(platform, "set_cancel_checker"):
        platform.set_cancel_checker(logger.is_cancel_requested)
    return platform


def _run_single_account_check(
    account_id: int,
    logger: TaskLogger | None = None,
    *,
    proxy: str | None = None,
    check_chain: bool | None = None,
) -> tuple[bool, dict[str, Any]]:
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        # Status checks should also enrich the account overview with Plus
        # trial eligibility.  The standalone trial action and the status
        # action must use the same eligibility signal.
        config_extra = {"check_trial": True}
        if check_chain is not None:
            config_extra["check_checkout_chain"] = bool(check_chain)
        plugin = get(model.platform)(config=RegisterConfig(proxy=proxy, extra=config_extra))
        account = build_platform_account(session, model)

    valid = plugin.check_valid(account)
    check_overview = plugin.get_last_check_overview() if hasattr(plugin, "get_last_check_overview") else {}
    check_state = str((check_overview or {}).get("check_state") or "").strip().lower()
    unavailable = check_state in {"unavailable", "unknown", "credential_missing", "credential_invalid"}
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            model.updated_at = _utcnow()
            current_graph = load_account_graphs(session, [account_id]).get(account_id, {})
            summary_updates = {"checked_at": _utcnow_iso(), "valid": None if unavailable else bool(valid)}
            summary_updates.update(check_overview or {})
            lifecycle_status = None
            if valid:
                # **bug 修复**：原实现 ``recover_lifecycle_status_for_valid_account``
                # 直接读 ``current_graph`` 老快照——但 plugin 刚拉到的新
                # ``plan_state`` 在 ``summary_updates`` 里、还没写回 graph，
                # 导致 free → 重新刷新仍然被认成 subscribed。这里把
                # ``summary_updates`` merge 到 graph 里再算 lifecycle。
                merged_graph = dict(current_graph)
                merged_overview = dict(merged_graph.get("overview") or {})
                merged_overview.update(summary_updates)
                merged_graph["overview"] = merged_overview
                lifecycle_status = recover_lifecycle_status_for_valid_account(merged_graph)
            patch_account_graph(
                session,
                model,
                lifecycle_status=lifecycle_status,
                summary_updates=summary_updates,
            )
            session.add(model)
            session.commit()

    result = {
        "account_id": account_id,
        "valid": bool(valid),
        "check_state": check_state or ("valid" if valid else "invalid"),
        "platform": account.platform,
        "email": account.email,
    }
    if logger:
        logger.log(f"{account.email}: {'有效' if valid else ('检测失败' if unavailable else '失效')}")
    return valid, result


def execute_task(task_id: str) -> None:
    with Session(engine) as session:
        task = session.get(TaskModel, task_id)
        if not task:
            return
        task_type = task.type
        payload = task.get_payload()

    logger = TaskLogger(task_id)
    logger.mark_running()

    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务在启动后立即被取消")
        if task_type == TASK_TYPE_REGISTER:
            clear_register_sub2api_upload_config(task_id)
            manual_otp_registry.clear_task(task_id)
        clear_task_cancel_callbacks(task_id)
        return

    handlers: dict[str, Callable[[dict[str, Any], TaskLogger], None]] = {
        TASK_TYPE_REGISTER: _execute_register_task,
        TASK_TYPE_ACCOUNT_CHECK_ALL: _execute_account_check_all_task,
        TASK_TYPE_GCASH_CHECK_ALL: _execute_gcash_check_all_task,
        TASK_TYPE_TRIAL_CHECK_ALL: _execute_trial_check_all_task,
        TASK_TYPE_PLATFORM_ACTION: _execute_platform_action_task,
    }
    handler = handlers.get(task_type)
    if not handler:
        logger.finish(TASK_STATUS_FAILED, error=f"未知任务类型: {task_type}")
        return
    try:
        handler(payload, logger)
    finally:
        if task_type == TASK_TYPE_REGISTER:
            clear_register_sub2api_upload_config(task_id)
            manual_otp_registry.clear_task(task_id)
        clear_task_cancel_callbacks(task_id)


def _resolve_registration_proxy_for_platform(
    platform_name: str,
    *,
    explicit_proxy: str | None,
    proxy_getter: Callable[[], str | None],
) -> str | None:
    normalized_explicit_proxy = str(explicit_proxy or "").strip() or None
    if str(platform_name or "").strip().lower() == "chatgpt":
        # ChatGPT 只使用本次任务显式传入的动态 IP；留空时固定本地直连，
        # 不从全局代理池回退。
        return normalized_explicit_proxy
    return normalized_explicit_proxy or proxy_getter()


def _registration_concurrency(requested: Any, count: int) -> int:
    return min(
        max(int(requested or 1), 1),
        max(int(count or 1), 1),
        MAX_REGISTER_CONCURRENCY,
    )


def _upload_registered_chatgpt_account_to_sub2api(
    account_id: int,
    *,
    sub2api_url: str,
    api_key: str,
) -> dict:
    """Send exactly one newly saved Agent Identity to Sub2API."""
    from application.account_exports import AccountExportsService
    from domain.accounts import AccountExportSelection

    return AccountExportsService().upload_chatgpt_agent_identity_to_sub2api(
        AccountExportSelection(
            platform="chatgpt",
            ids=[int(account_id)],
            select_all=False,
        ),
        sub2api_url=sub2api_url,
        api_key=api_key,
    )


def _execute_register_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    from core.proxy_pool import proxy_pool

    count = max(int(payload.get("count", 1) or 1), 1)
    concurrency = _registration_concurrency(payload.get("concurrency", 1), count)
    platform_name = "chatgpt"
    email = payload.get("email") or None
    password = payload.get("password") or None
    explicit_proxy = str(payload.get("proxy") or "").strip() or None
    extra = dict(payload.get("extra") or {})
    task_id = str(getattr(logger, "task_id", "") or "")
    sub2api_upload_enabled = bool(extra.get("auto_upload_sub2api_agent_identity"))
    sub2api_upload_config = (
        get_register_sub2api_upload_config(task_id)
        if sub2api_upload_enabled and task_id
        else None
    )
    resolved_proxy = _resolve_registration_proxy_for_platform(
        platform_name,
        explicit_proxy=explicit_proxy,
        proxy_getter=proxy_pool.get_next,
    )

    logger.set_progress(0, count)
    if sub2api_upload_enabled and not sub2api_upload_config:
        logger.log(
            "已启用 Sub2API 自动上传，但当前任务缺少临时上传凭据；注册账号不会上传",
            level="warning",
        )
    try:
        get(platform_name)
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return

    shared_mailbox = None
    try:
        from core.base_identity import normalize_identity_provider

        identity_provider = normalize_identity_provider(extra.get("identity_provider", "mailbox"))
        if identity_provider == "mailbox":
            if not extra.get("mail_provider"):
                from infrastructure.provider_settings_repository import ProviderSettingsRepository

                extra["mail_provider"] = ProviderSettingsRepository().get_default_provider_key("mailbox")
            shared_mailbox = create_mailbox(
                provider=extra.get("mail_provider", ""),
                extra=extra,
                proxy=resolved_proxy,
            )
    except Exception as exc:
        logger.log(f"邮箱初始化失败: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=f"邮箱初始化失败: {exc}")
        return

    upload_lock = threading.Lock()
    failure_lock = threading.Lock()
    uploaded_count = 0
    upload_failures: list[str] = []
    failed_accounts: list[dict[str, Any]] = []

    def _do_one(index: int) -> dict[str, Any] | str:
        nonlocal uploaded_count
        if logger.is_cancel_requested():
            return "__cancel_requested__"
        logger.set_subtask(f"worker_{index + 1}", f"Worker {index + 1}")
        try:
            platform = _build_platform_instance(
                platform_name,
                payload,
                logger,
                resolved_proxy=resolved_proxy,
                shared_mailbox=shared_mailbox,
            )
            if logger.is_cancel_requested():
                return "__cancel_requested__"
            logger.log(f"开始注册第 {index + 1}/{count} 个账号")
            if resolved_proxy:
                logger.log(f"使用代理: {resolved_proxy}")
            account = platform.register(email=email, password=password)
            # A registration may return at the same moment the user presses
            # cancel.  Never persist a late result after cancellation.
            if logger.is_cancel_requested():
                return "__cancel_requested__"
            saved_account = save_account(account)
            saved_account_id = int(saved_account.id)
            if logger.is_cancel_requested():
                return "__cancel_requested__"
            if resolved_proxy:
                proxy_pool.report_success(resolved_proxy)
            if sub2api_upload_config:
                if logger.is_cancel_requested():
                    return "__cancel_requested__"
                logger.log(f"正在上传 {account.email} 的 Agent Identity 到 Sub2API")
                try:
                    _upload_registered_chatgpt_account_to_sub2api(
                        saved_account_id,
                        sub2api_url=sub2api_upload_config["sub2api_url"],
                        api_key=sub2api_upload_config["api_key"],
                    )
                except Exception as upload_exc:
                    upload_error = str(upload_exc)
                    with upload_lock:
                        upload_failures.append(upload_error)
                    logger.log(
                        f"{account.email} 的 Agent Identity 上传失败：{upload_error}",
                        level="error",
                    )
                else:
                    with upload_lock:
                        uploaded_count += 1
                    logger.log(f"{account.email} 的 Agent Identity 已上传到 Sub2API")
            logger.record_success()
            logger.log(f"注册成功: {account.email}")
            return {
                "account_id": saved_account_id,
                "email": account.email,
            }
        except Exception as exc:
            if resolved_proxy:
                proxy_pool.report_fail(resolved_proxy)
            error = str(exc)
            failed_email = str(
                getattr(exc, "registration_email", "") or email or ""
            ).strip()
            failure_detail = {
                "index": index,
                "email": failed_email,
                "error": error,
            }
            with failure_lock:
                failed_accounts.append(failure_detail)
            logger.record_error(error)
            logger.log(
                f"注册失败: {failed_email + ' - ' if failed_email else ''}{error}",
                level="error",
            )
            return error
        finally:
            logger.clear_subtask()

    success = 0
    errors: list[str] = []
    registered_accounts: list[dict[str, Any]] = []
    completed = 0
    pool: ThreadPoolExecutor | None = None
    cancelled = False
    try:
        pool = ThreadPoolExecutor(max_workers=concurrency)
        pending = {pool.submit(_do_one, index) for index in range(count)}
        while pending:
            # A short timeout makes the dispatcher responsive to the cancel
            # event even while every registration is blocked in network I/O.
            done, pending = wait(
                pending,
                timeout=0.1,
                return_when=FIRST_COMPLETED,
            )
            if logger.is_cancel_requested():
                cancelled = True
                for future in pending:
                    future.cancel()
                break
            for future in done:
                result = future.result()
                completed += 1
                if isinstance(result, dict):
                    success += 1
                    registered_accounts.append(result)
                elif result != "__cancel_requested__":
                    errors.append(str(result))
                logger.set_progress(completed, count)
    except Exception as exc:
        logger.log(f"致命错误: {exc}", level="error")
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return
    finally:
        if pool is not None:
            # Do not hold the task worker open for in-flight registrations.
            # Their platform/browser/session has already received the cancel
            # callback and will exit cooperatively in its own worker thread.
            pool.shutdown(wait=not cancelled, cancel_futures=True)

    if cancelled:
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return

    result_data = {
        "success": success,
        "fail": len(errors),
        "account_ids": [item["account_id"] for item in registered_accounts],
        "accounts": registered_accounts,
        "auto_upload_sub2api_agent_identity": sub2api_upload_enabled,
    }
    if sub2api_upload_enabled:
        result_data["sub2api_agent_identity_upload"] = {
            "submitted": uploaded_count,
            "failed": len(upload_failures),
            "errors": upload_failures,
        }
    if failed_accounts:
        ordered_failures = sorted(failed_accounts, key=lambda item: int(item["index"]))
        result_data["failed_accounts"] = [
            {"email": item["email"], "error": item["error"]}
            for item in ordered_failures
        ]
        result_data["failed_emails"] = [
            item["email"] or "未知邮箱"
            for item in ordered_failures
        ]
    logger.set_result_data(result_data)
    summary = f"完成: 成功 {success} 个, 失败 {len(errors)} 个"
    if failed_accounts:
        failed_emails = [
            item["email"] or "未知邮箱"
            for item in sorted(failed_accounts, key=lambda item: int(item["index"]))
        ]
        summary += f"；失败邮箱: {'、'.join(failed_emails)}"
    logger.log(summary, event_type="summary")
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    final_status = TASK_STATUS_FAILED if errors and success == 0 else TASK_STATUS_SUCCEEDED
    final_error = errors[0] if final_status == TASK_STATUS_FAILED else ""
    if final_status == TASK_STATUS_FAILED and failed_accounts:
        first_failure = min(failed_accounts, key=lambda item: int(item["index"]))
        if first_failure["email"]:
            final_error = f"{first_failure['email']}: {final_error}"
    logger.finish(final_status, error=final_error)


def _execute_platform_action_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    command_platform = str(payload.get("platform", ""))
    account_id = int(payload.get("account_id", 0) or 0)
    action_id = str(payload.get("action_id", ""))
    params = dict(payload.get("params") or {})
    runtime = PlatformRuntime()
    result = runtime.execute_action(
        type("Command", (), {
            "platform": command_platform,
            "account_id": account_id,
            "action_id": action_id,
            "params": params,
        })(),
        log_fn=logger.log,
        cancel_check=logger.is_cancel_requested,
    )
    if logger.is_cancel_requested() or str(result.error or "") == "任务已取消":
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    if not result.ok:
        logger.record_error(result.error)
        logger.finish(TASK_STATUS_FAILED, error=result.error)
        return
    logger.set_result_data(result.data)
    message = ""
    if isinstance(result.data, dict):
        message = str(result.data.get("message", "") or "")
    if message:
        logger.log(message, event_type="summary")
    logger.set_progress(1, 1)
    logger.finish(TASK_STATUS_SUCCEEDED)


def _execute_account_check_all_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform", "") or "")
    proxy = str(payload.get("proxy") or "").strip() or None
    check_chain = payload.get("check_chain")
    check_chain = True if check_chain is None else bool(check_chain)
    # Keep the result shape of tasks created by the pre-batch-check API.  New
    # API-created tasks always carry ``ids`` (possibly an empty list), so they
    # receive the richer summary below without breaking old callers/tests.
    legacy_result_shape = (
        "limit" in payload
        and "ids" not in payload
        and "account_ids" not in payload
    )
    raw_ids = payload.get("ids")
    if raw_ids is None:
        # ``account_ids`` was used by an early internal caller; accept it so
        # tasks created during an upgrade can still be resumed.
        raw_ids = payload.get("account_ids")
    account_ids: list[int] = []
    for raw_id in raw_ids or []:
        try:
            account_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if account_id > 0 and account_id not in account_ids:
            account_ids.append(account_id)

    raw_limit = payload.get("limit")
    limit: int | None = None
    if raw_limit not in (None, ""):
        limit = max(int(raw_limit or 1), 1)

    with Session(engine) as session:
        q = select(AccountModel)
        if platform:
            q = q.where(AccountModel.platform == platform)
        if account_ids:
            q = q.where(AccountModel.id.in_(account_ids))
        q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        if limit is not None:
            q = q.limit(limit)
        accounts = session.exec(q).all()

    total = len(accounts)
    logger.set_progress(0, total)
    results = {
        "total": total,
        "checked": 0,
        "valid": 0,
        "invalid": 0,
        "error": 0,
    }

    def publish_results() -> None:
        logger.set_result_data(
            {key: results[key] for key in ("valid", "invalid", "error")}
            if legacy_result_shape
            else results
        )

    if total == 0:
        publish_results()
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    completed = 0
    for model in accounts:
        if logger.is_cancel_requested():
            results["checked"] = completed
            publish_results()
            logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
            return
        try:
            valid, result = _run_single_account_check(
                int(model.id or 0),
                logger,
                proxy=proxy,
                check_chain=check_chain,
            )
            if valid:
                results["valid"] += 1
                logger.record_success()
            elif str(result.get("check_state") or "").lower() in {
                "unavailable", "unknown", "credential_missing", "credential_invalid",
            }:
                results["error"] += 1
                logger.log(f"{model.email}: 检测失败", level="error")
                logger.record_error(f"{model.email}: 检测失败")
            else:
                results["invalid"] += 1
        except Exception as exc:
            results["error"] += 1
            logger.record_error(str(exc))
            logger.log(f"{model.email}: 检测异常 {exc}", level="error")
        completed += 1
        results["checked"] = completed
        logger.set_progress(completed, total)
    publish_results()
    logger.log(
        f"批量检测完成: 共 {total} 个，已检查 {completed} 个，"
        f"有效 {results['valid']}，失效 {results['invalid']}，异常 {results['error']}",
        event_type="summary",
    )
    logger.finish(TASK_STATUS_SUCCEEDED)


def _run_single_gcash_check(
    account_id: int,
    logger: TaskLogger | None = None,
    *,
    proxy: str,
    context: Any,
) -> dict[str, Any]:
    from platforms.chatgpt.gcash import check_gcash

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        account = build_platform_account(session, model)
        email = str(model.email or "")

    result = check_gcash(account, proxy=proxy, context=context)
    updates = dict(result or {})
    updates["gcash_checked_at"] = _utcnow_iso()
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            model.updated_at = _utcnow()
            current_graph = load_account_graphs(session, [account_id]).get(account_id, {})
            current_overview = dict(current_graph.get("overview") or {})
            chips = [
                str(chip)
                for chip in current_overview.get("chips") or []
                if str(chip or "").strip().lower() != "gcash"
            ]
            if result.get("gcash_eligible"):
                chips.append("GCash")
            updates["chips"] = chips
            patch_account_graph(session, model, summary_updates=updates)
            session.add(model)
            session.commit()
    if logger:
        logger.log(f"{email}: {'有 GCash 资格' if result.get('gcash_eligible') else '无 GCash 资格'}")
    return {
        "account_id": account_id,
        "email": email,
        "gcash_eligible": bool(result.get("gcash_eligible")),
    }


def _execute_gcash_check_all_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform", "chatgpt") or "chatgpt")
    proxy = str(payload.get("proxy") or "").strip()
    raw_ids = payload.get("ids")
    account_ids: list[int] = []
    for raw_id in raw_ids or []:
        try:
            account_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if account_id > 0 and account_id not in account_ids:
            account_ids.append(account_id)

    with Session(engine) as session:
        q = select(AccountModel).where(AccountModel.platform == platform)
        if account_ids:
            q = q.where(AccountModel.id.in_(account_ids))
        q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        accounts = session.exec(q).all()

    total = len(accounts)
    results = {"total": total, "checked": 0, "qualified": 0, "not_qualified": 0, "error": 0}
    logger.set_progress(0, total)
    if total == 0:
        logger.set_result_data(results)
        logger.finish(TASK_STATUS_SUCCEEDED)
        return

    context = None
    try:
        from platforms.chatgpt.gcash import GCashCheckContext
        context = GCashCheckContext(proxy)
        logger.register_cancel_callback(context.close)
        for model in accounts:
            if logger.is_cancel_requested():
                logger.set_result_data(results)
                logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
                return
            try:
                result = _run_single_gcash_check(
                    int(model.id or 0), logger, proxy=proxy, context=context
                )
                if result["gcash_eligible"]:
                    results["qualified"] += 1
                    logger.record_success()
                else:
                    results["not_qualified"] += 1
            except Exception as exc:
                results["error"] += 1
                logger.record_error(f"{model.email}: {exc}")
                logger.log(f"{model.email}: GCash 检测失败 {exc}", level="error")
            results["checked"] += 1
            logger.set_progress(results["checked"], total)
    except Exception as exc:
        logger.record_error(str(exc))
        logger.log(f"GCash 检测任务初始化失败: {exc}", level="error")
        logger.set_result_data(results)
        logger.finish(TASK_STATUS_FAILED, error=str(exc))
        return
    finally:
        if context is not None:
            logger.unregister_cancel_callback(context.close)
            try:
                context.close()
            except Exception:
                pass

    logger.set_result_data(results)
    logger.log(
        f"GCash 检测完成: 共 {total} 个，已检查 {results['checked']} 个，"
        f"有资格 {results['qualified']}，无资格 {results['not_qualified']}，异常 {results['error']}",
        event_type="summary",
    )
    logger.finish(TASK_STATUS_SUCCEEDED)


def _run_single_trial_check(
    account_id: int,
    logger: TaskLogger | None = None,
    *,
    proxy: str,
    country: str,
    check_chain: bool,
) -> dict[str, Any]:
    from platforms.chatgpt.subscription import check_plus_trial

    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if not model:
            raise ValueError("账号不存在")
        if model.platform != "chatgpt":
            raise ValueError("试用资格检测目前仅支持 ChatGPT")
        account = build_platform_account(session, model)
        email = str(model.email or "")

    class _A:
        pass

    a = _A()
    a.access_token = getattr(account, "access_token", "") or getattr(account, "token", "")
    a.cookies = getattr(account, "cookies", "")
    a.id_token = getattr(account, "id_token", "")
    a.extra = getattr(account, "extra", {}) or {}
    a.account_id = getattr(account, "account_id", "") or getattr(account, "user_id", "")
    a.region = getattr(account, "region", "")
    result = check_plus_trial(
        a,
        proxy=proxy,
        country=country,
        check_chain=check_chain,
    )
    updates = dict(result or {})
    updates["plus_trial_checked_at"] = _utcnow_iso()
    with Session(engine) as session:
        model = session.get(AccountModel, account_id)
        if model:
            model.updated_at = _utcnow()
            current_graph = load_account_graphs(session, [account_id]).get(account_id, {})
            current_overview = dict(current_graph.get("overview") or {})
            chips = [
                str(chip)
                for chip in current_overview.get("chips") or []
                if str(chip or "").strip() not in {"带Plus试用", "Plus trial eligible"}
            ]
            if result.get("plus_trial_eligible") is True:
                chips.append("带Plus试用")
            updates["chips"] = chips
            patch_account_graph(session, model, summary_updates=updates)
            session.add(model)
            session.commit()
    if logger:
        logger.log(f"{email}: {'有试用资格' if result.get('plus_trial_eligible') else '无试用资格'}")
    return {
        "account_id": account_id,
        "email": email,
        "plus_trial_eligible": result.get("plus_trial_eligible"),
    }


def _execute_trial_check_all_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    platform = str(payload.get("platform", "chatgpt") or "chatgpt")
    proxy = str(payload.get("proxy") or "").strip()
    country = str(payload.get("country") or "PH").strip().upper()
    check_chain = bool(payload.get("check_chain"))
    raw_ids = payload.get("ids")
    account_ids: list[int] = []
    for raw_id in raw_ids or []:
        try:
            account_id = int(raw_id)
        except (TypeError, ValueError):
            continue
        if account_id > 0 and account_id not in account_ids:
            account_ids.append(account_id)
    with Session(engine) as session:
        q = select(AccountModel).where(AccountModel.platform == platform)
        if account_ids:
            q = q.where(AccountModel.id.in_(account_ids))
        q = q.order_by(AccountModel.created_at.desc(), AccountModel.id.desc())
        accounts = session.exec(q).all()

    total = len(accounts)
    results = {"total": total, "checked": 0, "qualified": 0, "not_qualified": 0, "error": 0}
    logger.set_progress(0, total)
    for model in accounts:
        if logger.is_cancel_requested():
            logger.set_result_data(results)
            logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
            return
        try:
            result = _run_single_trial_check(
                int(model.id or 0),
                logger,
                proxy=proxy,
                country=country,
                check_chain=check_chain,
            )
            if result.get("plus_trial_eligible") is True:
                results["qualified"] += 1
                logger.record_success()
            else:
                results["not_qualified"] += 1
        except Exception as exc:
            results["error"] += 1
            logger.record_error(f"{model.email}: {exc}")
            logger.log(f"{model.email}: 试用检测失败 {exc}", level="error")
        results["checked"] += 1
        logger.set_progress(results["checked"], total)

    logger.set_result_data(results)
    logger.log(
        f"试用检测完成: 共 {total} 个，已检查 {results['checked']} 个，"
        f"有资格 {results['qualified']}，无资格 {results['not_qualified']}，异常 {results['error']}",
        event_type="summary",
    )
    logger.finish(TASK_STATUS_SUCCEEDED)
