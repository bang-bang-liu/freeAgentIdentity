"""刷新登录客户端的纯单元测试：不发网络请求，全部用假对象。

覆盖设计 13.1 的相关项：JWT exp 解析、非 JWT 不硬编码七天、取码只接受新
邮件、手动兜底可恢复、日志与结果不含敏感串。
"""
import base64
import json
import time

import pytest

from platforms.chatgpt import login_client
from platforms.chatgpt.login_types import ChatGPTLoginError, LoginErrorCode


def _jwt(payload: dict) -> str:
    def seg(data: dict) -> str:
        raw = json.dumps(data, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return f"{seg({'alg': 'RS256'})}.{seg(payload)}.signature-placeholder"


class FakeMailbox:
    def __init__(self, code: str = "482615", ids=None, raises: bool = False):
        self.code = code
        self.ids = ids or {"m1"}
        self.raises = raises
        self.calls = []

    def get_current_ids(self, account):
        return set(self.ids)

    def wait_for_code(self, account, **kwargs):
        self.calls.append(kwargs)
        if self.raises:
            raise RuntimeError("mailbox unavailable")
        return self.code


class FakeRebuilt:
    def __init__(self, mailbox=None, ok: bool = True, email: str = "old@example.com"):
        self.ok = ok
        self.mailbox = mailbox
        self.account = object()
        self.email = email


class FakeManualHandle:
    def __init__(self, code: str = "135790", delay_polls: int = 0):
        self.code = code
        self.delay_polls = delay_polls
        self.polls = 0
        self.opened = []
        self.closed = []

    def open_request(self, email):
        self.opened.append(email)
        return "req-1"

    def consume_code(self, request_id):
        self.polls += 1
        if self.polls > self.delay_polls:
            return self.code
        return None

    def close_request(self, request_id):
        self.closed.append(request_id)


# ------------------------------------------------------------ JWT exp 解析

def test_parses_expiry_from_jwt_exp():
    token = _jwt({"exp": 1789000000})
    assert login_client.parse_access_token_expiry(token) == "2026-09-10T00:26:40Z"


def test_non_jwt_token_yields_empty_expiry_not_seven_days():
    assert login_client.parse_access_token_expiry("opaque-token-value") == ""
    assert login_client.parse_access_token_expiry("") == ""
    assert login_client.parse_access_token_expiry("a.b") == ""


def test_jwt_without_exp_yields_empty_expiry():
    assert login_client.parse_access_token_expiry(_jwt({"sub": "user_1"})) == ""


def test_jwt_with_garbage_exp_yields_empty_expiry():
    assert login_client.parse_access_token_expiry(_jwt({"exp": "soon"})) == ""
    assert login_client.parse_access_token_expiry(_jwt({"exp": 0})) == ""


def test_mask_email_keeps_only_first_char():
    assert login_client.mask_email("alice@example.com") == "a***@example.com"
    assert login_client.mask_email("") == ""
    assert login_client.mask_email("not-an-email") == "***"


# ---------------------------------------------------------------- 取码解析

def test_auto_fetch_passes_baseline_so_only_new_mail_is_accepted():
    mailbox = FakeMailbox(ids={"old-1", "old-2"})
    resolver = login_client.EmailOtpResolver(rebuilt=FakeRebuilt(mailbox))

    assert resolver.fetch() == "482615"
    assert resolver.last_source == "auto"
    assert mailbox.calls[0]["before_ids"] == {"old-1", "old-2"}


def test_auto_fetch_forwards_pre_send_baseline_and_issue_time():
    mailbox = FakeMailbox(ids={"old-1"})
    resolver = login_client.EmailOtpResolver(rebuilt=FakeRebuilt(mailbox))

    assert resolver.fetch(
        before_ids={"before-authorize"},
        issued_after=1789477281.75,
    ) == "482615"
    assert mailbox.calls[0]["before_ids"] == {"before-authorize"}
    assert mailbox.calls[0]["issued_after"] == 1789477281.75


def test_auto_failure_falls_back_to_manual():
    mailbox = FakeMailbox(raises=True)
    handle = FakeManualHandle()
    logs = []
    resolver = login_client.EmailOtpResolver(
        rebuilt=FakeRebuilt(mailbox), manual_handle=handle, log=logs.append
    )

    assert resolver.fetch() == "135790"
    assert resolver.last_source == "manual"
    assert handle.opened == ["old@example.com"]
    assert handle.closed == ["req-1"]


def test_manual_only_mode_skips_mailbox_entirely():
    mailbox = FakeMailbox()
    handle = FakeManualHandle()
    resolver = login_client.EmailOtpResolver(
        rebuilt=FakeRebuilt(mailbox), manual_handle=handle, mode="manual"
    )

    assert resolver.fetch() == "135790"
    assert mailbox.calls == []


def test_manual_poll_waits_until_code_arrives():
    handle = FakeManualHandle(delay_polls=3)
    resolver = login_client.EmailOtpResolver(
        manual_handle=handle, mode="manual", timeout=5
    )

    assert resolver.fetch() == "135790"
    assert handle.polls == 4


def test_manual_timeout_raises_manual_otp_timeout():
    class NeverHandle(FakeManualHandle):
        def consume_code(self, request_id):
            return None

    resolver = login_client.EmailOtpResolver(
        manual_handle=NeverHandle(), mode="manual", timeout=1
    )
    with pytest.raises(ChatGPTLoginError) as exc:
        resolver.fetch()
    assert exc.value.code == LoginErrorCode.MANUAL_OTP_TIMEOUT


def test_no_source_at_all_raises_email_otp_invalid():
    resolver = login_client.EmailOtpResolver(mode="auto")
    with pytest.raises(ChatGPTLoginError) as exc:
        resolver.fetch()
    assert exc.value.code == LoginErrorCode.EMAIL_OTP_INVALID


def test_cancel_during_manual_wait_raises_cancelled():
    handle = FakeManualHandle(delay_polls=99)
    resolver = login_client.EmailOtpResolver(
        manual_handle=handle,
        mode="manual",
        timeout=5,
        cancel_check=lambda: True,
    )
    with pytest.raises(ChatGPTLoginError) as exc:
        resolver.fetch()
    assert exc.value.code == LoginErrorCode.CANCELLED
    assert handle.closed == ["req-1"]


def test_describe_reports_capability_without_secrets():
    resolver = login_client.EmailOtpResolver(
        rebuilt=FakeRebuilt(FakeMailbox()), manual_handle=FakeManualHandle()
    )
    assert resolver.describe() == "auto=yes, manual=yes"

    bare = login_client.EmailOtpResolver()
    assert bare.describe() == "auto=no, manual=no"


def test_baseline_survives_mailbox_error():
    class Boom(FakeMailbox):
        def get_current_ids(self, account):
            raise RuntimeError("boom")

    resolver = login_client.EmailOtpResolver(rebuilt=FakeRebuilt(Boom()))
    assert resolver.baseline() == set()


def test_logs_never_contain_the_code():
    logs = []
    resolver = login_client.EmailOtpResolver(
        rebuilt=FakeRebuilt(FakeMailbox(code="999888")), log=logs.append
    )
    resolver.fetch()

    joined = "\n".join(logs)
    assert "999888" not in joined
    assert "邮箱验证码已自动获取" in joined


def test_manual_log_masks_email_and_states_purpose():
    logs = []
    resolver = login_client.EmailOtpResolver(
        rebuilt=FakeRebuilt(None, ok=False, email="alice@example.com"),
        manual_handle=FakeManualHandle(),
        log=logs.append,
    )
    resolver.fetch(purpose="security_reauth")

    joined = "\n".join(logs)
    assert "a***@example.com" in joined
    assert "alice@example.com" not in joined
    assert "security_reauth" in joined
