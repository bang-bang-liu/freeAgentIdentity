from __future__ import annotations

import json

from sqlmodel import Session, select

from application.tasks import _run_single_account_check
from core.account_graph import patch_account_graph
from core.base_platform import RegisterConfig
from core.db import AccountModel, AccountOverviewModel, engine
from core.lifecycle import check_accounts_validity
from core.proxy_pool import proxy_pool
from platforms.chatgpt import subscription
from platforms.chatgpt.plugin import ChatGPTPlatform


class _AlwaysValidPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return True


class _AlwaysInvalidPlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return False


class _UnavailablePlatform:
    def __init__(self, config: RegisterConfig | None = None):
        self.config = config

    def check_valid(self, account) -> bool:
        return False

    def get_last_check_overview(self) -> dict:
        return {
            "check_state": "unavailable",
            "check_error": "connection timed out",
            "network_path": "direct",
        }


def _create_account(*, platform: str = "chatgpt", lifecycle_status: str = "registered") -> int:
    with Session(engine) as session:
        model = AccountModel(platform=platform, email=f"{platform}@example.com", password="secret")
        session.add(model)
        session.commit()
        session.refresh(model)
        patch_account_graph(
            session,
            model,
            lifecycle_status=lifecycle_status,
            summary_updates={"valid": lifecycle_status != "invalid"},
        )
        session.commit()
        return int(model.id or 0)


def _overview(account_id: int):
    with Session(engine) as session:
        return session.exec(
            select(AccountOverviewModel).where(AccountOverviewModel.account_id == account_id)
        ).one()


def test_single_account_check_recovers_previously_invalid_account(monkeypatch):
    account_id = _create_account(lifecycle_status="invalid")
    monkeypatch.setattr("application.tasks.get", lambda _platform: _AlwaysValidPlatform)

    valid, result = _run_single_account_check(account_id)

    assert valid is True
    assert result["valid"] is True
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "valid"
    assert overview.display_status == "registered"
    assert overview.checked_at


def test_lifecycle_validity_check_does_not_overwrite_lifecycle_status(monkeypatch):
    account_id = _create_account(lifecycle_status="registered")
    monkeypatch.setattr("core.lifecycle.get", lambda _platform: _AlwaysInvalidPlatform)

    results = check_accounts_validity(platform="chatgpt", limit=10)

    assert results["invalid"] == 1
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "invalid"
    assert overview.display_status == "invalid"
    assert overview.checked_at


def test_single_check_network_failure_keeps_validity_unknown(monkeypatch):
    account_id = _create_account(lifecycle_status="registered")
    monkeypatch.setattr("application.tasks.get", lambda _platform: _UnavailablePlatform)

    valid, result = _run_single_account_check(account_id)

    assert valid is False
    assert result["check_state"] == "unavailable"
    overview = _overview(account_id)
    assert overview.lifecycle_status == "registered"
    assert overview.validity_status == "unknown"
    assert overview.get_summary()["check_error"] == "connection timed out"


def test_lifecycle_network_failure_is_counted_as_check_error(monkeypatch):
    account_id = _create_account(lifecycle_status="registered")
    monkeypatch.setattr("core.lifecycle.get", lambda _platform: _UnavailablePlatform)

    results = check_accounts_validity(platform="chatgpt", limit=10)

    assert results["valid"] == 0
    assert results["invalid"] == 0
    assert results["error"] == 1
    assert _overview(account_id).validity_status == "unknown"


def test_single_check_rejected_token_keeps_account_validity_unknown(monkeypatch):
    account_id = _create_account(lifecycle_status="registered")

    class _RejectedTokenPlatform(_UnavailablePlatform):
        def get_last_check_overview(self) -> dict:
            return {"check_state": "credential_invalid", "check_error": "token rejected"}

    monkeypatch.setattr("application.tasks.get", lambda _platform: _RejectedTokenPlatform)

    valid, result = _run_single_account_check(account_id)

    assert valid is False
    assert result["check_state"] == "credential_invalid"
    assert _overview(account_id).validity_status == "unknown"


def test_chatgpt_subscription_status_falls_back_to_wham_usage(monkeypatch):
    captured_headers: dict[str, str] = {}

    class _Resp:
        def __init__(self, data=None, error: Exception | None = None):
            self._data = data
            self._error = error

        def raise_for_status(self):
            if self._error:
                raise self._error

        def json(self):
            return self._data

    def _fake_get(url, **kwargs):
        if url.endswith("/backend-api/me"):
            return _Resp(error=RuntimeError("403"))
        captured_headers.update(kwargs.get("headers") or {})
        return _Resp(data={"plan_type": "free"})

    monkeypatch.setattr(subscription.requests, "get", _fake_get)
    account = type(
        "AccountStub",
        (),
        {
            "access_token": "token",
            "cookies": "",
            "id_token": json.dumps({"chatgpt_account_id": "acct-123"}),
            "extra": {},
        },
    )()

    status = subscription.check_subscription_status(account)

    assert status == "free"
    assert captured_headers["Authorization"] == "Bearer token"
    assert captured_headers["Chatgpt-Account-Id"] == "acct-123"


