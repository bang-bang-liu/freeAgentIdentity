from core.base_platform import Account
from core.proxy_pool import proxy_pool
from platforms.chatgpt.plugin import ChatGPTPlatform


def test_chatgpt_actions_use_capability_ids():
    actions = ChatGPTPlatform().get_platform_actions()
    action_ids = {item["id"] for item in actions}

    assert "query_state" in action_ids
    assert "upload_cpa" in action_ids
    assert "switch_desktop" not in action_ids
    assert "upload_tm" not in action_ids
    assert "get_account_state" not in action_ids
    assert "switch_account" not in action_ids
    query_action = next(item for item in actions if item["id"] == "query_state")
    assert query_action["params"] == [{
        "key": "proxy",
        "label": "查询代理",
        "type": "proxy",
        "options": [
            "http://127.0.0.1:7897",
            "http://127.0.0.1:7890",
        ],
        "placeholder": "http://user:pass@host:port",
        "required": True,
    }]


def test_chatgpt_legacy_get_account_state_routes_to_query_state(monkeypatch):
    platform = ChatGPTPlatform()
    account = Account(platform="chatgpt", email="user@example.com", password="secret")
    calls = []

    def fake_query_state(received_account, params):
        calls.append((received_account, params))
        return {"ok": True, "data": {"valid": True}}

    monkeypatch.setattr(platform, "_handle_query_state", fake_query_state)

    result = platform.execute_action("get_account_state", account, {"source": "legacy"})

    assert result == {"ok": True, "data": {"valid": True}}
    assert calls == [(account, {"source": "legacy"})]


def test_chatgpt_capability_handlers_delegate_to_existing_actions(monkeypatch):
    platform = ChatGPTPlatform()
    account = Account(platform="chatgpt", email="user@example.com", password="secret")
    calls = []

    def fake_platform_action(action_id, received_account, params):
        calls.append((action_id, received_account, params))
        return {"ok": True, "data": action_id}

    monkeypatch.setattr(platform, "_execute_platform_action", fake_platform_action)

    assert platform.execute_action("upload_cpa", account, {"api_url": "https://example.com"})["data"] == "upload_cpa"
    assert [call[0] for call in calls] == ["upload_cpa"]


def test_chatgpt_query_state_uses_project_proxy_before_direct(monkeypatch):
    from platforms.chatgpt import switch

    calls = []
    proxy_events = []
    platform = ChatGPTPlatform()
    account = Account(platform="chatgpt", email="user@example.com", password="secret")
    account.extra = {"access_token": "token"}

    def fake_state(**kwargs):
        calls.append(kwargs["proxy"])
        return {"valid": kwargs["proxy"] == "http://127.0.0.1:7890"}

    monkeypatch.setattr(switch, "fetch_chatgpt_account_state", fake_state)
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": "http://127.0.0.1:7890")
    monkeypatch.setattr(proxy_pool, "report_success", lambda url: proxy_events.append(("success", url)))
    monkeypatch.setattr(proxy_pool, "report_fail", lambda url: proxy_events.append(("fail", url)))

    result = platform.execute_action("query_state", account, {})

    assert calls == ["http://127.0.0.1:7890"]
    assert proxy_events == [("success", "http://127.0.0.1:7890")]
    assert result["data"]["network_path"] == "project_proxy"
    assert result["data"]["check_state"] == "valid"
    assert "local_app_account" not in result["data"]
    assert "desktop_app_state" not in result["data"]


def test_chatgpt_query_state_uses_manual_proxy_without_fallback(monkeypatch):
    from platforms.chatgpt import switch

    calls = []
    proxy_events = []
    platform = ChatGPTPlatform()
    account = Account(platform="chatgpt", email="user@example.com", password="secret")
    account.extra = {"access_token": "token"}

    def fake_state(**kwargs):
        calls.append(kwargs["proxy"])
        return {"valid": True}

    monkeypatch.setattr(switch, "fetch_chatgpt_account_state", fake_state)
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": (_ for _ in ()).throw(AssertionError("pool should not be used")))
    monkeypatch.setattr(proxy_pool, "report_success", lambda url: proxy_events.append(("success", url)))
    monkeypatch.setattr(proxy_pool, "report_fail", lambda url: proxy_events.append(("fail", url)))

    result = platform.execute_action("query_state", account, {"proxy": "  http://127.0.0.1:8080  "})

    assert calls == ["http://127.0.0.1:8080"]
    assert proxy_events == []
    assert result["data"]["network_path"] == "manual_proxy"
    assert result["data"]["check_state"] == "valid"


