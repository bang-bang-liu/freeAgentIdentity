from types import SimpleNamespace

from rebind_core import pipeline


def _login(email: str, access_token: str):
    result = SimpleNamespace(
        access_token=access_token,
        session_token="session-sensitive-placeholder",
        id_token="id-sensitive-placeholder",
        refresh_token="refresh-sensitive-placeholder",
        auth_session_json='{"user":{"email":"%s"}}' % email,
        device_id="device-sensitive-placeholder",
    )
    return SimpleNamespace(
        email=email,
        password="test-password",
        totp_secret="TESTSECRET",
        result=result,
        access_token=access_token,
        session_token=result.session_token,
        device_id=result.device_id,
        cookie_header="cookie-sensitive-placeholder",
        account_id="acct_placeholder",
        factor_id="factor_placeholder",
    )


def test_rebind_pipeline_runs_protocol_and_returns_new_credentials(monkeypatch):
    calls = []
    logs = []
    old_login = _login("old@example.com", "old-at-sensitive-placeholder")
    new_login = _login("new@example.com", "new-at-sensitive-placeholder")

    def fake_login(email, password, totp_secret, *, proxy=None):
        calls.append(("login", email, proxy))
        return old_login if email == "old@example.com" else new_login

    class FakeClient:
        def __init__(self, login, session_id=""):
            calls.append(("client", login.email))

        def eligibility(self):
            calls.append(("eligibility",))
            return {"eligible": True, "eligibility_type": "password"}

        def begin(self, email):
            calls.append(("begin", email))
            return {"ok": True}

        def verify(self, email, code):
            calls.append(("verify", email, code))
            return {"ok": True}

    monkeypatch.setattr(pipeline, "login_with_password_and_totp", fake_login)
    monkeypatch.setattr(pipeline, "ChangeEmailClient", FakeClient)
    monkeypatch.setattr(pipeline, "wait_code", lambda *args, **kwargs: "123456")
    monkeypatch.setattr(
        pipeline,
        "build_login_bundle",
        lambda login, rebind_email="": {"email": rebind_email},
    )

    result = pipeline.run_rebind_email(
        old_email="old@example.com",
        password="test-password",
        totp_secret="TESTSECRET",
        new_email="new@example.com",
        mail_api="https://mail-api.example.com/messages?token=placeholder",
        proxy="http://proxy.example.com:8080",
        log_fn=logs.append,
        write_artifacts=False,
    )

    assert result.ok is True
    assert result.access_token == "new-at-sensitive-placeholder"
    assert result.session_token == "session-sensitive-placeholder"
    assert ("begin", "new@example.com") in calls
    assert ("verify", "new@example.com", "123456") in calls
    joined_logs = "\n".join(logs)
    assert "test-password" not in joined_logs
    assert "TESTSECRET" not in joined_logs
    assert "mail-api.example.com" not in joined_logs
    assert "proxy.example.com" not in joined_logs
