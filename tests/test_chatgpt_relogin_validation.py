"""No external services: exercise login states and final bearer validation."""
import base64
import json
import time
from types import SimpleNamespace

import pytest

from platforms.chatgpt.login_client import EmailOtpResolver, login_chatgpt_web
from platforms.chatgpt.login_types import ChatGPTLoginError, ChatGPTLoginResult, LoginErrorCode
from platforms.chatgpt.registration_credentials import finalize_registered_credentials
from platforms.chatgpt.token_validation import validate_chatgpt_token


EMAIL = "target@example.com"
PROFILE = {"id": "user-target", "email": EMAIL}
CALLBACK = "https://chatgpt.com/api/auth/callback/openai?code=fake"


class Response:
    def __init__(self, payload, status=200, *, headers=None):
        self.payload, self.status_code = payload, status
        self.headers = headers or {}
        self.text = payload if isinstance(payload, str) else json.dumps(payload) if not isinstance(payload, Exception) else ""
        self.content = self.text.encode()
        self.url = "https://chatgpt.com/backend-api/me"

    def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.fixture
def bearer(monkeypatch):
    class Session:
        closed = False

        def get(self, url, **kwargs):
            state.requests.append((url, kwargs))
            if state.after_request:
                state.after_request()
            if isinstance(state.response, Exception):
                raise state.response
            return state.response

        def request(self, method, url, **kwargs):
            assert method == "GET"
            return self.get(url, **kwargs)

        def close(self):
            self.closed = True

    state = SimpleNamespace(response=Response(PROFILE), requests=[], sessions=[], creation_options=[], after_request=None)

    def create(**kwargs):
        state.creation_options.append(kwargs)
        session = Session()
        state.sessions.append(session)
        return session

    monkeypatch.setattr("platforms.chatgpt.token_validation.create_http_session", create)
    return state


@pytest.mark.parametrize("status,payload,code", [
    (401, {"error": {"code": "token_revoked"}}, LoginErrorCode.CREDENTIAL_REVOKED),
    (401, {"error": {"code": "token_expired"}}, LoginErrorCode.CREDENTIAL_INVALID),
    (200, {"id": "ua-guest", "email": ""}, LoginErrorCode.ANONYMOUS_PROFILE),
    (200, {"id": "user-other", "email": "other@example.com"}, LoginErrorCode.IDENTITY_MISMATCH),
    (200, {"id": "user-target"}, LoginErrorCode.API_RESPONSE_INVALID),
    (200, ValueError("HTML"), LoginErrorCode.API_RESPONSE_INVALID),
    (503, {}, LoginErrorCode.API_RESPONSE_INVALID),
])
def test_bearer_rejection(bearer, status, payload, code):
    bearer.response = Response(payload, status)
    with pytest.raises(ChatGPTLoginError) as exc:
        validate_chatgpt_token("new-token", expected_email=EMAIL)
    assert exc.value.code == code
    assert bearer.sessions[0].closed


def test_bearer_has_no_cookie_auth_or_trial_request(bearer):
    assert validate_chatgpt_token("new-token", expected_email=EMAIL.upper()) == PROFILE
    assert len(bearer.requests) == 1
    url, kwargs = bearer.requests[0]
    assert url.endswith("/backend-api/me")
    assert kwargs["headers"]["Authorization"] == "Bearer new-token"
    assert not kwargs.get("cookies") and "Cookie" not in kwargs["headers"]
    assert kwargs["allow_redirects"] is False


