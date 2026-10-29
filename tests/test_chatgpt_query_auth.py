import json

import pytest

from core.base_platform import Account
from platforms.chatgpt import subscription, switch
from platforms.chatgpt.plugin import ChatGPTPlatform


class _Response:
    def __init__(self, status_code, data):
        self.status_code = status_code
        self.data = data
        self.text = json.dumps(data)

    def json(self):
        return self.data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP Error {self.status_code}: ")


def _guest_profile():
    return _Response(200, {"id": "ua-guest", "email": "", "name": "", "object": ""})


def _revoked_token():
    return _Response(401, {
        "error": {
            "code": "token_revoked",
            "message": "Your access token has been invalidated. Please try signing in again.",
        },
    })


def test_guest_cookie_profile_does_not_hide_revoked_bearer(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        if kwargs["headers"].get("authorization"):
            return _revoked_token()
        return _guest_profile()

    monkeypatch.setattr(switch.curl_requests, "get", fake_get)

    state = switch.fetch_chatgpt_account_state(
        access_token="revoked-token",
        cookies="__Secure-next-auth.session-token=session; oai-did=device",
    )

    assert state["valid"] is False
    assert "profile" not in state
    assert state["profile_error"]["status_code"] == 401
    assert state["profile_error"]["error_code"] == "token_revoked"
    assert "重新登录" in state["profile_error"]["error"]
    assert len(calls) == 2
    assert "cookies" not in calls[1]


def test_guest_cookie_profile_falls_back_to_authenticated_bearer(monkeypatch):
    calls = []

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        if kwargs["headers"].get("authorization"):
            return _Response(200, {"id": "user-authenticated", "email": "user@example.com"})
        return _guest_profile()

    monkeypatch.setattr(switch.curl_requests, "get", fake_get)
    monkeypatch.setattr(
        subscription,
        "fetch_subscription_status_details",
        lambda *args, **kwargs: {"status": "free"},
    )

    state = switch.fetch_chatgpt_account_state(
        access_token="valid-token",
        cookies="oai-did=device",
    )

    assert state["valid"] is True
    assert state["profile_auth_method"] == "access_token"
    assert state["profile"]["email"] == "user@example.com"
    assert len(calls) == 2


def test_guest_cookie_profile_without_bearer_is_not_valid(monkeypatch):
    monkeypatch.setattr(switch.curl_requests, "get", lambda *args, **kwargs: _guest_profile())

    state = switch.fetch_chatgpt_account_state(cookies="oai-did=device")

    assert state["valid"] is False
    assert state["profile_error"]["error_code"] == "anonymous_profile"


def test_query_state_reports_guest_profile_as_missing_login(monkeypatch):
    monkeypatch.setattr(switch.curl_requests, "get", lambda *args, **kwargs: _guest_profile())
    account = Account(
        platform="chatgpt",
        email="user@example.com",
        password="secret",
        extra={"cookies": "oai-did=device"},
    )

    result = ChatGPTPlatform().execute_action(
        "query_state", account, {"proxy": "http://proxy:8080", "check_chain": False},
    )

    assert result["data"]["check_state"] == "credential_invalid"
    assert "valid" not in result["data"]
    assert "重新登录" in result["data"]["check_error"]


@pytest.mark.parametrize("fetch", [
    subscription.fetch_plus_trial_eligibility,
    subscription.fetch_subscription_status_details,
])
def test_revoked_token_queries_explain_login_recovery(monkeypatch, fetch):
    monkeypatch.setattr(subscription.requests, "get", lambda *args, **kwargs: _revoked_token())
    account = type("AccountStub", (), {"access_token": "revoked-token", "cookies": ""})()

    with pytest.raises(RuntimeError) as exc:
        fetch(account, proxy="http://proxy:8080")

    assert "401" in str(exc.value)
    assert "token_revoked" in str(exc.value)
    assert "重新登录" in str(exc.value)
