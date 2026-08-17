import time

from platforms._browser_backend import BrowserBackendConfig
from platforms.chatgpt.browser_register import (
    _apply_camoufox_visible_window_limit,
    _build_totp_otpauth,
    _generate_totp_code,
    _get_security_email_code,
    _resend_security_email_otp,
    _submit_security_password_api,
    _validate_security_email_otp,
    _setup_chatgpt_password,
    _setup_chatgpt_password_and_totp,
)


def test_apply_camoufox_visible_window_limit_sets_1280_by_720_window_for_headed_camoufox():
    launch_opts = {"headless": False}

    _apply_camoufox_visible_window_limit(
        launch_opts,
        BrowserBackendConfig.camoufox(headless=False),
    )

    assert launch_opts["window"] == (1280, 720)


def test_apply_camoufox_visible_window_limit_skips_headless_camoufox():
    launch_opts = {"headless": True}

    _apply_camoufox_visible_window_limit(
        launch_opts,
        BrowserBackendConfig.camoufox(headless=True),
    )

    assert "window" not in launch_opts


def test_apply_camoufox_visible_window_limit_skips_bitbrowser():
    launch_opts = {"headless": False}

    _apply_camoufox_visible_window_limit(
        launch_opts,
        BrowserBackendConfig.bitbrowser(profile_id="profile-1"),
    )

    assert "window" not in launch_opts


def test_chatgpt_totp_code_matches_rfc6238_vector():
    # Base32 for 12345678901234567890; RFC 6238 SHA-1 vector at t=59.
    assert _generate_totp_code("GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ", timestamp=59) == "287082"


def test_chatgpt_otpauth_uri_matches_user_script_shape():
    assert _build_totp_otpauth("user@example.com", "JBSWY3DPEHPK3PXP") == (
        "otpauth://totp/OpenAI:user%40example.com?secret=JBSWY3DPEHPK3PXP"
        "&issuer=OpenAI&algorithm=SHA1&digits=6&period=30"
    )


def test_chatgpt_reauth_otp_rejects_registration_code_and_waits_for_new_code():
    returned = iter(["001454", "987654"])
    calls = []
    logs = []

    def callback(**kwargs):
        calls.append(kwargs)
        return next(returned)

    callback.supports_timeout_override = True

    assert _get_security_email_code(
        callback,
        excluded_codes={"001454"},
        deadline=time.time() + 5,
        log=logs.append,
    ) == "987654"
    assert len(calls) == 2
    assert all("timeout_override" in call for call in calls)
    assert any("忽略注册阶段已使用的邮箱验证码" in message for message in logs)


def test_chatgpt_reauth_email_otp_uses_resend_and_validate_api(monkeypatch):
    requests = []
    responses = iter(
        [
            {"ok": True, "status": 200, "data": {}, "text": ""},
            {"ok": True, "status": 200, "data": {"continue_url": "https://auth.openai.com/reset-password/new-password"}, "text": ""},
        ]
    )
    page = type("Page", (), {"url": "https://auth.openai.com/email-verification"})()

    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._build_browser_sentinel_headers",
        lambda _page, flow, _log: {"OpenAI-Sentinel-Token": flow},
    )

    def fake_fetch(_page, url, **kwargs):
        requests.append((url, kwargs))
        return next(responses)

    monkeypatch.setattr("platforms.chatgpt.browser_register._browser_fetch", fake_fetch)

    _resend_security_email_otp(page, lambda _message: None)
    validation = _validate_security_email_otp(page, "987654", lambda _message: None)

    assert validation["ok"] is True
    assert requests[0][0].endswith("/api/accounts/email-otp/resend")
    assert requests[0][1]["method"] == "POST"
    assert requests[0][1]["body"] == "{}"
    assert requests[1][0].endswith("/api/accounts/email-otp/validate")
    assert '"code":"987654"' in requests[1][1]["body"]
    assert requests[1][1]["headers"]["OpenAI-Sentinel-Token"] == "email_otp_validate"


