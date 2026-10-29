"""刷新登录功能集成测试。

设计依据：docs/CHATGPT_LOGIN_REFRESH_DESIGN.md 13.1。
测试范围：任务创建、预检、账号锁定、凭据持久化逻辑。
不测试真实网络登录（那属于 Phase 1 的 test_chatgpt_login_client.py）。
"""
import json
from unittest.mock import MagicMock, patch

import pytest
from sqlmodel import Session, select

from application.login_refresh import (
    _execute_login_refresh_task,
    create_login_refresh_task,
    preview_login_refresh,
    _persist_credentials,
)
from application.login_refresh_types import (
    LoginRefreshAccountResult,
    LoginRefreshPreview,
)
from application.tasks import (
    TASK_STATUS_CANCELLED,
    TASK_TYPE_LOGIN_REFRESH,
    _task_account_keys,
)
from core.db import AccountModel, engine
from platforms.chatgpt.login_types import (
    ChatGPTLoginError,
    ChatGPTLoginResult,
    LoginErrorCode,
)


def test_task_account_keys_extracts_all_account_ids():
    """任务账号锁定键提取。"""
    payload = {"account_ids": [10, 20, 30]}
    keys = _task_account_keys(TASK_TYPE_LOGIN_REFRESH, payload)
    assert keys == ["account:10", "account:20", "account:30"]


def test_task_account_keys_filters_invalid_ids():
    """过滤非法账号 ID。"""
    payload = {"account_ids": [10, "invalid", None, 20]}
    keys = _task_account_keys(TASK_TYPE_LOGIN_REFRESH, payload)
    assert keys == ["account:10", "account:20"]


def test_task_account_keys_returns_empty_for_missing_field():
    """缺少 account_ids 字段时返回空列表。"""
    keys = _task_account_keys(TASK_TYPE_LOGIN_REFRESH, {})
    assert keys == []


def test_create_task_validates_empty_account_ids():
    """拒绝空账号列表。"""
    with pytest.raises(ValueError, match="account_ids 不能为空"):
        create_login_refresh_task(
            platform="chatgpt",
            account_ids=[],
        )


def test_create_task_validates_max_100_accounts():
    """单次任务最多 100 个账号。"""
    with pytest.raises(ValueError, match="单次任务账号数量不能超过 100"):
        create_login_refresh_task(
            platform="chatgpt",
            account_ids=list(range(101)),
        )


def test_create_task_deduplicates_account_ids():
    """任务创建时去重账号 ID。"""
    with patch("application.login_refresh.create_task") as mock_create_task:
        with patch("application.tasks.serialize_task") as mock_serialize:
            mock_task = {
                "task_id": "test-task-id",
                "status": "pending",
                "progress_total": 2,
                "progress_current": 0,
            }
            mock_create_task.return_value = mock_task
            mock_serialize.return_value = mock_task

            result = create_login_refresh_task(
                platform="chatgpt",
                account_ids=[1, 2, 1, 2, 1],
            )

            # 验证传给 create_task 的 payload 已去重
            assert mock_create_task.called
            call_args = mock_create_task.call_args
            payload = call_args.kwargs["payload"]
            assert payload["account_ids"] == [1, 2]


def test_cancelled_login_error_finishes_task_as_cancelled(monkeypatch):
    class FakeLogger:
        task_id = "task-login-refresh-cancel"

        def __init__(self):
            self.finished = []
            self.result_data = None

        def mark_running(self):
            return None

        def is_cancel_requested(self):
            return False

        def log(self, _message, **_kwargs):
            return None

        def set_progress(self, *_args):
            return None

        def set_result_data(self, data):
            self.result_data = data

        def finish(self, status, *, error=""):
            self.finished.append((status, error))

    def cancelled_refresh(**_kwargs):
        raise ChatGPTLoginError(LoginErrorCode.CANCELLED, "任务已取消")

    monkeypatch.setattr(
        "application.login_refresh._refresh_single_account",
        cancelled_refresh,
    )
    logger = FakeLogger()

    _execute_login_refresh_task(
        {
            "platform": "chatgpt",
            "account_ids": [101],
            "concurrency": 1,
        },
        logger,
    )

    assert logger.finished[-1][0] == TASK_STATUS_CANCELLED
    assert logger.result_data["failed"] == 0
    assert logger.result_data["succeeded"] == 0


