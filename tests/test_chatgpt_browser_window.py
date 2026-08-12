from platforms._browser_backend import BrowserBackendConfig
from platforms.chatgpt.browser_register import (
    _apply_camoufox_visible_window_limit,
    _build_totp_otpauth,
    _generate_totp_code,
    _submit_security_password_api,
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


def test_chatgpt_security_step_sets_totp_before_password(monkeypatch):
    calls = []

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
        log=lambda _message: None,
    )

    assert calls == ["totp", "password"]
    assert result["password_set"] is True
    assert result["totp_set"] is True


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
