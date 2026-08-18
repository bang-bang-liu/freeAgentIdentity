from __future__ import annotations

import json
import sys
import types

from platforms.chatgpt.constants import CHATGPT_APP, OPENAI_API_ENDPOINTS, SENTINEL_REQ_URL
from platforms.chatgpt.plugin import ChatGPTPlatform
from platforms.chatgpt.fingerprint import (
    FIREFOX_ACCEPT_LANGUAGE,
    FIREFOX_IMPERSONATE,
    FIREFOX_PLATFORM,
    FIREFOX_UA,
    SENTINEL_ACCEPT_LANGUAGE,
    SENTINEL_LANGUAGE,
    SENTINEL_SCREEN,
    check_alignment,
)
from platforms.chatgpt.protocol_register import (
    ChatGPTProtocolRegister,
    OpenAISentinelClient,
    _SentinelBrowserRuntime,
    _SentinelTokenGenerator,
)


class _FakeCookies:
    def get(self, key):
        return "device-from-cookie" if key == "oai-did" else None

    def get_dict(self):
        return {"oai-did": "device-from-cookie"}


class _FakeResponse:
    def __init__(self, status_code=200, payload=None, *, headers=None, text="", url=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}
        self.text = text
        self.url = url

    def json(self):
        return self._payload


class _FakeSession:
    def __init__(self):
        self.cookies = _FakeCookies()
        self.calls = []
        self.create_headers = {}
        self.password_body = {}
        self.password_add_body = {}
        self.mfa_enroll_body = {}
        self.mfa_activate_body = {}
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append(("GET", url, kwargs))
        if url == f"{CHATGPT_APP}/api/auth/csrf":
            return _FakeResponse(payload={"csrfToken": "csrf-token"})
        if url == "https://auth.openai.com/authorize-start":
            return _FakeResponse(headers={"location": "/email-verification"})
        if url == "https://auth.openai.com/security-authorize-start":
            return _FakeResponse(url="https://auth.openai.com/email-verification?reauth=password")
        if url == f"{CHATGPT_APP}/backend-api/accounts/mfa_info":
            return _FakeResponse(payload={"mfa_enabled_v2": False})
        if url == "https://auth.openai.com/reset-password/new-password":
            return _FakeResponse(url=url)
        if url == f"{CHATGPT_APP}/api/auth/session":
            return _FakeResponse(
                payload={
                    "accessToken": "header.payload.signature",
                    "sessionToken": "session-token",
                    "expires": "2026-08-01T00:00:00Z",
                    "account": {"id": "account-123", "planType": "free"},
                }
            )
        return _FakeResponse()

    def post(self, url, **kwargs):
        self.calls.append(("POST", url, kwargs))
        if url.startswith(f"{CHATGPT_APP}/api/auth/signin/openai?"):
            if "post_login_add_password=true" in url:
                return _FakeResponse(payload={"url": "https://auth.openai.com/security-authorize-start"})
            return _FakeResponse(payload={"url": "https://auth.openai.com/authorize-start"})
        if url == OPENAI_API_ENDPOINTS["validate_otp"]:
            code = kwargs["json"]["code"]
            if code == "123456":
                return _FakeResponse(payload={"continue_url": "/create-account/password"})
            assert code == "654321"
            return _FakeResponse(payload={"continue_url": "/reset-password/new-password"})
        if url == "https://auth.openai.com/api/accounts/email-otp/resend":
            return _FakeResponse(payload={})
        if url == SENTINEL_REQ_URL:
            request_payload = json.loads(kwargs["data"])
            return _FakeResponse(
                payload={
                    "token": "challenge-token",
                    "proofofwork": {"required": False},
                    "flow": request_payload["flow"],
                }
            )
        if url == OPENAI_API_ENDPOINTS["create_account"]:
            self.create_headers = kwargs["headers"]
            return _FakeResponse(
                payload={
                    "continue_url": f"{CHATGPT_APP}/api/auth/callback/openai?code=ok&state=test"
                }
            )
        if url == OPENAI_API_ENDPOINTS["register"]:
            self.password_body = kwargs["json"]
            return _FakeResponse(payload={"continue_url": "/about-you"})
        if url == f"{CHATGPT_APP}/backend-api/accounts/mfa/enroll":
            self.mfa_enroll_body = kwargs["json"]
            return _FakeResponse(payload={"secret": "JBSWY3DPEHPK3PXP", "session_id": "mfa-session"})
        if url == f"{CHATGPT_APP}/backend-api/accounts/mfa/user/activate_enrollment":
            self.mfa_activate_body = kwargs["json"]
            return _FakeResponse(payload={"success": True})
        if url == "https://auth.openai.com/api/accounts/password/add":
            self.password_add_body = kwargs["json"]
            return _FakeResponse(payload={"continue_url": f"{CHATGPT_APP}/?security_setup=password_done"})
        raise AssertionError(f"unexpected POST {url}")

    def close(self):
        self.closed = True