def test_completed_account_is_kept_when_cancel_arrives_before_result_is_consumed(
    monkeypatch,
):
    class FakeLogger:
        task_id = "task-login-refresh-late-cancel"

        def __init__(self):
            self.cancelled = False
            self.finished = []
            self.result_data = None

        def mark_running(self):
            return None

        def is_cancel_requested(self):
            return self.cancelled

        def log(self, _message, **_kwargs):
            return None

        def set_progress(self, *_args):
            return None

        def set_result_data(self, data):
            self.result_data = data

        def finish(self, status, *, error=""):
            self.finished.append((status, error))

    logger = FakeLogger()

    def successful_refresh(**kwargs):
        logger.cancelled = True
        return LoginRefreshAccountResult(
            account_id=kwargs["account_id"],
            email="u***@example.com",
            status="succeeded",
        )

    monkeypatch.setattr(
        "application.login_refresh._refresh_single_account",
        successful_refresh,
    )

    _execute_login_refresh_task(
        {
            "platform": "chatgpt",
            "account_ids": [101, 102],
            "concurrency": 1,
        },
        logger,
    )

    assert logger.finished[-1][0] == TASK_STATUS_CANCELLED
    assert logger.result_data["succeeded"] == 1
    assert len(logger.result_data["items"]) == 1
    assert logger.result_data["items"][0]["account_id"] == 101
    assert logger.result_data["items"][0]["status"] == "succeeded"



def test_preview_identifies_password_totp_and_manual_modes():
    from core.account_graph import patch_account_graph
    with Session(engine) as session:
        accounts = [AccountModel(platform="chatgpt", email="password@example.com", password="saved"),
                    AccountModel(platform="chatgpt", email="manual@example.com", password="")]
        session.add_all(accounts)
        session.commit()
        ids = [a.id for a in accounts]
        patch_account_graph(session, accounts[0], credential_updates={"totp_secret": "SAVEDSECRET"})
        session.commit()
    preview = preview_login_refresh(platform="chatgpt", account_ids=ids)
    modes = {item.email: item.refresh_mode for item in preview.items}
    assert modes == {"password@example.com": "password_totp", "manual@example.com": "manual_required"}
    assert preview.can_start and all(item.ready for item in preview.items)


def test_persist_credentials_updates_password_and_tokens():
    from core.account_graph import load_account_graphs, patch_account_graph
    with Session(engine) as session:
        account = AccountModel(platform="chatgpt", email="test@example.com", password="old")
        session.add(account)
        session.commit()
        patch_account_graph(session, account, credential_updates={
            "legacy_token": "old-legacy-token", "accessToken": "old-camel-token",
            "sessionToken": "old-camel-session",
        })
        session.commit()
        login = ChatGPTLoginResult(
            access_token="fresh-token", session_token="fresh-session", cookie_header="new-cookies",
            account_id="new-account-id", login_method="password_totp", token_verified=True,
            verified_email=account.email, verified_user_id="user-test",
            access_token_expires_at="2026-11-01T00:00:00Z",
        )
        _persist_credentials(account_id=account.id, account_email=account.email, password="saved-password",
                             totp_secret="KEPTSECRET", login_result=login, password_created=False,
                             totp_created=False, security_setup_status="skipped", session=session)
        graph = load_account_graphs(session, [account.id])[account.id]
        creds = {c["key"]: c["value"] for c in graph["credentials"]}
        assert creds["access_token"] == "fresh-token" and creds["session_token"] == "fresh-session"
        assert creds["legacy_token"] == creds["accessToken"] == "fresh-token"
        assert creds["sessionToken"] == "fresh-session"
        assert creds["cookies"] == "new-cookies" and creds["account_id"] == "new-account-id"
        assert creds["totp_secret"] == "KEPTSECRET"
        assert account.password == "saved-password" and account.user_id == "new-account-id"
        assert graph["validity_status"] == "valid"
        assert graph["overview"]["auth"]["last_login_status"] == "succeeded"
        assert graph["overview"]["login_required"] is False