def test_chatgpt_security_step_sets_totp_before_password(monkeypatch):
    calls = []
    logs = []

    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._chatgpt_security_session",
        lambda page, log: {"accessToken": "access", "user": {"email": "user@example.com"}},
    )

    def setup_totp(**kwargs):
        calls.append("totp")
        assert kwargs["session_data"]["accessToken"] == "access"
        return {
            "totp_set": True,
            "totp_secret": "JBSWY3DPEHPK3PXP",
            "otpauth": "otpauth://totp/example",
        }

    def setup_password(**kwargs):
        calls.append("password")
        assert kwargs["secret"] == "JBSWY3DPEHPK3PXP"
        return {"password_set": True, "password_path": "dom"}

    monkeypatch.setattr("platforms.chatgpt.browser_register._setup_chatgpt_totp", setup_totp)
    monkeypatch.setattr("platforms.chatgpt.browser_register._setup_chatgpt_password", setup_password)

    result = _setup_chatgpt_password_and_totp(
        object(),
        email="user@example.com",
        password="StrongPass123!",
        otp_callback=None,
        log=logs.append,
    )

    assert calls == ["totp", "password"]
    assert result["password_set"] is True
    assert result["totp_set"] is True
    assert "设置的密码: StrongPass123!" in logs


def test_chatgpt_password_reauths_before_dom_submit(monkeypatch):
    calls = []
    page = type("Page", (), {"url": "https://chatgpt.com/"})()

    def start_reauth(_page, email, params, callback_url, _log):
        calls.append(("reauth", email, params, callback_url))
        page.url = "https://auth.openai.com/reset-password/new-password"
        return page.url

    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._start_chatgpt_security_reauth",
        start_reauth,
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._complete_security_reauth",
        lambda _page, **kwargs: calls.append(("complete", kwargs["secret"], kwargs["expect_password_page"])),
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._submit_security_password_dom",
        lambda _page, **kwargs: calls.append(("dom", kwargs["secret"])) or True,
    )
    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._goto_with_retry",
        lambda _page, url, **kwargs: calls.append(("return", url)),
    )

    result = _setup_chatgpt_password(
        page,
        email="user@example.com",
        password="StrongPass123!",
        secret="JBSWY3DPEHPK3PXP",
        otp_callback=lambda: "old-email-code",
        log=lambda _message: None,
    )

    assert result == {"password_set": True, "password_path": "dom"}
    assert [item[0] for item in calls] == ["reauth", "complete", "dom", "return"]
    assert calls[0][2] == {
        "connection": "password",
        "reauth": "password",
        "post_login_add_password": "true",
        "prompt": "login",
        "max_age": "0",
    }
    assert calls[1] == ("complete", "JBSWY3DPEHPK3PXP", True)
    assert calls[2] == ("dom", "JBSWY3DPEHPK3PXP")


def test_chatgpt_password_api_falls_back_from_add_to_reset(monkeypatch):
    paths = []
    responses = iter(
        [
            {"ok": False, "status": 400, "data": {"error": "unsupported"}, "text": ""},
            {"ok": True, "status": 200, "data": {"continue_url": "https://chatgpt.com/"}, "text": ""},
        ]
    )

    monkeypatch.setattr(
        "platforms.chatgpt.browser_register._build_browser_sentinel_headers",
        lambda page, flow, log: {"OpenAI-Sentinel-Token": "sentinel"},
    )

    def fake_fetch(page, url, **kwargs):
        paths.append(url)
        return next(responses)

    monkeypatch.setattr("platforms.chatgpt.browser_register._browser_fetch", fake_fetch)

    result = _submit_security_password_api(
        object(),
        "StrongPass123!",
        "add",
        lambda _message: None,
    )

    assert result["ok"] is True
    assert paths == [
        "https://auth.openai.com/api/accounts/password/add",
        "https://auth.openai.com/api/accounts/password/reset",
    ]
