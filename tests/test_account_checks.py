"""Batch account status/subscription check contract tests."""
from __future__ import annotations

import pytest

from application import account_checks as account_checks_module
from application import tasks as tasks_module
from core.base_platform import Account
from core.db import save_account


class _Logger:
    def __init__(self):
        self.result_data = None
        self.finished = None
        self.progress = []

    def set_progress(self, current, total=None):
        self.progress.append((current, total))

    def set_result_data(self, data):
        self.result_data = data

    def is_cancel_requested(self):
        return False

    def log(self, *args, **kwargs):
        return None

    def record_error(self, *args, **kwargs):
        return None

    def record_success(self):
        return None

    def finish(self, status, *, error=""):
        self.finished = (status, error)


def _create(email: str, platform: str = "chatgpt") -> int:
    model = save_account(
        Account(
            platform=platform,
            email=email,
            password="test-password",
            extra={"access_token": "test-access-token"},
        )
    )
    return int(model.id)


def test_check_service_rejects_selected_account_from_another_platform(monkeypatch):
    chatgpt_id = _create("chatgpt@example.com")
    cursor_id = _create("cursor@example.com", platform="cursor")
    monkeypatch.setattr(account_checks_module.task_runtime, "wake_up", lambda: None)

    captured = {}

    def fake_create(platform, *, account_ids=None, **kwargs):
        captured.update(platform=platform, account_ids=account_ids, kwargs=kwargs)
        return {"task_id": "task-check"}

    monkeypatch.setattr(account_checks_module, "create_account_check_all_task", fake_create)
    result = account_checks_module.AccountChecksService().check_all_async(
        "chatgpt", account_ids=[chatgpt_id, chatgpt_id]
    )

    assert result == {"task_id": "task-check"}
    assert captured == {"platform": "chatgpt", "account_ids": [chatgpt_id], "kwargs": {}}
    with pytest.raises(ValueError, match="不属于平台"):
        account_checks_module.AccountChecksService().check_all_async(
            "chatgpt", account_ids=[cursor_id]
        )


def test_batch_task_checks_all_accounts_without_the_old_fifty_row_limit(monkeypatch):
    for index in range(51):
        _create(f"batch-{index}@example.com")

    checked: list[int] = []

    def fake_check(account_id, logger):
        checked.append(account_id)
        return True, {"check_state": "valid"}

    monkeypatch.setattr(tasks_module, "_run_single_account_check", fake_check)
    logger = _Logger()
    tasks_module._execute_account_check_all_task(
        {"platform": "chatgpt", "ids": []}, logger
    )

    assert len(checked) == 51
    assert logger.result_data == {
        "total": 51,
        "checked": 51,
        "valid": 51,
        "invalid": 0,
        "error": 0,
    }
    assert logger.finished == (tasks_module.TASK_STATUS_SUCCEEDED, "")


def test_batch_check_api_accepts_selected_ids_and_keeps_query_compatibility(client, monkeypatch):
    captured = []

    def fake_check(platform, *, account_ids=None):
        captured.append((platform, account_ids))
        return {"task_id": "task-check"}

    monkeypatch.setattr("api.account_checks.service.check_all_async", fake_check)

    selected = client.post(
        "/api/accounts/check-all",
        json={"platform": "chatgpt", "ids": [3, 4]},
    )
    legacy = client.post(
        "/api/accounts/check-all?platform=cursor",
    )

    assert selected.status_code == 200
    assert legacy.status_code == 200
    assert captured == [("chatgpt", [3, 4]), ("cursor", [])]