def test_protocol_register_completes_email_flow_without_browser():
    session = _FakeSession()
    logs = []

    codes = iter(["123456", "654321"])

    def otp_callback(**_kwargs):
        return next(codes)

    otp_callback.supports_timeout_override = True
    worker = ChatGPTProtocolRegister(
        session=session,
        otp_callback=otp_callback,
        log_fn=logs.append,
        sentinel_runtime=False,
    )

    result = worker.run(email="user@outlook.com", password="StrongPass123!")

    assert result["email"] == "user@outlook.com"
    assert result["password"] == "StrongPass123!"
    assert result["access_token"] == "header.payload.signature"
    assert result["session_token"] == "session-token"
    assert result["account_id"] == "account-123"
    assert result["totp_set"] is True
    assert result["totp_secret"] == "JBSWY3DPEHPK3PXP"
    assert result["password_set"] is True
    assert result["password_path"] == "/api/accounts/password/add"
    assert session.password_body == {
        "username": "user@outlook.com",
        "password": "StrongPass123!",
    }
    assert session.password_add_body == {"password": "StrongPass123!"}
    assert session.mfa_enroll_body == {"factor_type": "totp"}
    assert session.mfa_activate_body["factor_type"] == "totp"
    assert len(session.mfa_activate_body["code"]) == 6
    assert session.closed is True
    sentinel = json.loads(session.create_headers["openai-sentinel-token"])
    assert sentinel["flow"] == "oauth_create_account"
    assert sentinel["c"] == "challenge-token"
    assert any("协议注册完成" in line for line in logs)
    assert any("TOTP enrollment/activate 成功，Base32 Secret: JBSWY3DPEHPK3PXP" in line for line in logs)
    assert any("设置的密码: StrongPass123!" in line for line in logs)


def test_protocol_profile_is_shared_by_headers_and_sentinel_fingerprint():
    logs = []
    worker = ChatGPTProtocolRegister(
        session=_FakeSession(),
        sentinel_runtime=False,
        log_fn=logs.append,
    )

    assert worker.user_agent == FIREFOX_UA
    assert worker.impersonate == FIREFOX_IMPERSONATE
    assert worker.platform == FIREFOX_PLATFORM
    assert any("三层指纹已对齐" in line for line in logs)
    assert worker.fingerprint_diagnostics == {
        "aligned": True,
        "tls": True,
        "headers": True,
        "sentinel": True,
    }
    assert worker.check_fingerprint_alignment() == worker.fingerprint_diagnostics

    common = worker._common_headers("https://auth.openai.com/email-verification")
    security = worker._chatgpt_security_headers("access-token", "/backend-api/test")
    for headers in (common, security):
        assert headers["user-agent"] == FIREFOX_UA
        assert headers["accept-language"] == FIREFOX_ACCEPT_LANGUAGE

    generator = _SentinelTokenGenerator(worker.user_agent)
    fingerprint = generator._fingerprint()
    reference = generator._reference_fingerprint()
    assert fingerprint[0] == SENTINEL_SCREEN
    assert fingerprint[4] == FIREFOX_UA
    assert fingerprint[8] == SENTINEL_LANGUAGE
    assert fingerprint[9] == SENTINEL_ACCEPT_LANGUAGE
    assert reference[4] == FIREFOX_UA
    assert reference[7] == SENTINEL_LANGUAGE
    assert reference[8] == SENTINEL_ACCEPT_LANGUAGE


def test_fingerprint_alignment_rejects_mixed_firefox_and_chrome_layers():
    report = check_alignment(
        impersonate=FIREFOX_IMPERSONATE,
        user_agent=FIREFOX_UA,
        sentinel_user_agent="Mozilla/5.0 Chrome/142.0.0.0",
    )
    assert report == {
        "aligned": False,
        "tls": True,
        "headers": True,
        "sentinel": False,
    }