def test_fresh_bearer_validation_handles_challenge_with_same_fingerprint(bearer, monkeypatch):
    from registration_core.http_client import browser_profiles_for_flow
    profile = browser_profiles_for_flow()[0]
    bearer.response = Response('<script>window._cf_chl_opt={};</script>', 403,
                               headers={"cf-mitigated": "challenge"})
    def solve(raw, **kwargs):
        assert raw is bearer.sessions[0]
        assert kwargs["user_agent"] == profile.user_agent
        bearer.response = Response(PROFILE)
        return {"ok": True}
    monkeypatch.setattr("registration_core.login_transport.solve_challenge", solve)
    result = validate_chatgpt_token("new-token", expected_email=EMAIL, browser_profile=profile,
                                   challenge_options={"log": lambda _msg: None, "check_budget": lambda: None,
                                                      "deadline": time.monotonic() + 30})
    assert result == PROFILE and len(bearer.sessions) == 1
    assert bearer.creation_options[0]["profile"] is profile
    assert len(bearer.requests) == 2 and bearer.sessions[0].closed
    assert all(call[1]["headers"]["Authorization"] == "Bearer new-token" for call in bearer.requests)
    assert all("Cookie" not in call[1]["headers"] for call in bearer.requests)


def test_bearer_timeout_and_cancel_are_distinct(bearer):
    bearer.response = TimeoutError("timed out")
    with pytest.raises(ChatGPTLoginError) as exc:
        validate_chatgpt_token("token", expected_email=EMAIL)
    assert exc.value.code == LoginErrorCode.NETWORK_TIMEOUT
    cancelled = [False]
    bearer.after_request = lambda: cancelled.__setitem__(0, True)
    with pytest.raises(ChatGPTLoginError) as exc:
        validate_chatgpt_token("token", expected_email=EMAIL, cancel_check=lambda: cancelled[0])
    assert exc.value.code == LoginErrorCode.CANCELLED


@pytest.fixture
def auth(monkeypatch, bearer):
    from registration_core.auth_flow import AuthFlow
    class FakeAuth:
        _extract_page_type = staticmethod(AuthFlow._extract_page_type)
        _extract_continue_url_from_step = staticmethod(AuthFlow._extract_continue_url_from_step)
        _normalize_continue_url = staticmethod(lambda url: url or "")

        def __init__(self, _config):
            state.instances.append(self)
            state.proxies.append(_config.proxy)
            self.result = SimpleNamespace()
            self.session = SimpleNamespace(cookies={})
            self.closed = False

        def close(self): self.closed = True
        def enable_login_challenge_handling(self, **kwargs): state.deadlines.append(kwargs["deadline"])
        def open_chatgpt_home(self): state.events.append("home")
        def get_auth_providers(self): state.events.append("providers")
        def get_csrf_token(self):
            state.events.append("csrf")
            return "csrf"
        def _get_cookie_value_by_name(self, _name): return "csrf-cookie"
        def get_auth_url(self, *_args, **kwargs):
            state.events.append("signin")
            assert kwargs["screen_hint"] == "login_or_signup"
            return "https://auth.openai.com/authorize"
        def auth_oauth_init(self, _url):
            state.events.append("oauth")
            if state.bootstrap_errors:
                raise state.bootstrap_errors.pop(0)
            page = state.first_step.get("page", {}).get("type")
            self._oauth_landed_url = (state.landed_urls.pop(0) if state.landed_urls else state.landed_url) or {
                "login_password": "https://auth.openai.com/log-in/password",
                "email_otp_verification": "https://auth.openai.com/email-verification",
            }.get(page, CALLBACK)
            return "device"
        def get_sentinel_token(self, _id, **_kwargs): return "sentinel"
        def authorize_continue(self, **_kwargs):
            state.events.append("authorize_continue")
            assert _kwargs["allow_retry"] is False
            if state.authorize_errors:
                raise state.authorize_errors.pop(0)
            return state.first_step

        def login_password_verify(self, password):
            assert password == "saved-password"
            if state.password_error: raise state.password_error
            return state.after_password

        def kickoff_otp_delivery(self, _mode):
            state.events.append("send_otp")
            return True
        def verify_otp(self, code):
            assert code == "123456"
            if state.otp_error: raise state.otp_error
            return {"continue_url": CALLBACK}

        def _extract_workspace_id(self): return ""
        def _workspace_select(self, id):
            state.selected_workspace = id
            return CALLBACK

        def follow_redirect_chain(self, url):
            state.events.append("follow_redirects")
            return CALLBACK, CALLBACK
        def _consume_callback_for_session(self, url): state.callback_consumed = True

        def get_auth_session(self):
            assert state.callback_consumed
            payload = base64.urlsafe_b64encode(json.dumps({"exp": 1900000000, "https://api.openai.com/auth": {"chatgpt_account_id": "account-target"}}).encode()).decode().rstrip("=")
            self.result.access_token = f"header.{payload}.signature"
            self.result.session_token = "fresh-session"
            self.result.cookie_header = "__Secure-next-auth.session-token=fresh-session"

    state = SimpleNamespace(instances=[], first_step={"page": {"type": "login_password"}},
                            after_password={"continue_url": CALLBACK}, password_error=None,
                            otp_error=None, callback_consumed=False, selected_workspace=None,
                            events=[], landed_url="", landed_urls=[], authorize_errors=[],
                            bootstrap_errors=[], proxies=[], deadlines=[])
    monkeypatch.setattr("registration_core.auth_flow.AuthFlow", FakeAuth)
    return state


