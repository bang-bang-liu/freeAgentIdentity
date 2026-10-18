"""Outcome accounting uses isolated database rows and mocked registration only."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlmodel import Session

from api.tasks import router
from application import tasks as tasks_module
from application.tasks_query import TasksQueryService
from core.db import TaskModel, engine
from domain.registration_stats import observe_registration_stage, registration_snapshot


def _task(task_id: str, **overrides) -> TaskModel:
    result = overrides.pop("result", {})
    task = TaskModel(id=task_id, type="register", platform="chatgpt", status="succeeded")
    for key, value in overrides.items():
        setattr(task, key, value)
    task.set_result(result)
    with Session(engine) as session:
        session.add(task)
        session.commit()
    return task


def _client() -> TestClient:
    # No main.py lifespan/dispatcher: fixture tasks can never reach a real provider.
    app = FastAPI()
    app.include_router(router, prefix="/api")
    return TestClient(app)


def test_empty_history_has_zero_counts_and_no_fabricated_success_rate():
    with _client() as client:
        response = client.get("/api/tasks/register/stats")
    assert response.status_code == 200
    data = response.json()
    assert data["success"] == data["failed"] == data["total_tasks"] == 0
    assert data["success_rate"] is None
    assert data["failure_stages"] == []


def test_history_counts_outcomes_not_batch_status_and_filters_scope():
    _task("mixed", success_count=3, error_count=2, result={"failure_stages": {"verification": 2}})
    _task("legacy", status="failed", error_count=1, result={"errors": ["private fixture credential"]})
    _task("cancelled", status="cancelled", success_count=1, progress_total=10, progress_current=1)
    _task("old", created_at=datetime.now(timezone.utc) - timedelta(days=45), success_count=8)
    _task("check", type="account_check_all", success_count=99, error_count=99)
    _task("other-platform", platform="other", success_count=99)
    with _client() as client:
        data = client.get("/api/tasks/register/stats?days=30").json()
        all_time = client.get("/api/tasks/register/stats?days=0").json()
    assert (data["success"], data["failed"], data["completed"]) == (4, 3, 7)
    assert data["total_tasks"] == 3
    assert data["cancelled_tasks"] == 1
    assert data["success_rate"] == 57.1
    assert data["failure_stages"] == [{"stage": "verification", "count": 2}, {"stage": "unknown", "count": 1}]
    assert all_time["success"] == 12
    assert "private fixture credential" not in str(data)


def test_setup_aborts_are_separate_from_attempts_and_cancellation():
    _task("setup", status="failed", progress_total=5, error="邮箱初始化失败: fixture unavailable", result={"setup_failure_stage": "mailbox"})
    _task("cancelled-before-start", status="cancelled", progress_total=5)
    _task("interrupted", status="interrupted", progress_total=10)
    with _client() as client:
        data = client.get("/api/tasks/register/stats").json()
    assert data["success"] == data["failed"] == 0
    assert data["setup_failures"] == 1
    assert data["setup_failure_stages"] == [{"stage": "mailbox", "count": 1}]
    assert data["cancelled_tasks"] == data["interrupted_tasks"] == 1


def test_task_snapshot_preserves_stages_when_result_data_is_written():
    _task("snapshot", status="running", progress_total=4)
    logger = tasks_module.TaskLogger("snapshot")
    logger.set_subtask("worker_1")
    logger.log("[registration:verification] 邮箱验证")
    logger.record_error("fixture request timed out")
    logger.record_success()
    logger.set_result_data({"success": 1, "fail": 1, "accounts": []})
    task = TasksQueryService().get_task("snapshot")
    snapshot = task["registration_stats"]
    assert snapshot["failure_stages"] == [{"stage": "verification", "count": 1}]
    assert (snapshot["success"], snapshot["failed"], snapshot["remaining"]) == (1, 1, 2)
    assert task["result"]["data"] == {"success": 1, "fail": 1, "accounts": []}


def test_concurrent_workers_keep_independent_stages_and_atomic_totals():
    _task("concurrent", status="running", progress_total=12)
    logger = tasks_module.TaskLogger("concurrent")

    def fail(index):
        logger.set_subtask(f"worker_{index}")
        stage = "verification" if index % 2 else "credentials"
        logger.log(f"[registration:{stage}] fixture stage")
        logger.record_error("fixture error")
        logger.clear_subtask()

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(fail, range(12)))
    snapshot = TasksQueryService().get_task("concurrent")["registration_stats"]
    assert snapshot["failed"] == 12
    assert snapshot["failure_stages"] == [{"stage": "verification", "count": 6}, {"stage": "credentials", "count": 6}]


def test_late_cancellation_errors_do_not_increment_registration_failures():
    _task("cancel-race", status="cancel_requested", error_count=1, result={"failure_stages": {"profile": 1}})
    tasks_module.TaskLogger("cancel-race").record_error("request closed by cancellation", stage="credentials")
    snapshot = TasksQueryService().get_task("cancel-race")["registration_stats"]
    assert snapshot["failed"] == 1
    assert snapshot["failure_stages"] == [{"stage": "profile", "count": 1}]


def test_security_substeps_and_explicit_transitions_are_distinguished():
    assert observe_registration_stage("等待邮箱验证码", "security") == "security"
    assert observe_registration_stage("[registration:credentials] 获取最终凭据", "security") == "credentials"
    assert observe_registration_stage("a generic request failed", "profile") == "profile"


def test_unknown_stages_and_malformed_stage_totals_stay_consistent():
    snapshot = registration_snapshot(success=0, failed=3, result={"failure_stages": {"verification": 2, "unrecognized": 2, "profile": -1}}, status="failed", requested=5)
    assert sum(item["count"] for item in snapshot["failure_stages"]) == 3
    assert snapshot["failure_stages"] == [{"stage": "verification", "count": 2}, {"stage": "unknown", "count": 1}]
    assert snapshot["unattempted"] == 2
    assert snapshot["success_rate"] == 0


def test_statistics_period_is_validated():
    with _client() as client:
        assert client.get("/api/tasks/register/stats?days=-1").status_code == 422
