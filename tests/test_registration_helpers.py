from types import SimpleNamespace

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