def test_password_login_validates_bearer_before_success(auth, bearer):
    result = login_chatgpt_web(email=EMAIL, password="saved-password")
    assert result.is_valid() and result.verified_email == EMAIL
    assert result.account_id == "account-target"
    assert result.login_method == "password"
    assert result.credential_updates()["account_id"] == "account-target"
    assert auth.instances[0].closed
    assert auth.events[:5] == ["home", "providers", "csrf", "signin", "oauth"]
    assert "authorize_continue" not in auth.events
    assert "follow_redirects" not in auth.events


def test_email_landing_keeps_pre_oauth_baseline_and_does_not_resend(auth):
    auth.first_step = {"page": {"type": "email_otp_verification"}}
    resolver = EmailOtpResolver(mode="manual", manual_handle=object())

    def baseline():
        auth.events.append("baseline")
        return {"old-email"}

    def fetch(**kwargs):
        assert kwargs["before_ids"] == {"old-email"}
        assert kwargs["issued_after"] is not None
        return "123456"

    resolver.baseline, resolver.fetch = baseline, fetch
    assert login_chatgpt_web(email=EMAIL, otp_resolver=resolver).token_verified
    assert auth.events.index("baseline") < auth.events.index("oauth")
    assert "send_otp" not in auth.events and "authorize_continue" not in auth.events


@pytest.mark.parametrize("path", ["/log-in", "/log-in-or-create-account"])
def test_email_input_landing_submits_email_once(auth, path):
    auth.landed_url = "https://auth.openai.com" + path
    assert login_chatgpt_web(email=EMAIL, password="saved-password").token_verified
    assert auth.events.count("authorize_continue") == 1


def test_invalid_state_rebuild_uses_new_landing_without_resubmitting_email(auth):
    from registration_core.auth_errors import AuthFlowError
    auth.landed_urls = ["https://auth.openai.com/log-in", "https://auth.openai.com/email-verification"]
    auth.authorize_errors = [AuthFlowError("authorize/continue", status_code=409, error_code="invalid_state")]
    resolver = EmailOtpResolver(mode="manual", manual_handle=object())
    resolver.baseline = lambda: {auth.events.count("oauth")}
    def fetch(**kwargs):
        assert kwargs["before_ids"] == {1}
        return "123456"
    resolver.fetch = fetch
    assert login_chatgpt_web(email=EMAIL, otp_resolver=resolver).token_verified
    assert auth.events.count("home") == 2 and auth.events.count("authorize_continue") == 1
    assert "send_otp" not in auth.events


def test_repeated_invalid_state_stops_after_one_rebuild(auth):
    from registration_core.auth_errors import AuthFlowError
    auth.landed_url = "https://auth.openai.com/log-in"
    auth.authorize_errors = [AuthFlowError("authorize/continue", status_code=409, error_code="invalid_state") for _ in range(2)]
    with pytest.raises(ChatGPTLoginError) as exc:
        login_chatgpt_web(email=EMAIL)
    assert exc.value.code == LoginErrorCode.AUTH_SESSION_INVALID
    assert auth.events.count("home") == 2 and auth.events.count("authorize_continue") == 2


