"""幂等补设密码/TOTP 的测试。

覆盖设计 13.1：两项都存在时不调任何接口、只缺一项时只补那一项、enrollment
成功但 activate 失败不保存 secret、已有密码不覆盖。
"""
from platforms.chatgpt import ensure_security as sec
from platforms.chatgpt.login_types import (
    ChatGPTLoginResult,
    SecuritySetupStatus,
)


def _session() -> ChatGPTLoginResult:
    return ChatGPTLoginResult(
        access_token="at-sensitive-placeholder",
        cookie_header="cookie-sensitive-placeholder",
        device_id="device-placeholder",
    )


def _password_ok(calls: list):
    def setter(*, email, password, session):
        calls.append(("password", email, len(password)))
        return {"password_set": True}

    return setter


def _totp_ok(calls: list, secret: str = "NEWSECRET234567"):
    def enabler(*, access_token, cookie_header, device_id, proxy):
        calls.append(("totp", access_token, proxy))
        # 键名与 MfaProtocolResult.to_dict() 一致，见 mfa_totp_protocol.py:76。
        return {"ok": True, "secret": secret, "otpauth_uri": "otpauth://totp/x"}

    return enabler


# ------------------------------------------------------------ 计划判定

def test_plan_matches_design_table():
    assert sec.plan_security_setup(has_password=True, has_totp=True) == "skip"
    assert sec.plan_security_setup(has_password=True, has_totp=False) == "add_totp"
    assert sec.plan_security_setup(has_password=False, has_totp=True) == "add_password"
    assert (
        sec.plan_security_setup(has_password=False, has_totp=False)
        == "add_password_then_totp"
    )


# ------------------------------------------------------------ 幂等跳过

def test_both_present_calls_nothing():
    calls: list = []
    result = sec.ensure_security(
        email="a@example.com",
        session=_session(),
        has_password=True,
        has_totp=True,
        password_setter=_password_ok(calls),
        totp_enabler=_totp_ok(calls),
    )

    assert calls == []
    assert result.security_setup_status == SecuritySetupStatus.NOT_NEEDED
    assert result.password_created is False
    assert result.totp_created is False


def test_password_present_only_adds_totp():
    calls: list = []
    result = sec.ensure_security(
        email="a@example.com",
        session=_session(),
        has_password=True,
        has_totp=False,
        password_setter=_password_ok(calls),
        totp_enabler=_totp_ok(calls),
    )

    assert [c[0] for c in calls] == ["totp"]
    assert result.security_setup_status == SecuritySetupStatus.COMPLETED
    assert result.totp_created is True
    assert result.totp_secret == "NEWSECRET234567"
    assert result.otpauth == "otpauth://totp/x"
    assert result.password_created is False
    assert result.new_password == ""


def test_totp_present_only_adds_password():
    calls: list = []
    result = sec.ensure_security(
        email="a@example.com",
        session=_session(),
        has_password=False,
        has_totp=True,
        password_setter=_password_ok(calls),
        totp_enabler=_totp_ok(calls),
    )

    assert [c[0] for c in calls] == ["password"]
    assert result.security_setup_status == SecuritySetupStatus.COMPLETED
    assert result.password_created is True
    assert result.totp_created is False
    assert result.totp_secret == ""


def test_both_missing_adds_password_then_totp_in_order():
    calls: list = []
    result = sec.ensure_security(
        email="a@example.com",
        session=_session(),
        has_password=False,
        has_totp=False,
        password_setter=_password_ok(calls),
        totp_enabler=_totp_ok(calls),
    )

    assert [c[0] for c in calls] == ["password", "totp"]
    assert result.security_setup_status == SecuritySetupStatus.COMPLETED
    assert result.password_created is True
    assert result.totp_created is True


# ------------------------------------------------------------ 失败语义