def test_protocol_registration_accepts_current_chatgpt_otp_subjects():
    adapter = ChatGPTPlatform().build_protocol_mailbox_adapter()

    # Current messages are titled "Your temporary ChatGPT ... code" and may
    # not contain the old OpenAI brand keyword.
    assert adapter.otp_spec is not None
    assert adapter.otp_spec.keyword == ""


def test_sentinel_headers_include_vm_and_session_observer_tokens():
    class _FakeRuntime:
        def vm_tokens(self, chat_req, cached_proof):
            return {"t": "turnstile-proof", "so": "observer-proof"}

    client = OpenAISentinelClient(
        session=object(),
        user_agent="test-agent",
        use_browser_runtime=True,
    )
    client._browser_runtime = _FakeRuntime()
    client.session = type(
        "NoNetworkSession",
        (),
        {"post": lambda *args, **kwargs: None},
    )()

    # Bypass the network challenge and exercise the header assembly using a
    # deterministic VM result.
    def fake_post(*args, **kwargs):
        return _FakeResponse(
            payload={
                "token": "challenge",
                "proofofwork": {"required": False},
            }
        )

    client.session.post = fake_post
    headers = client.build_headers("device-1", "oauth_create_account")
    assert set(headers) == {
        "openai-sentinel-token",
        "openai-sentinel-so-token",
    }
    token = json.loads(headers["openai-sentinel-token"])
    so_token = json.loads(headers["openai-sentinel-so-token"])
    assert token["t"] == "turnstile-proof"
    assert so_token["so"] == "observer-proof"


def test_sentinel_runtime_uses_camoufox_and_releases_it(monkeypatch):
    events = []

    class _Page:
        def goto(self, *_args, **_kwargs):
            events.append("goto")

        def evaluate(self, expression, *_args):
            if expression == "typeof window.SentinelSDK":
                return "object"
            return None

    class _Browser:
        def new_page(self):
            return _Page()

    class _Camoufox:
        options = None

        def __init__(self, **options):
            type(self).options = options

        def __enter__(self):
            events.append("enter")
            return _Browser()

        def __exit__(self, *_args):
            events.append("exit")

    class _Session:
        def get(self, *_args, **_kwargs):
            return _FakeResponse(text="before t.token=ye,t}({}); after")

    monkeypatch.setitem(
        sys.modules,
        "camoufox.sync_api",
        types.SimpleNamespace(Camoufox=_Camoufox),
    )
    monkeypatch.setattr(_SentinelBrowserRuntime, "_sdk_code", None)

    runtime = _SentinelBrowserRuntime.create(
        _Session(),
        user_agent=FIREFOX_UA,
        proxy="http://name:pass@127.0.0.1:8080",
    )
    assert _Camoufox.options["headless"] is True
    assert _Camoufox.options["block_webrtc"] is True
    assert _Camoufox.options["os"] == "windows"
    assert _Camoufox.options["ff_version"] == 144
    assert _Camoufox.options["locale"] == ["en-US", "en"]
    assert _Camoufox.options["config"]["navigator.userAgent"] == FIREFOX_UA
    assert _Camoufox.options["config"]["headers.Accept-Language"] == FIREFOX_ACCEPT_LANGUAGE
    assert _Camoufox.options["proxy"] == {
        "server": "http://127.0.0.1:8080",
        "username": "name",
        "password": "pass",
    }

    runtime.close()
    runtime.close()
    assert events.count("enter") == 1
    assert events.count("exit") == 1


def test_sentinel_runtime_releases_failed_camoufox_startup(monkeypatch):
    events = []

    class _Camoufox:
        def __init__(self, **_options):
            pass

        def __enter__(self):
            events.append("enter")
            raise RuntimeError("Camoufox startup failed")

        def __exit__(self, *_args):
            events.append("exit")

    monkeypatch.setitem(
        sys.modules,
        "camoufox.sync_api",
        types.SimpleNamespace(Camoufox=_Camoufox),
    )

    try:
        _SentinelBrowserRuntime.create(
            object(), user_agent="unused-by-camoufox", proxy=None
        )
    except RuntimeError as exc:
        assert str(exc) == "Camoufox startup failed"
    else:
        raise AssertionError("expected Camoufox startup error")

    assert events == ["enter", "exit"]
