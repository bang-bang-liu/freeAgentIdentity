"""Exercise browser registration transitions without a browser or external calls."""
from contextlib import contextmanager
import json
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from platforms.chatgpt import browser_register as registration
from platforms.chatgpt.constants import CHATGPT_APP, OPENAI_AUTH


EMAIL = "target@example.com"
HOME = f"{CHATGPT_APP}/"
SESSION_URL = f"{CHATGPT_APP}/api/auth/session"
CALLBACK = f"{CHATGPT_APP}/api/auth/callback/openai?code=test-code&state=test-state"
AUTHORIZE = f"{OPENAI_AUTH}/oauth/authorize?" + urlencode({
    "redirect_uri": f"{CHATGPT_APP}/api/auth/callback/openai",
    "response_type": "code", "scope": "openid email profile",
})


class Page:
    def __init__(self, url, *, cookies=None, session_data=None):
        self.url = url
        self.cookies = dict(cookies or {})
        self.context = SimpleNamespace(cookies=lambda: [
            {"name": name, "value": value} for name, value in self.cookies.items()
        ])
        self.session_data = session_data if session_data is not None else {}
        self.fetches = []

    def evaluate(self, _script, *args):
        if args and args[0] == SESSION_URL:
            self.fetches.append((self.url, args[0]))
            return {"status": 200, "url": SESSION_URL, "text": json.dumps(self.session_data)}
        return False


@pytest.fixture
def clock(monkeypatch):
    value = [0.0]
    def sleep(seconds):
        value[0] += seconds
    monkeypatch.setattr(registration.time, "time", lambda: value[0])
    monkeypatch.setattr(registration.time, "monotonic", lambda: value[0])
    monkeypatch.setattr(registration.time, "sleep", sleep)
    return value


@pytest.fixture
def no_dom(monkeypatch):
    monkeypatch.setattr(registration, "_find_first_selector", lambda *_args, **_kwargs: "")
    monkeypatch.setattr(registration, "_click_passwordless_login_if_available", lambda *_args, **_kwargs: False)
    monkeypatch.setattr(registration, "_extract_auth_error_text", lambda _page: "")
    monkeypatch.setattr(registration, "_recover_signup_password_page", lambda *_args: False)
    monkeypatch.setattr(registration, "_handle_post_signup_onboarding", lambda *_args: None)


@pytest.mark.parametrize("url", [
    AUTHORIZE,
    f"{OPENAI_AUTH}/oauth/authorize?redirect_uri={CHATGPT_APP}/api/auth/callback/openai&code=misleading",
    f"{OPENAI_AUTH}/oauth/authorize?next=/email-verification&other=/about-you",
    "https://example.com/?redirect_uri=https://chatgpt.com/&code=misleading",
    "https://chatgpt.com.example.com/",
    "https://chatgpt.com@example.com/",
    f"{CHATGPT_APP}/auth/login",
    SESSION_URL,
    f"{CHATGPT_APP}/api/auth/callback/openai?error=OAuthCallback",
    "https://[broken",
])
def test_unfinished_or_unrelated_urls_cannot_complete_registration(url):
    state = registration._extract_flow_state(None, url)
    assert state["page_type"] == ""
    # Even a stale page type must not override the actual URL.
    assert not registration._is_registration_complete({**state, "page_type": "chatgpt_home"})
    assert not registration._is_email_otp(state)
    assert not registration._is_about_you(state)


@pytest.mark.parametrize("url,page_type", [
    (HOME, "chatgpt_home"),
    (CALLBACK, "oauth_callback"),
    (f"{OPENAI_AUTH}/create-account/password", "create_account_password"),
    (f"{OPENAI_AUTH}/log-in/password", "login_password"),
    (f"{OPENAI_AUTH}/email-verification", "email_otp_verification"),
    (f"{OPENAI_AUTH}/about-you", "about_you"),
    (f"{OPENAI_AUTH}/add-phone", "add_phone"),
])
def test_real_registration_pages_keep_their_state(url, page_type):
    assert registration._extract_flow_state(None, url)["page_type"] == page_type


@pytest.mark.parametrize("url", [AUTHORIZE, "http://chatgpt.com/", "https://chatgpt.com:444/"])
def test_session_fetch_is_not_attempted_across_origins(url):
    page = Page(url)
    assert registration._fetch_chatgpt_session_via_same_origin(page, {}, lambda _: None, SESSION_URL) == (None, "", False)
    assert not page.fetches