def test_activate_failure_does_not_persist_secret():
    """enrollment 拿到 secret 不代表成功，activate 失败必须不保存。"""

    def enabler(*, access_token, cookie_header, device_id, proxy):
        return {"ok": False, "secret": "ENROLLED-BUT-NOT-ACTIVE", "error": "invalid_code"}

    result = sec.ensure_security(
        email="a@example.com",
        session=_session(),
        has_password=True,
        has_totp=False,
        totp_enabler=enabler,
    )

    assert result.totp_secret == ""
    assert result.totp_created is False
    assert result.security_setup_status == SecuritySetupStatus.FAILED


def test_already_enabled_is_idempotent_success_without_fake_secret():
    def enabler(*, access_token, cookie_header, device_id, proxy):
        return {"ok": True, "mfa_enabled": True, "error": "already_enabled"}

    result = sec.ensure_security(
        email="a@example.com",
        session=_session(),
        has_password=True,
        has_totp=False,
        totp_enabler=enabler,
    )

    assert result.totp_set is True
    assert result.totp_created is False
    assert result.totp_secret == ""
    assert result.security_setup_status == SecuritySetupStatus.COMPLETED


def test_password_success_but_totp_failure_is_partial():
    calls: list = []

    def failing_totp(*, access_token, cookie_header, device_id, proxy):
        raise RuntimeError("mfa/enroll HTTP 500")

    result = sec.ensure_security(
        email="a@example.com",
        session=_session(),
        has_password=False,
        has_totp=False,
        password_setter=_password_ok(calls),
        totp_enabler=failing_totp,
    )

    assert result.password_created is True
    assert result.totp_created is False
    assert result.security_setup_status == SecuritySetupStatus.PARTIAL


def test_password_setter_raising_marks_failed():
    def failing_setter(*, email, password, session):
        raise RuntimeError("password/add HTTP 400")

    result = sec.ensure_security(
        email="a@example.com",
        session=_session(),
        has_password=False,
        has_totp=True,
        password_setter=failing_setter,
    )

    assert result.password_created is False
    assert result.security_setup_status == SecuritySetupStatus.FAILED


def test_unconfirmed_password_is_not_treated_as_created():
    def vague_setter(*, email, password, session):
        return {"password_set": False}

    result = sec.ensure_security(
        email="a@example.com",
        session=_session(),
        has_password=False,
        has_totp=True,
        password_setter=vague_setter,
    )

    assert result.password_created is False
    assert result.new_password == ""
    assert result.security_setup_status == SecuritySetupStatus.FAILED


# ------------------------------------------------------------ 密码与脱敏

def test_generated_password_satisfies_policy():
    for _ in range(20):
        password = sec.generate_password()
        assert 16 <= len(password) <= 24
        assert any(c.islower() for c in password)
        assert any(c.isupper() for c in password)
        assert any(c.isdigit() for c in password)
        assert any(c in "!@#$%^&*-_=+" for c in password)


def test_take_new_password_clears_it_from_result():
    calls: list = []
    result = sec.ensure_security(
        email="a@example.com",
        session=_session(),
        has_password=False,
        has_totp=True,
        password_setter=_password_ok(calls),
    )

    password = sec.take_new_password(result)
    assert len(password) >= 16
    assert result.new_password == ""
    assert sec.take_new_password(result) == ""


def test_public_dict_excludes_secrets():
    calls: list = []
    result = sec.ensure_security(
        email="a@example.com",
        session=_session(),
        has_password=False,
        has_totp=False,
        password_setter=_password_ok(calls),
        totp_enabler=_totp_ok(calls),
    )

    public = result.public_dict()
    serialized = repr(public)
    assert "NEWSECRET234567" not in serialized
    assert result.new_password not in serialized or result.new_password == ""
    assert "totp_secret" not in public
    assert "new_password" not in public
    assert public["totp_created"] is True


def test_logs_do_not_leak_password_or_secret():
    logs: list = []
    calls: list = []
    sec.ensure_security(
        email="a@example.com",
        session=_session(),
        has_password=False,
        has_totp=False,
        password_setter=_password_ok(calls),
        totp_enabler=_totp_ok(calls),
        log=logs.append,
    )

    joined = "\n".join(logs)
    assert "NEWSECRET234567" not in joined
    assert "password=created" in joined
    assert "totp=created" in joined
