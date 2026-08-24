from types import SimpleNamespace
import threading
import time

from core.registration.helpers import build_otp_callback


def test_otp_callback_advances_mailbox_baseline_after_consuming_code():
    account = object()
    calls = []

    class Mailbox:
        def wait_for_code(self, current_account, **kwargs):
            assert current_account is account
            assert kwargs["before_ids"] == {"old-message"}
            calls.append("wait")
            return "123456"

        def get_current_ids(self, current_account):
            assert current_account is account
            calls.append("refresh")
            return {"new-message"}

    identity = SimpleNamespace(mailbox_account=account, before_ids={"old-message"})
    platform = SimpleNamespace(mailbox=Mailbox(), is_cancel_requested=None)
    ctx = SimpleNamespace(
        platform=platform,
        identity=identity,
        log=lambda _message: None,
    )

    callback = build_otp_callback(ctx)

    assert callback() == "123456"
    assert calls == ["wait", "refresh"]
    assert identity.before_ids == {"new-message"}


def test_otp_callback_registers_manual_request_and_logs_code_for_debugging():
    account = SimpleNamespace(email="generated@example.com")
    log_messages = []
    closed = []
    manual_ready = threading.Event()
    baseline_refreshed = threading.Event()

    class ManualHandle:
        def open_request(self, email):
            assert email == account.email
            return "request-1"

        def consume_code(self, request_id):
            assert request_id == "request-1"
            return "654321" if manual_ready.is_set() else None

        def close_request(self, request_id):
            closed.append(request_id)

    class Mailbox:
        def wait_for_code(self, current_account, **kwargs):
            assert current_account is account
            manual_ready.set()
            while not kwargs["cancel_check"]():
                time.sleep(0.01)
            raise RuntimeError("automatic polling cancelled")

        def get_current_ids(self, current_account):
            assert current_account is account
            time.sleep(0.5)
            baseline_refreshed.set()
            return {"new-message"}

    ctx = SimpleNamespace(
        platform=SimpleNamespace(mailbox=Mailbox(), is_cancel_requested=None),
        identity=SimpleNamespace(mailbox_account=account, before_ids=set()),
        extra={"_manual_otp_handle": ManualHandle()},
        log=log_messages.append,
    )

    callback = build_otp_callback(ctx)

    started_at = time.monotonic()
    assert callback() == "654321"
    assert time.monotonic() - started_at < 0.3
    assert closed == ["request-1"]
    assert any("验证码=654321" in message for message in log_messages)
    assert baseline_refreshed.wait(2)
    assert ctx.identity.before_ids == {"new-message"}


def test_otp_callback_keeps_manual_request_open_after_automatic_mailbox_failure():
    account = SimpleNamespace(email="fixed@example.com")
    submitted = threading.Event()
    closed = []

    class ManualHandle:
        def open_request(self, _email):
            return "request-1"

        def consume_code(self, _request_id):
            return "807632" if submitted.is_set() else None

        def close_request(self, request_id):
            closed.append(request_id)

    class Mailbox:
        def wait_for_code(self, _account, **_kwargs):
            raise RuntimeError("IMAP 无法打开邮箱目录 'INBOX': EXAMINE Unsafe Login")

        def get_current_ids(self, _account):
            return set()

    ctx = SimpleNamespace(
        platform=SimpleNamespace(mailbox=Mailbox(), is_cancel_requested=None),
        identity=SimpleNamespace(mailbox_account=account, before_ids=set()),
        extra={"_manual_otp_handle": ManualHandle()},
        log=lambda _message: None,
    )
    callback = build_otp_callback(ctx, timeout=2)

    timer = threading.Timer(0.1, submitted.set)
    timer.start()
    try:
        assert callback() == "807632"
    finally:
        timer.cancel()
    assert closed == ["request-1"]