@pytest.mark.parametrize("failure", ["revoked", "cancelled", "password", "unverified"])
def test_failed_login_preserves_credentials_and_records_reason(monkeypatch, failure):
    from core.account_graph import patch_account_graph, load_account_graphs
    from application.login_refresh import _refresh_single_account
    with Session(engine) as session:
        account = AccountModel(platform="chatgpt", email="test@example.com", password="old-password")
        session.add(account)
        session.commit()
        aid = account.id
        patch_account_graph(session, account, primary_token="old-token", credential_updates={
            "session_token": "old-session", "cookies": "old-cookies", "totp_secret": "OLDSECRET",
        })
        session.commit()
    code = {"revoked": LoginErrorCode.CREDENTIAL_REVOKED, "cancelled": LoginErrorCode.CANCELLED,
            "password": LoginErrorCode.INVALID_PASSWORD, "unverified": LoginErrorCode.CREDENTIAL_INVALID}[failure]
    def login(**_kwargs):
        if failure == "unverified": return ChatGPTLoginResult(access_token="unverified")
        raise ChatGPTLoginError(code, "fixture reason")
    monkeypatch.setattr("application.login_refresh.login_chatgpt_web", login)
    logger = MagicMock()
    logger.task_id = "fixture-task"
    logger.is_cancel_requested.return_value = False
    result = _refresh_single_account(account_id=aid, platform="chatgpt", proxy="http://127.0.0.1:7897",
                                     setup_security=False, email_otp_mode="manual", logger=logger)
    assert result.error_code == code
    assert result.status == ("cancelled" if failure == "cancelled" else "failed")
    with Session(engine) as session:
        graph = load_account_graphs(session, [aid])[aid]
        creds = {c["key"]: c["value"] for c in graph["credentials"]}
        assert creds["access_token"] == "old-token" and creds["session_token"] == "old-session"
        assert creds["cookies"] == "old-cookies" and creds["totp_secret"] == "OLDSECRET"
        assert session.get(AccountModel, aid).password == "old-password"
        assert graph["overview"]["auth"]["last_login_error_code"] == code


def test_credential_updates_excludes_empty_values():
    """LoginResult.credential_updates() 只返回非空凭据。"""
    result = ChatGPTLoginResult(
        access_token="token123",
        session_token="",
        refresh_token="",
        id_token="",
        cookie_header="cookie123",
        auth_session_json="",
        device_id="device123",
        account_id="acc123",
        login_method="password",
        access_token_expires_at="2026-09-10T00:00:00Z",
    )
    updates = result.credential_updates()
    # 空值不应该出现在返回的字典中
    assert "session_token" not in updates or updates["session_token"] != ""
    # 非空值应该出现
    assert updates["access_token"] == "token123"
    assert updates["cookies"] == "cookie123"
    assert updates["device_id"] == "device123"
    # 空值字段不应该出现
    assert "refresh_token" not in updates
    assert "id_token" not in updates


def test_task_query_exposes_persisted_login_results(client):
    from core.db import TaskModel
    data = {"items": [{"account_id": 1, "status": "succeeded", "token_verified": True,
                        "email": "target@example.com", "login_method": "password_totp"}]}
    with Session(engine) as session:
        task = TaskModel(id="login-query-test", type="login_refresh", platform="chatgpt", status="succeeded")
        task.set_result({"data": data})
        session.add(task)
        session.commit()
    response = client.get('/api/tasks/login-query-test')
    assert response.status_code == 200
    assert response.json()["data"] == data
    assert response.json()["result"]["data"] == data
