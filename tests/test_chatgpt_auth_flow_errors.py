"""Regression coverage for invalid OAuth sessions and blocked bootstrap pages."""
import json
import time

import pytest

from platforms.chatgpt.login_client import login_chatgpt_web
from platforms.chatgpt.login_types import ChatGPTLoginError, LoginErrorCode
from platforms.chatgpt.token_validation import network_login_error
from registration_core.auth_errors import AuthFlowError
from registration_core.auth_flow import AuthFlow
from registration_core.config import Config


EMAIL = "target@example.com"
INVALID_STATE = {"error": {
    "code": "invalid_state",
    "message": "Your sign-in session is no longer valid. Please start over to continue.",
}}
PASSWORD_STEP = {"page": {"type": "login_password"}}


class Response:
    def __init__(self, status=200, payload=None, *, url="https://auth.openai.com/log-in", text=None):
        self.status_code = status
        self.payload = payload
        self.headers = {"x-request-id": "test-request-123"}
        self.url = url
        self.text = json.dumps(payload) if text is None else text

    def json(self):
        if self.payload is None:
            raise ValueError("not JSON")
        return self.payload


class Session:
    def __init__(self):
        self.cookies = {"oai-did": "test-device"}
        self.gets = []
        self.posts = []
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        return self.gets.pop(0)

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        return self.posts.pop(0)

    def request(self, method, url, **kwargs):
        return self.get(url, **kwargs) if method == "GET" else self.post(url, **kwargs)

    def close(self):
        self.closed = True


@pytest.fixture
def session(monkeypatch):
    session = Session()
    monkeypatch.setattr("registration_core.auth_flow.create_http_session", lambda **_kwargs: session)
    return session


def test_challenged_bootstrap_stops_before_sentinel_or_login_request(session, monkeypatch):
    session.gets = [Response(403, text="<html><title>Just a moment...</title>private-body</html>") for _ in range(4)]
    monkeypatch.setattr(AuthFlow, "open_chatgpt_home", lambda _self: None)
    monkeypatch.setattr(AuthFlow, "get_auth_providers", lambda _self: None)
    monkeypatch.setattr(AuthFlow, "_get_cookie_value_by_name", lambda *_args: "csrf-cookie")
    monkeypatch.setattr(AuthFlow, "get_csrf_token", lambda _self: "csrf")
    monkeypatch.setattr(AuthFlow, "get_auth_url", lambda *_args, **_kwargs: "https://auth.openai.com/oauth/authorize")
    monkeypatch.setattr(AuthFlow, "get_sentinel_token", lambda *_args: pytest.fail("blocked OAuth bootstrap continued"))

    with pytest.raises(ChatGPTLoginError) as exc:
        login_chatgpt_web(email=EMAIL)

    assert exc.value.code == LoginErrorCode.RISK_CONTROL
    assert "Cloudflare" in exc.value.message and "403" in exc.value.message
    assert "private-body" not in exc.value.message
    assert len(session.calls) == 4 and session.closed


@pytest.mark.parametrize("status", [400, 409])
def test_invalid_state_keeps_reason_and_request_id_without_body(status):
    response = Response(status, INVALID_STATE, text="private-token-and-cookie")
    error = AuthFlowError.from_response("authorize/continue", response)
    mapped = network_login_error(error, "进入登录流程")

    assert mapped.code == LoginErrorCode.AUTH_SESSION_INVALID
    assert f"HTTP {status}" in mapped.message
    assert "invalid_state" in mapped.message and "登录会话已失效" in mapped.message
    assert "test-request-123" in mapped.message
    assert "private-token-and-cookie" not in str(error) + mapped.message


@pytest.mark.parametrize("landed", ["log-in", "create-account"])
@pytest.mark.parametrize("retry_succeeds", [True, False])
def test_invalid_state_rebuild_preserves_login_and_retries_once(session, monkeypatch, landed, retry_succeeds):
    auth = AuthFlow(Config(registration_mode="protocol"))
    session.posts = [Response(409, INVALID_STATE), Response(200, PASSWORD_STEP) if retry_succeeds else Response(409, INVALID_STATE)]
    session.gets = [Response(url=f"https://auth.openai.com/{landed}")]
    if landed == "create-account":
        session.gets.append(Response(url="https://auth.openai.com/log-in"))
    bootstrap_requests = []
    monkeypatch.setattr(auth, "open_chatgpt_home", lambda: None)
    monkeypatch.setattr(auth, "get_auth_providers", lambda: None)
    monkeypatch.setattr(auth, "get_csrf_token", lambda: "fresh-csrf")

    def auth_url(csrf, **kwargs):
        bootstrap_requests.append(kwargs)
        return "https://auth.openai.com/oauth/authorize"

    def prepare(*_args, **_kwargs):
        auth._last_sentinel_token = "fresh-sentinel"

    monkeypatch.setattr(auth, "get_auth_url", auth_url)
    monkeypatch.setattr(auth, "_prepare_sentinel_for_step", prepare)
    try:
        kwargs = dict(email=EMAIL, sentinel_token="old-sentinel", screen_hint="login", referer="https://auth.openai.com/log-in")
        if retry_succeeds:
            assert auth.authorize_continue(**kwargs) == PASSWORD_STEP
        else:
            with pytest.raises(AuthFlowError) as exc:
                auth.authorize_continue(**kwargs)
            assert network_login_error(exc.value, "进入登录流程").code == LoginErrorCode.AUTH_SESSION_INVALID
        assert bootstrap_requests == [{"email": EMAIL, "screen_hint": "login", "prompt": "login"}]
        posts = [kwargs for method, _url, kwargs in session.calls if method == "POST"]
        assert len(posts) == 2
        assert all(post["json"]["screen_hint"] == "login" for post in posts)
        assert posts[1]["headers"]["openai-sentinel-token"] == "fresh-sentinel"
    finally:
        auth.close()


