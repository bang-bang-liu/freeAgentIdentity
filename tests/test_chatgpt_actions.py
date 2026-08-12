from core.base_platform import Account
from core.proxy_pool import proxy_pool
from platforms.chatgpt.plugin import ChatGPTPlatform


def test_chatgpt_actions_use_capability_ids():
    action_ids = {item["id"] for item in ChatGPTPlatform().get_platform_actions()}

    assert "query_state" in action_ids
    assert "switch_desktop" in action_ids
    assert "get_account_state" not in action_ids
    assert "switch_account" not in action_ids


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

    assert platform.execute_action("switch_account", account, {})["data"] == "switch_desktop"
    assert platform.execute_action("upload_cpa", account, {"api_url": "https://example.com"})["data"] == "upload_cpa"
    assert platform.execute_action("upload_tm", account, {"api_url": "https://example.com"})["data"] == "upload_tm"
    assert [call[0] for call in calls] == ["switch_desktop", "upload_cpa", "upload_tm"]


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
    monkeypatch.setattr(switch, "read_current_codex_account", lambda: {})
    monkeypatch.setattr(switch, "get_codex_desktop_state", lambda: {})
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": "http://127.0.0.1:7890")
    monkeypatch.setattr(proxy_pool, "report_success", lambda url: proxy_events.append(("success", url)))
    monkeypatch.setattr(proxy_pool, "report_fail", lambda url: proxy_events.append(("fail", url)))

    result = platform.execute_action("query_state", account, {})

    assert calls == ["http://127.0.0.1:7890"]
    assert proxy_events == [("success", "http://127.0.0.1:7890")]
    assert result["data"]["network_path"] == "project_proxy"
    assert result["data"]["check_state"] == "valid"


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
    monkeypatch.setattr(switch, "read_current_codex_account", lambda: {})
    monkeypatch.setattr(switch, "get_codex_desktop_state", lambda: {})
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
    monkeypatch.setattr(switch, "read_current_codex_account", lambda: {})
    monkeypatch.setattr(switch, "get_codex_desktop_state", lambda: {})
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
    monkeypatch.setattr(switch, "read_current_codex_account", lambda: {})
    monkeypatch.setattr(switch, "get_codex_desktop_state", lambda: {})
    monkeypatch.setattr(proxy_pool, "get_next", lambda region="": None)

    result = platform.execute_action("query_state", account, {})

    assert "valid" not in result["data"]
    assert result["data"]["check_state"] == "unavailable"
