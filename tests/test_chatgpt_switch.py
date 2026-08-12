from platforms.chatgpt import switch
from platforms.chatgpt.switch import extract_session_token


def test_extract_session_token_prefers_explicit_value():
    assert extract_session_token("explicit", "__Secure-next-auth.session-token=from-cookie") == "explicit"


def test_extract_session_token_reads_single_cookie():
    assert extract_session_token("", "foo=bar; __Secure-next-auth.session-token=whole; oai-did=device") == "whole"


def test_extract_session_token_reassembles_numbered_cookie_chunks():
    cookies = (
        "__Secure-next-auth.session-token.1=second; "
        "other=value; "
        "__Secure-next-auth.session-token.0=first"
    )

    assert extract_session_token("", cookies) == "firstsecond"


def test_fetch_account_state_prefers_session_cookies_over_bearer(monkeypatch):
    calls = []

    class _Response:
        status_code = 200

        def json(self):
            return {"email": "user@example.com"}

    def fake_get(url, **kwargs):
        calls.append(kwargs)
        return _Response()

    monkeypatch.setattr(switch.curl_requests, "get", fake_get)

    state = switch.fetch_chatgpt_account_state(
        access_token="token",
        cookies="__Secure-next-auth.session-token=session; oai-did=device",
    )

    assert state["valid"] is True
    assert state["profile_auth_method"] == "cookies"
    assert calls[0]["cookies"]["__Secure-next-auth.session-token"] == "session"
    assert "authorization" not in calls[0]["headers"]