def test_real_same_origin_session_fetch_returns_token():
    page = Page(HOME, session_data={"accessToken": "fresh-token", "user": {"email": EMAIL}})
    result, error, attempted = registration._fetch_chatgpt_session_via_same_origin(page, {}, lambda _: None, SESSION_URL)
    assert attempted and not error and result["access_token"] == "fresh-token"
    assert page.fetches == [(HOME, SESSION_URL)]
    assert registration._same_origin("https://CHATGPT.com:443/", HOME)


def start_authorization(monkeypatch, page):
    monkeypatch.setattr(registration, "_goto_with_retry", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(registration, "_get_browser_csrf_token", lambda *_args: "csrf")
    monkeypatch.setattr(registration, "_start_browser_signin", lambda *_args: AUTHORIZE)
    monkeypatch.setattr(registration, "_browser_authorize", lambda *_args: page.url)


def test_authorization_waits_for_delayed_verification_page(monkeypatch, clock, no_dom):
    page = Page(AUTHORIZE)
    start_authorization(monkeypatch, page)
    def advance(seconds):
        clock[0] += seconds
        page.url = f"{OPENAI_AUTH}/email-verification"
    monkeypatch.setattr(registration.time, "sleep", advance)
    state = registration._start_browser_signup_via_authorize(page, EMAIL, "device", lambda _: None)
    assert clock[0] > 0 and state["page_type"] == "email_otp_verification"
    assert not registration._is_registration_complete(state)


def test_stalled_authorization_fails_before_collecting_credentials(monkeypatch, clock, no_dom):
    page = Page(AUTHORIZE)
    start_authorization(monkeypatch, page)
    logs = []
    with pytest.raises(RuntimeError, match="OAuth 授权页面未推进"):
        registration._browser_registration_flow(page, EMAIL, "password", None, logs.append)
    assert clock[0] == 20
    assert not page.fetches
    assert not any("注册流程完成" in entry for entry in logs)


def test_pending_callback_is_consumed_before_flow_completes(monkeypatch, clock, no_dom):
    page = Page(f"{OPENAI_AUTH}/about-you")
    calls = []
    monkeypatch.setattr(registration, "_start_browser_signup_via_authorize",
                        lambda *_args: registration._extract_flow_state(None, page.url))
    monkeypatch.setattr(registration, "_submit_about_you_via_page", lambda *_args: {
        "ok": True, "status": 200, "url": page.url,
        "data": {"page": {"type": "external_url"}, "continue_url": CALLBACK},
    })
    def follow(_page, url, **_kwargs):
        calls.append(url)
        page.url = HOME
        page.cookies["__Secure-next-auth.session-token"] = "fresh-session"
    monkeypatch.setattr(registration, "_goto_with_retry", follow)
    state = registration._browser_registration_flow(page, EMAIL, "password", None, lambda _: None)
    assert calls == [CALLBACK]
    assert state["page_type"] == "chatgpt_home" and state["account_created"]


def test_consumed_callback_is_not_requested_again(monkeypatch, no_dom):
    page = Page(HOME, cookies={"__Secure-next-auth.session-token.0": "part1", "__Secure-next-auth.session-token.1": "part2"})
    monkeypatch.setattr(registration, "_goto_with_retry", lambda *_args, **_kwargs: pytest.fail("callback already consumed"))
    state = registration._complete_registration_navigation(page, registration._extract_flow_state(None, CALLBACK), lambda _: None)
    assert state["page_type"] == "chatgpt_home"


def test_callback_that_never_redirects_cannot_finish_registration(monkeypatch, clock, no_dom):
    page = Page(CALLBACK)
    with pytest.raises(RuntimeError, match="注册回调页面未推进"):
        registration._complete_registration_navigation(page, registration._extract_flow_state(None, CALLBACK), lambda _: None)
    assert clock[0] == 20


def test_callback_navigation_abort_after_redirect_does_not_replay_code(monkeypatch, no_dom):
    page = Page(f"{OPENAI_AUTH}/about-you")
    calls = []
    def navigate(_page, url, **kwargs):
        calls.append((url, kwargs["attempts"]))
        page.url = HOME
        raise RuntimeError('NS_BINDING_ABORTED; navigating to callback')
    monkeypatch.setattr(registration, "_goto_with_retry", navigate)
    state = registration._complete_registration_navigation(page, registration._extract_flow_state(None, CALLBACK), lambda _: None)
    assert calls == [(CALLBACK, 1)] and state["page_type"] == "chatgpt_home"


def test_created_account_keeps_creation_evidence_when_callback_fails(monkeypatch, no_dom):
    page = Page(f"{OPENAI_AUTH}/about-you")
    monkeypatch.setattr(registration, "_start_browser_signup_via_authorize",
                        lambda *_args: registration._extract_flow_state(None, page.url))
    monkeypatch.setattr(registration, "_submit_about_you_via_page", lambda *_args: {
        "ok": True, "status": 200, "url": page.url,
        "data": {"continue_url": CALLBACK},
    })
    def fail(*_args, **_kwargs):
        raise RuntimeError("callback connection reset")
    monkeypatch.setattr(registration, "_goto_with_retry", fail)
    state = registration._browser_registration_flow(page, EMAIL, "password", None, lambda _: None)
    assert state["account_created"] and state["page_type"] == "about_you"


def worker_with_page(monkeypatch, page, *, setup_security=False):
    worker = registration.ChatGPTBrowserRegister(headless=True, setup_security=setup_security, log_fn=lambda _: None)
    @contextmanager
    def browser(_options):
        yield SimpleNamespace(new_page=lambda: page)
    monkeypatch.setattr(worker, "_open_browser", browser)
    return worker


@pytest.mark.parametrize("setup_security", [False, True])
def test_guest_home_with_empty_session_is_not_saved_as_registered(monkeypatch, clock, no_dom, setup_security):
    page = Page(HOME)
    worker = worker_with_page(monkeypatch, page, setup_security=setup_security)
    monkeypatch.setattr(registration, "_start_browser_signup_via_authorize",
                        lambda *_args: registration._extract_flow_state(None, HOME))
    monkeypatch.setattr(registration, "_setup_chatgpt_password_and_totp", lambda *_args, **_kwargs: pytest.fail("guest cannot set security"))
    monkeypatch.setattr("platforms.chatgpt.registration_credentials.finalize_registered_credentials",
                        lambda *_args, **_kwargs: pytest.fail("unconfirmed account must not be saved"))
    with pytest.raises(RuntimeError, match="未确认账号创建成功"):
        worker.run(EMAIL, "password")
    assert page.fetches and all(origin == HOME for origin, _ in page.fetches)


def test_confirmed_account_survives_empty_session_and_requires_login(monkeypatch, clock, no_dom):
    page = Page(HOME)
    worker = worker_with_page(monkeypatch, page)
    monkeypatch.setattr(registration, "_browser_registration_flow", lambda *_args: {
        **registration._extract_flow_state(None, HOME), "account_created": True,
    })
    result = worker.run(EMAIL, "saved-password")
    assert result["registration_state"]["account_created"]
    assert result["login_required"] and not result["token_verified"]
    assert result["password"] == "saved-password" and result["access_token"] == ""


def test_registration_with_session_keeps_final_bearer_validation(monkeypatch, no_dom):
    page = Page(HOME, session_data={"accessToken": "fresh-token", "user": {"email": EMAIL}})
    worker = worker_with_page(monkeypatch, page)
    monkeypatch.setattr(registration, "_start_browser_signup_via_authorize",
                        lambda *_args: registration._extract_flow_state(None, HOME))
    validations = []
    def validate(token, **kwargs):
        validations.append((token, kwargs["expected_email"]))
        return {"id": "user-target", "email": EMAIL}
    monkeypatch.setattr("platforms.chatgpt.registration_credentials.validate_chatgpt_token", validate)
    result = worker.run(EMAIL, "password")
    assert result["access_token"] == "fresh-token" and result["token_verified"]
    assert not result["login_required"] and validations == [("fresh-token", EMAIL)]


def test_unconfirmed_account_with_rejected_session_is_not_success(monkeypatch, no_dom):
    from platforms.chatgpt.login_types import ChatGPTLoginError, LoginErrorCode
    page = Page(HOME, session_data={"accessToken": "guest-token"})
    worker = worker_with_page(monkeypatch, page)
    logs = []
    worker.log = logs.append
    monkeypatch.setattr(registration, "_start_browser_signup_via_authorize",
                        lambda *_args: registration._extract_flow_state(None, HOME))
    def reject(*_args, **_kwargs):
        raise ChatGPTLoginError(LoginErrorCode.ANONYMOUS_PROFILE, "业务接口返回匿名访客资料")
    monkeypatch.setattr("platforms.chatgpt.registration_credentials.validate_chatgpt_token", reject)
    with pytest.raises(RuntimeError, match="未确认账号创建成功"):
        worker.run(EMAIL, "password")
    assert not any(entry.startswith("注册流程完成:") for entry in logs)


def test_otp_profile_callback_and_credentials_run_in_order(monkeypatch, no_dom):
    page = Page(f"{OPENAI_AUTH}/email-verification")
    events = []
    worker = worker_with_page(monkeypatch, page)
    worker.otp_callback = lambda: events.append("mailbox") or "123456"
    monkeypatch.setattr(registration, "_start_browser_signup_via_authorize",
                        lambda *_args: registration._extract_flow_state(None, page.url))
    monkeypatch.setattr(registration, "_wait_for_visible_otp_input", lambda *_args, **_kwargs: True)
    def verify(_page, code, _log):
        assert code == "123456"
        events.append("verify")
        page.url = f"{OPENAI_AUTH}/about-you"
        return {"ok": True, "url": page.url}
    def profile(*_args):
        events.append("profile")
        return {"ok": True, "status": 200, "data": {"continue_url": CALLBACK}}
    def follow(_page, url, **_kwargs):
        assert url == CALLBACK
        events.append("callback")
        page.url = HOME
        page.cookies["__Secure-next-auth.session-token"] = "new-session"
        page.session_data = {"accessToken": "fresh-token", "user": {"email": EMAIL}}
    def validate(token, **kwargs):
        assert token == "fresh-token" and kwargs["expected_email"] == EMAIL
        events.append("bearer")
        return {"id": "user-target", "email": EMAIL}
    monkeypatch.setattr(registration, "_submit_otp_via_page", verify)
    monkeypatch.setattr(registration, "_submit_about_you_via_page", profile)
    monkeypatch.setattr(registration, "_goto_with_retry", follow)
    monkeypatch.setattr("platforms.chatgpt.registration_credentials.validate_chatgpt_token", validate)
    result = worker.run(EMAIL, "password")
    assert events == ["mailbox", "verify", "profile", "callback", "bearer"]
    assert result["registration_state"]["account_created"] and result["token_verified"]
    assert result["session_token"] == "new-session" and not result["login_required"]


def test_failed_profile_submission_cannot_preserve_an_uncreated_account(monkeypatch, no_dom):
    page = Page(f"{OPENAI_AUTH}/about-you")
    worker = worker_with_page(monkeypatch, page)
    monkeypatch.setattr(registration, "_start_browser_signup_via_authorize",
                        lambda *_args: registration._extract_flow_state(None, page.url))
    monkeypatch.setattr(registration, "_submit_about_you_via_page", lambda *_args: {
        "ok": False, "status": 400, "text": "profile rejected",
    })
    monkeypatch.setattr("platforms.chatgpt.registration_credentials.finalize_registered_credentials",
                        lambda *_args, **_kwargs: pytest.fail("profile never accepted"))
    with pytest.raises(RuntimeError, match="about_you 提交失败"):
        worker.run(EMAIL, "password")
    assert not page.fetches


def test_security_setup_session_loss_uses_verified_registration_snapshot(monkeypatch, clock, no_dom):
    page = Page(HOME, session_data={"accessToken": "before-security-token", "user": {"email": EMAIL}})
    worker = worker_with_page(monkeypatch, page, setup_security=True)
    monkeypatch.setattr(registration, "_start_browser_signup_via_authorize",
                        lambda *_args: registration._extract_flow_state(None, HOME))
    def security(*_args, **_kwargs):
        page.session_data = {}
        return {"password_set": True, "totp_set": True, "totp_secret": "SAVEDSECRET"}
    monkeypatch.setattr(registration, "_setup_chatgpt_password_and_totp", security)
    validated = []
    def validate(token, **_kwargs):
        validated.append(token)
        return {"id": "user-target", "email": EMAIL}
    monkeypatch.setattr("platforms.chatgpt.registration_credentials.validate_chatgpt_token", validate)
    result = worker.run(EMAIL, "password")
    assert result["token_verified"] and result["totp_secret"] == "SAVEDSECRET"
    assert result["access_token"] == "before-security-token" and validated == ["before-security-token"]