def test_chatgpt_query_state_checks_eligible_checkout_with_same_proxy(monkeypatch):
    from platforms.chatgpt import subscription, switch

    checkout_calls = []
    platform = ChatGPTPlatform()
    account = Account(
        platform="chatgpt",
        email="user@example.com",
        password="secret",
        user_id="account-123",
        region="DE",
    )
    account.extra = {"access_token": "token", "cookies": "oai-did=device"}

    monkeypatch.setattr(
        switch,
        "fetch_chatgpt_account_state",
        lambda **kwargs: {
            "valid": True,
            "plus_trial_eligible": True,
            "plus_trial_check_state": "available",
        },
    )

    def fake_checkout(checkout_account, proxy=None):
        checkout_calls.append({
            "access_token": checkout_account.access_token,
            "account_id": checkout_account.account_id,
            "region": checkout_account.region,
            "with_promo": checkout_account.checkout_with_promo,
            "proxy": proxy,
        })
        return {
            "plus_trial_checkout_state": "available",
            "plus_trial_checkout_chain": "cs",
        }

    monkeypatch.setattr(subscription, "fetch_plus_trial_checkout_chain", fake_checkout)

    result = platform.execute_action("query_state", account, {"proxy": "http://127.0.0.1:8080"})

    assert checkout_calls == [{
        "access_token": "token",
        "account_id": "account-123",
        "region": "DE",
        "with_promo": True,
        "proxy": "http://127.0.0.1:8080",
    }]
    assert result["data"]["check_state"] == "valid"
    assert result["data"]["plus_trial_checkout_state"] == "available"
    assert result["data"]["plus_trial_checkout_chain"] == "cs"


def test_chatgpt_query_state_checks_checkout_when_promo_is_not_eligible(monkeypatch):
    from platforms.chatgpt import subscription, switch

    checkout_calls = []
    platform = ChatGPTPlatform()
    account = Account(
        platform="chatgpt",
        email="user@example.com",
        password="secret",
        region="GB",
    )
    account.extra = {"access_token": "token"}

    monkeypatch.setattr(
        switch,
        "fetch_chatgpt_account_state",
        lambda **kwargs: {
            "valid": True,
            "plus_trial_eligible": False,
            "plus_trial_check_state": "available",
        },
    )

    def fake_checkout(checkout_account, proxy=None):
        checkout_calls.append((checkout_account.checkout_with_promo, proxy))
        return {
            "plus_trial_checkout_state": "available",
            "plus_trial_checkout_chain": "oaics",
        }

    monkeypatch.setattr(subscription, "fetch_plus_trial_checkout_chain", fake_checkout)

    result = platform.execute_action("query_state", account, {"proxy": "http://127.0.0.1:8080"})

    assert checkout_calls == [(False, "http://127.0.0.1:8080")]
    assert result["data"]["plus_trial_eligible"] is False
    assert result["data"]["plus_trial_checkout_state"] == "available"
    assert result["data"]["plus_trial_checkout_chain"] == "oaics"


def test_chatgpt_query_state_checkout_failure_does_not_fail_state_query(monkeypatch):
    from platforms.chatgpt import subscription, switch

    platform = ChatGPTPlatform()
    account = Account(platform="chatgpt", email="user@example.com", password="secret")
    account.extra = {"access_token": "token"}

    monkeypatch.setattr(
        switch,
        "fetch_chatgpt_account_state",
        lambda **kwargs: {"valid": True, "plus_trial_eligible": True},
    )
    monkeypatch.setattr(
        subscription,
        "fetch_plus_trial_checkout_chain",
        lambda account, proxy=None: (_ for _ in ()).throw(RuntimeError("checkout unavailable")),
    )
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)

    result = platform.execute_action("query_state", account, {})

    assert result["ok"] is True
    assert result["data"]["check_state"] == "valid"
    assert result["data"]["plus_trial_checkout_chain"] is None
    assert result["data"]["plus_trial_checkout_state"] == "unavailable"
    assert "checkout unavailable" in result["data"]["plus_trial_checkout_error"]


def test_chatgpt_query_state_network_failure_is_not_invalid(monkeypatch):
    from platforms.chatgpt import switch

    platform = ChatGPTPlatform()
    account = Account(platform="chatgpt", email="user@example.com", password="secret")
    account.extra = {"access_token": "token"}

    monkeypatch.setattr(
        switch,
        "fetch_chatgpt_account_state",
        lambda **kwargs: {"valid": False, "profile_error": {"error": "timeout"}},
    )
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)

    result = platform.execute_action("query_state", account, {})

    assert "valid" not in result["data"]
    assert result["data"]["check_state"] == "unavailable"
    assert result["data"]["network_path"] == "direct"


def test_chatgpt_query_state_rejected_token_is_not_an_account_invalid(monkeypatch):
    from platforms.chatgpt import switch

    platform = ChatGPTPlatform()
    account = Account(platform="chatgpt", email="user@example.com", password="secret")
    account.extra = {"access_token": "token"}

    monkeypatch.setattr(
        switch,
        "fetch_chatgpt_account_state",
        lambda **kwargs: {
            "valid": False,
            "profile_error": {"status_code": 401, "body": "token rejected"},
        },
    )
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)

    result = platform.execute_action("query_state", account, {})

    assert "valid" not in result["data"]
    assert result["data"]["check_state"] == "credential_invalid"


def test_chatgpt_query_state_forbidden_is_retryable_not_a_credential_failure(monkeypatch):
    from platforms.chatgpt import switch

    platform = ChatGPTPlatform()
    account = Account(platform="chatgpt", email="user@example.com", password="secret")
    account.extra = {"access_token": "token"}

    monkeypatch.setattr(
        switch,
        "fetch_chatgpt_account_state",
        lambda **kwargs: {
            "valid": False,
            "profile_error": {"status_code": 403, "body": "access denied"},
        },
    )
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)

    result = platform.execute_action("query_state", account, {})

    assert "valid" not in result["data"]
    assert result["data"]["check_state"] == "unavailable"