def test_rebuild_aborts_on_blocked_oauth_page(session, monkeypatch):
    auth = AuthFlow(Config(registration_mode="protocol"))
    session.posts = [Response(409, INVALID_STATE)]
    session.gets = [Response(403, text="<title>Just a moment...</title>")]
    monkeypatch.setattr(auth, "open_chatgpt_home", lambda: None)
    monkeypatch.setattr(auth, "get_auth_providers", lambda: None)
    monkeypatch.setattr(auth, "get_csrf_token", lambda: "csrf")
    monkeypatch.setattr(auth, "get_auth_url", lambda *_args, **_kwargs: "https://auth.openai.com/oauth/authorize")
    monkeypatch.setattr(auth, "_prepare_sentinel_for_step", lambda *_args, **_kwargs: pytest.fail("blocked rebuild continued"))
    try:
        with pytest.raises(AuthFlowError) as exc:
            auth.authorize_continue(email=EMAIL, sentinel_token="old", screen_hint="login")
        assert network_login_error(exc.value, "进入登录流程").code == LoginErrorCode.RISK_CONTROL
        assert len([call for call in session.calls if call[0] == "POST"]) == 1
    finally:
        auth.close()


def test_unknown_http_and_network_failures_stay_distinct():
    assert network_login_error(RuntimeError("HTTP 409"), "登录").code == LoginErrorCode.API_RESPONSE_INVALID
    assert network_login_error(TimeoutError("timed out"), "登录").code == LoginErrorCode.NETWORK_TIMEOUT
    assert network_login_error(ConnectionError("connection reset"), "登录").code == LoginErrorCode.NETWORK_ERROR


@pytest.mark.parametrize("kind,flow", [("password", "password_verify"), ("email", "email_otp_validate"), ("mfa", "password_verify")])
def test_login_validation_carries_matching_sentinel_bundle(session, monkeypatch, kind, flow):
    from rebind_core.mfa_login import verify_mfa_totp
    auth = AuthFlow(Config(registration_mode="protocol"))
    auth.enable_login_challenge_handling(log=lambda _msg: None, check_budget=lambda: None,
                                         deadline=time.monotonic() + 30)
    session.posts = [Response(200, {"continue_url": "https://chatgpt.com/api/auth/callback/openai?code=fake"})]
    requested_flows = []
    def bundle(_device, current_flow):
        requested_flows.append(current_flow)
        return {"token": f"sentinel-{current_flow}", "so_token": "observer"}
    monkeypatch.setattr(auth, "_get_sentinel_token_bundle", bundle)
    try:
        if kind == "password":
            auth.get_sentinel_token("device", flow="password_verify")
            auth.login_password_verify("password")
        elif kind == "email":
            auth.verify_otp("123456")
        else:
            verify_mfa_totp(auth, "factor", "123456")
        headers = session.calls[-1][2]["headers"]
        assert requested_flows == [flow]
        assert headers["openai-sentinel-token"] == f"sentinel-{flow}"
        assert headers["openai-sentinel-so-token"] == "observer"
    finally:
        auth.close()


def test_callback_challenge_is_propagated_and_callback_is_not_consumed_twice(session):
    from platforms.chatgpt.login_client import _finish_session
    callback = "https://chatgpt.com/api/auth/callback/openai?code=fake"
    auth = AuthFlow(Config(registration_mode="protocol"))
    auth.enable_login_challenge_handling(log=lambda _msg: None, check_budget=lambda: None,
                                         deadline=time.monotonic() + 30)
    auth.session.retries = 0
    session.gets = [Response(403, text="<title>Just a moment...</title>", url=callback)]
    try:
        with pytest.raises(AuthFlowError) as exc:
            _finish_session(auth, callback)
        assert exc.value.error_code == "cloudflare_challenge"
        assert len(session.calls) == 1 and session.calls[0][1] == callback
    finally:
        auth.close()