def test_callback_landing_accepts_existing_session_only_after_bearer_validation(auth):
    auth.landed_url = CALLBACK
    result = login_chatgpt_web(email=EMAIL)
    assert result.login_method == "existing_session" and result.token_verified
    assert "authorize_continue" not in auth.events


def test_unknown_landing_does_not_resubmit_email(auth):
    auth.landed_url = "https://auth.openai.com/error"
    with pytest.raises(ChatGPTLoginError) as exc:
        login_chatgpt_web(email=EMAIL, password="saved-password")
    assert exc.value.code == LoginErrorCode.API_RESPONSE_INVALID
    assert "authorize_continue" not in auth.events and auth.instances[0].closed


def test_proxy_rotation_restarts_bootstrap_under_one_total_deadline(auth):
    from registration_core.auth_errors import AuthFlowError
    auth.bootstrap_errors.append(AuthFlowError("OAuth", status_code=403, error_code="cloudflare_challenge"))
    result = login_chatgpt_web(email=EMAIL, password="saved-password", proxy="http://user-sid-old-t-20:password@proxy.example:8080")
    assert result.token_verified
    assert len(auth.instances) == 2 and all(instance.closed for instance in auth.instances)
    assert auth.proxies[0] != auth.proxies[1]
    assert auth.deadlines[0] == auth.deadlines[1]
    assert auth.events.count("home") == 2


def test_fixed_proxy_does_not_start_a_new_login_after_challenge_failure(auth):
    from registration_core.auth_errors import AuthFlowError
    auth.bootstrap_errors.append(AuthFlowError("OAuth", status_code=403, error_code="cloudflare_challenge"))
    with pytest.raises(ChatGPTLoginError) as exc:
        login_chatgpt_web(email=EMAIL, proxy="http://127.0.0.1:7897")
    assert exc.value.code == LoginErrorCode.RISK_CONTROL and len(auth.instances) == 1


def test_password_and_email_otp_failures_close_sessions(auth):
    auth.password_error = RuntimeError("401 invalid_username_or_password")
    with pytest.raises(ChatGPTLoginError) as exc:
        login_chatgpt_web(email=EMAIL, password="saved-password")
    assert exc.value.code == LoginErrorCode.INVALID_PASSWORD and auth.instances[-1].closed
    auth.first_step = {"page": {"type": "email_otp_verification"}}
    auth.otp_error = RuntimeError("invalid_code")
    resolver = EmailOtpResolver(mode="manual", manual_handle=object())
    resolver.fetch = lambda **_kwargs: "123456"
    with pytest.raises(ChatGPTLoginError) as exc:
        login_chatgpt_web(email=EMAIL, otp_resolver=resolver)
    assert exc.value.code == LoginErrorCode.EMAIL_OTP_INVALID and auth.instances[-1].closed


@pytest.mark.parametrize("error,expected", [
    ("challenge", LoginErrorCode.RISK_CONTROL),
    ("invalid_state", LoginErrorCode.AUTH_SESSION_INVALID),
    ("connection", LoginErrorCode.NETWORK_ERROR),
])
def test_email_verification_keeps_transport_failure_reason(auth, error, expected):
    from registration_core.auth_errors import AuthFlowError
    auth.first_step = {"page": {"type": "email_otp_verification"}}
    auth.otp_error = (ConnectionError("connection reset") if error == "connection" else
                      AuthFlowError("OTP", status_code=403 if error == "challenge" else 409,
                                    error_code="cloudflare_challenge" if error == "challenge" else error))
    resolver = EmailOtpResolver(mode="manual", manual_handle=object())
    resolver.fetch = lambda **_kwargs: "123456"
    with pytest.raises(ChatGPTLoginError) as exc:
        login_chatgpt_web(email=EMAIL, otp_resolver=resolver)
    assert exc.value.code == expected and auth.instances[-1].closed


