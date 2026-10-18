"""Registration outcome statistics, independent of task execution and storage."""
from __future__ import annotations

from collections import Counter
import re
from typing import Any, Iterable


REGISTRATION_STAGES = (
    "initialization", "mailbox", "authorization", "verification", "profile",
    "credentials", "security", "persistence", "unknown",
)
ACTIVE_STATUSES = {"pending", "claimed", "running", "cancel_requested"}


def normalize_registration_stage(value: Any) -> str:
    value = str(value or "").strip().lower()
    return value if value in REGISTRATION_STAGES else "unknown"


def observe_registration_stage(message: str, current: str = "unknown") -> str:
    """Observe existing progress messages; unrecognized messages retain the phase.

    Security re-auth has its own OTP/token steps. Keep these within security
    instead of moving backwards into the initial registration verification.
    """
    current = normalize_registration_stage(current)
    marker = re.match(r"^\[registration:([a-z_]+)\]", str(message))
    if marker:
        return normalize_registration_stage(marker.group(1))
    if current == "security":
        return current
    text = str(message).lower()
    markers = (
        ("security", ("安全设置", "totp", "2fa", "two-factor")),
        ("credentials", ("access token", "access_token", "refresh_token", "oauth", "获取 session", "获取session", "换取 token", "回调")),
        ("profile", ("创建账号", "账号资料", "about-you", "create_account")),
        ("verification", ("验证码", "otp", "email-verification")),
        ("mailbox", ("邮箱初始化", "创建邮箱", "获取邮箱", "申请邮箱", "分配邮箱", "邮箱池", "邮箱租约")),
        ("authorization", ("初始化 chatgpt 协议", "开始 chatgpt", "authorize", "建立会话", "登录密码", "设置密码")),
        ("initialization", ("浏览器启动", "启动浏览器", "浏览器初始化", "配置错误")),
    )
    for stage, terms in markers:
        if any(term in text for term in terms):
            return stage
    return current


def _count(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError, OverflowError):
        return 0


def _stage_items(counts: Counter) -> list[dict[str, Any]]:
    order = {stage: index for index, stage in enumerate(REGISTRATION_STAGES)}
    return [
        {"stage": stage, "count": count}
        for stage, count in sorted(counts.items(), key=lambda item: (-item[1], order[item[0]]))
        if count > 0
    ]


def registration_snapshot(
    *, success: int, failed: int, result: dict, status: str,
    requested: int = 0, error: str = "",
) -> dict[str, Any]:
    success, failed = _count(success), _count(failed)
    completed = success + failed
    stages: Counter = Counter()
    raw_stages = result.get("failure_stages") or {}
    if isinstance(raw_stages, dict):
        for stage, count in raw_stages.items():
            stages[normalize_registration_stage(stage)] += _count(count)
    # Historical outcomes without structured stage data stay explicitly unknown.
    # Cap malformed/stale stage totals to the persisted number of failures.
    remaining = failed
    bounded: Counter = Counter()
    for stage in REGISTRATION_STAGES:
        value = min(remaining, stages[stage])
        bounded[stage] = value
        remaining -= value
    bounded["unknown"] += remaining
    setup_failed = status == "failed" and completed == 0
    setup_stage = normalize_registration_stage(result.get("setup_failure_stage"))
    if setup_stage == "unknown" and setup_failed:
        setup_stage = observe_registration_stage(error, "initialization")
    uncompleted = max(0, _count(requested) - completed)
    return {
        "success": success,
        "failed": failed,
        "completed": completed,
        "requested": _count(requested),
        "success_rate": round(success / completed * 100, 1) if completed else None,
        "remaining": uncompleted if status in ACTIVE_STATUSES else 0,
        "unattempted": uncompleted if status not in ACTIVE_STATUSES else 0,
        "failure_stages": _stage_items(bounded),
        "setup_failures": int(setup_failed),
        "setup_failure_stages": [{"stage": setup_stage, "count": 1}] if setup_failed else [],
    }


def aggregate_registration_statistics(tasks: Iterable[dict]) -> dict[str, Any]:
    success = failed = setup_failures = total_tasks = active_tasks = cancelled_tasks = interrupted_tasks = 0
    stages: Counter = Counter()
    setup_stages: Counter = Counter()
    for task in tasks:
        total_tasks += 1
        snapshot = registration_snapshot(
            success=task.get("success", 0), failed=task.get("error_count", 0),
            result=task.get("result") or {}, status=task.get("status", ""),
            requested=task.get("requested", 0), error=task.get("error", ""),
        )
        success += snapshot["success"]
        failed += snapshot["failed"]
        setup_failures += snapshot["setup_failures"]
        active_tasks += int(task.get("status") in ACTIVE_STATUSES)
        cancelled_tasks += int(task.get("status") == "cancelled")
        interrupted_tasks += int(task.get("status") == "interrupted")
        stages.update({item["stage"]: item["count"] for item in snapshot["failure_stages"]})
        setup_stages.update({item["stage"]: item["count"] for item in snapshot["setup_failure_stages"]})
    completed = success + failed
    return {
        "success": success, "failed": failed, "completed": completed,
        "success_rate": round(success / completed * 100, 1) if completed else None,
        "total_tasks": total_tasks, "active_tasks": active_tasks,
        "cancelled_tasks": cancelled_tasks, "interrupted_tasks": interrupted_tasks,
        "failure_stages": _stage_items(stages), "setup_failures": setup_failures,
        "setup_failure_stages": _stage_items(setup_stages),
    }
