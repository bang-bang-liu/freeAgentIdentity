from types import SimpleNamespace

from core.base_identity import MailboxIdentityProvider


def test_mailbox_identity_allows_manual_otp_when_initial_imap_baseline_fails():
    account = SimpleNamespace(email="fixed@example.com")

    class Mailbox:
        def get_email(self):
            return account

        def get_current_ids(self, _account):
            raise RuntimeError("EXAMINE Unsafe Login")

        def release_email(self, _account):
            raise AssertionError("manual fallback should retain the mailbox")

    identity = MailboxIdentityProvider(
        mailbox=Mailbox(),
        extra={"_manual_otp_handle": SimpleNamespace(open_request=lambda _email: "request")},
    ).resolve()

    assert identity.email == "fixed@example.com"
    assert identity.before_ids == set()