def test_password_followed_by_email_verification(auth):
    auth.after_password = {"page": {"type": "email_otp_verification"}}
    resolver = EmailOtpResolver(mode="manual", manual_handle=object())
    resolver.fetch = lambda **_kwargs: "123456"
    result = login_chatgpt_web(email=EMAIL, password="saved-password", otp_resolver=resolver)
    assert result.token_verified and result.verified_email == EMAIL


def test_email_otp_then_totp_then_workspace(auth, monkeypatch):
    auth.first_step = {"page": {"type": "email_otp_verification"}}
    monkeypatch.setattr("rebind_core.mfa_login.extract_factor_id", lambda *_args: "factor")
    monkeypatch.setattr("rebind_core.mfa_login.issue_mfa_challenge", lambda *_args: None)
    monkeypatch.setattr("registration_core.mfa_totp_protocol.totp_code_candidates", lambda *_args, **_kwargs: ["654321"])
    workspace_step = {"page": {"type": "workspace"}, "continue_url": "https://auth.openai.com/workspace",
                      "oai-client-auth-session": {"workspaces": [{"id": "personal"}, {"id": "workspace-target", "kind": "organization"}]}}
    monkeypatch.setattr("rebind_core.mfa_login.verify_mfa_totp", lambda *_args: workspace_step)
    resolver = EmailOtpResolver(mode="manual", manual_handle=object())
    resolver.fetch = lambda **_kwargs: "123456"
    result = login_chatgpt_web(email=EMAIL, totp_secret="FAKESECRET", otp_resolver=resolver)
    assert result.login_method == "email_otp_totp" and result.token_verified
    assert auth.selected_workspace == "workspace-target"


def test_revoked_final_token_never_becomes_success(auth, bearer):
    bearer.response = Response({"error": {"code": "token_revoked"}}, 401)
    with pytest.raises(ChatGPTLoginError) as exc:
        login_chatgpt_web(email=EMAIL, password="saved-password")
    assert exc.value.code == LoginErrorCode.CREDENTIAL_REVOKED
    assert auth.instances[0].closed


def test_cancel_after_session_before_validation(auth, bearer):
    bearer.after_request = lambda: setattr(auth, "cancelled", True)
    with pytest.raises(ChatGPTLoginError) as exc:
        login_chatgpt_web(email=EMAIL, password="saved-password", cancel_check=lambda: getattr(auth, "cancelled", False))
    assert exc.value.code == LoginErrorCode.CANCELLED
    assert auth.instances[0].closed


@pytest.mark.parametrize("recovery_ok", [True, False])
def test_registration_reauth_once_retains_security_data(bearer, monkeypatch, recovery_ok):
    bearer.response = Response({"error": {"code": "token_revoked"}}, 401)
    calls = []
    def login(**kwargs):
        calls.append(kwargs)
        if not recovery_ok:
            raise ChatGPTLoginError(LoginErrorCode.NETWORK_TIMEOUT, "恢复超时")
        return ChatGPTLoginResult(access_token="fresh", account_id="account-target", token_verified=True,
                                  verified_user_id=PROFILE["id"], verified_email=EMAIL)
    monkeypatch.setattr("platforms.chatgpt.registration_credentials.login_chatgpt_web", login)
    result = finalize_registered_credentials({"email": EMAIL, "password": "kept-password", "totp_secret": "KEPTSECRET", "access_token": "old"})
    assert len(calls) == 1
    assert result["password"] == "kept-password" and result["totp_secret"] == "KEPTSECRET"
    assert result["login_required"] is (not recovery_ok)
    assert result["access_token"] == ("fresh" if recovery_ok else "old")


def test_registration_network_error_does_not_reauth(bearer, monkeypatch):
    bearer.response = TimeoutError("timed out")
    monkeypatch.setattr("platforms.chatgpt.registration_credentials.login_chatgpt_web", lambda **_kwargs: pytest.fail("unexpected reauth"))
    result = finalize_registered_credentials({"email": EMAIL, "access_token": "kept"})
    assert result["login_required"] and result["credential_error_code"] == LoginErrorCode.NETWORK_TIMEOUT