def test_chatgpt_plus_trial_eligibility_uses_bearer_and_proxy(monkeypatch):
    captured: dict = {}

    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "plan_type": "free",
                "subscription_plan": "chatgptfreeplan",
                "eligible_promo_campaigns": {"plus": True},
            }

    def _fake_get(url, **kwargs):
        captured["url"] = url
        captured.update(kwargs)
        return _Resp()

    monkeypatch.setattr(subscription.requests, "get", _fake_get)
    account = type("AccountStub", (), {"access_token": "token"})()

    result = subscription.fetch_plus_trial_eligibility(account, proxy="http://proxy:8080")

    assert result["plus_trial_eligible"] is True
    assert result["plus_trial_check_state"] == "available"
    assert captured["url"].endswith("/backend-api/accounts/check/v4-2023-04-27")
    assert captured["params"]["timezone_offset_min"].lstrip("-").isdigit()
    assert captured["headers"]["Authorization"] == "Bearer token"
    assert captured["proxies"] == {"http": "http://proxy:8080", "https": "http://proxy:8080"}


def test_chatgpt_plus_trial_retries_403_with_browser_cookies(monkeypatch):
    calls: list[dict] = []

    class _Resp:
        def __init__(self, status_code, data=None):
            self.status_code = status_code
            self._data = data

        def raise_for_status(self):
            if self.status_code >= 400:
                raise RuntimeError(f"HTTP Error {self.status_code}")

        def json(self):
            return self._data

    def _fake_get(_url, **kwargs):
        calls.append(kwargs)
        if len(calls) < 3:
            return _Resp(403)
        return _Resp(200, {
            "plan_type": "free",
            "eligible_promo_campaigns": {"plus": True},
        })

    monkeypatch.setattr(subscription.requests, "get", _fake_get)
    account = type(
        "AccountStub",
        (),
        {"access_token": "token", "cookies": "oai-did=device; foo=bar"},
    )()

    result = subscription.fetch_plus_trial_eligibility(account)

    assert result["plus_trial_eligible"] is True
    assert len(calls) == 3
    assert calls[0]["params"]["timezone_offset_min"].lstrip("-").isdigit()
    assert calls[1]["params"] == {}
    assert calls[2]["cookies"]["oai-did"] == "device"


def test_chatgpt_plus_trial_eligibility_is_false_for_paid_account(monkeypatch):
    class _Resp:
        status_code = 200

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "plan_type": "plus",
                "subscription_plan": "chatgptplusplan",
                "eligible_promo_campaigns": {"plus": True},
            }

    monkeypatch.setattr(subscription.requests, "get", lambda *_args, **_kwargs: _Resp())
    account = type("AccountStub", (), {"access_token": "token"})()

    result = subscription.fetch_plus_trial_eligibility(account)

    assert result["plus_trial_eligible"] is False


def test_chatgpt_plus_trial_failure_does_not_fail_subscription_status(monkeypatch):
    class _Resp:
        status_code = 200

        def __init__(self, data):
            self._data = data

        def raise_for_status(self):
            return None

        def json(self):
            return self._data

    def _fake_get(url, **_kwargs):
        if url.endswith("/backend-api/me"):
            return _Resp({"plan_type": "free"})
        if url.endswith("/backend-api/wham/usage"):
            return _Resp({"plan_type": "free"})
        raise RuntimeError("promotion endpoint unavailable")

    monkeypatch.setattr(subscription.requests, "get", _fake_get)
    account = type("AccountStub", (), {"access_token": "token", "extra": {}})()

    details = subscription.fetch_subscription_status_details(account, proxy="http://proxy:8080")

    assert details["status"] == "free"
    assert details["plus_trial_eligible"] is None
    assert details["plus_trial_check_state"] == "unavailable"
    assert "promotion endpoint unavailable" in details["plus_trial_error"]


def test_chatgpt_check_valid_uses_proxy_pool_before_direct(monkeypatch):
    calls: list[str | None] = []
    proxy_events: list[tuple[str, str]] = []

    def _fake_status(account, proxy=None):
        calls.append(proxy)
        if proxy != "http://127.0.0.1:7890":
            raise RuntimeError("should use proxy first")
        return {
            "status": "free",
            "source": "backend-api/wham/usage",
            "usage": {"plan_type": "free"},
        }

    monkeypatch.setattr(subscription, "fetch_subscription_status_details", _fake_status)
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": "http://127.0.0.1:7890")
    monkeypatch.setattr(proxy_pool, "report_success", lambda url: proxy_events.append(("success", url)))
    monkeypatch.setattr(proxy_pool, "report_fail", lambda url: proxy_events.append(("fail", url)))

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig()
    plugin.mailbox = None
    account = type(
        "AccountStub",
        (),
        {
            "token": "token",
            "region": "",
            "extra": {
                "access_token": "token",
                "id_token": "",
                "cookies": "",
            },
        },
    )()

    assert plugin.check_valid(account) is True
    assert calls == ["http://127.0.0.1:7890"]
    assert proxy_events == [("success", "http://127.0.0.1:7890")]
    assert plugin.get_last_check_overview()["chatgpt_usage"] == {"plan_type": "free"}


def test_chatgpt_check_valid_surfaces_plus_trial_eligibility(monkeypatch):
    monkeypatch.setattr(
        subscription,
        "fetch_subscription_status_details",
        lambda account, proxy=None: {
            "status": "free",
            "source": "backend-api/me",
            "plus_trial_eligible": True,
            "plus_trial_check_state": "available",
        },
    )
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)

    plugin = ChatGPTPlatform.__new__(ChatGPTPlatform)
    plugin.config = RegisterConfig()
    plugin.mailbox = None
    account = type(
        "AccountStub",
        (),
        {"token": "token", "region": "", "extra": {"access_token": "token"}},
    )()

    assert plugin.check_valid(account) is True
    overview = plugin.get_last_check_overview()
    assert overview["plus_trial_eligible"] is True
    assert "带Plus试用" in overview["chips"]