def test_totp_rejection_is_not_network_error(auth, monkeypatch):
    from rebind_core.mfa_login import MfaLoginError
    monkeypatch.setattr("rebind_core.mfa_login.extract_factor_id", lambda *_args: "factor")
    monkeypatch.setattr("rebind_core.mfa_login.issue_mfa_challenge", lambda *_args: None)
    monkeypatch.setattr("registration_core.mfa_totp_protocol.totp_code_candidates", lambda *_args, **_kwargs: ["bad-code"])
    def reject(*_args):
        raise MfaLoginError("MFA_FAILED", "verify HTTP 400: invalid_code")
    monkeypatch.setattr("rebind_core.mfa_login.verify_mfa_totp", reject)
    with pytest.raises(ChatGPTLoginError) as exc:
        login_chatgpt_web(email=EMAIL, password="saved-password", totp_secret="FAKESECRET")
    assert exc.value.code == LoginErrorCode.MFA_INVALID and auth.instances[0].closed


@pytest.mark.parametrize("stage", ["challenge", "verify", "manual_verify", "session"])
def test_later_cloudflare_failure_is_not_misreported_as_bad_otp(auth, monkeypatch, stage):
    from registration_core.auth_errors import AuthFlowError
    def blocked(*_args, **_kwargs):
        auth.events.append("blocked")
        raise AuthFlowError("later step", status_code=403, error_code="cloudflare_challenge")
    if stage == "session":
        monkeypatch.setattr("platforms.chatgpt.login_client._finish_session", blocked)
    else:
        monkeypatch.setattr("rebind_core.mfa_login.extract_factor_id", lambda *_args: "factor")
        monkeypatch.setattr("rebind_core.mfa_login.issue_mfa_challenge", blocked if stage == "challenge" else lambda *_args: None)
        monkeypatch.setattr("rebind_core.mfa_login.verify_mfa_totp", blocked)
        monkeypatch.setattr("registration_core.mfa_totp_protocol.totp_code_candidates", lambda *_args, **_kwargs: ["654321"])
    if stage == "manual_verify":
        monkeypatch.setattr(EmailOtpResolver, "fetch", lambda *_args, **_kwargs: "654321")
    with pytest.raises(ChatGPTLoginError) as exc:
        login_chatgpt_web(email=EMAIL, password="saved-password",
                          totp_secret="" if stage == "manual_verify" else "FAKESECRET",
                          manual_totp_handle=object() if stage == "manual_verify" else None)
    assert exc.value.code == LoginErrorCode.RISK_CONTROL
    assert auth.events.count("blocked") == 1 and auth.instances[-1].closed


def test_partial_security_setup_preserves_created_totp(monkeypatch):
    from platforms.chatgpt.browser_register import _setup_chatgpt_password_and_totp
    from platforms.chatgpt.registration_credentials import PartialSecuritySetupError
    monkeypatch.setattr("platforms.chatgpt.browser_register._chatgpt_security_session", lambda *_args: {"user": {"email": EMAIL}})
    monkeypatch.setattr("platforms.chatgpt.browser_register._setup_chatgpt_totp", lambda **_kwargs: {"totp_set": True, "totp_secret": "PRESERVEDSECRET", "otpauth": "otpauth://kept"})
    def fail_password(**_kwargs):
        raise TimeoutError("password step timed out")
    monkeypatch.setattr("platforms.chatgpt.browser_register._setup_chatgpt_password", fail_password)
    with pytest.raises(PartialSecuritySetupError) as exc:
        _setup_chatgpt_password_and_totp(object(), email=EMAIL, password="kept", otp_callback=None, log=lambda _msg: None)
    assert exc.value.partial_result["totp_secret"] == "PRESERVEDSECRET"
    assert exc.value.partial_result["password_set"] is True
