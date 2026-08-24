from concurrent.futures import ThreadPoolExecutor

import pytest

from core.domain_mailbox import (
    DomainImapMailbox,
    DomainMailboxConfigurationError,
    normalize_domain,
)


class FakeImap:
    def __init__(self, messages: dict[str, bytes]):
        self.messages = messages
        self.logged_out = False

    def uid(self, operation, *args):
        if operation.lower() == "search":
            return "OK", [" ".join(self.messages).encode("ascii")]
        if operation.lower() == "fetch":
            uid = str(args[0])
            raw = self.messages.get(uid)
            if raw is None:
                return "NO", [b"not found"]
            return "OK", [(f"{uid} (BODY[] {{{len(raw)}}})".encode("ascii"), raw), b")"]
        raise AssertionError(f"unexpected uid operation: {operation}")

    def logout(self):
        self.logged_out = True


class Fake2925Connection:
    def __init__(self):
        self.selected_folder = ""
        self.uid_calls = []
        self.logged_out = False

    def login(self, username, password):
        return "OK", [b"logged in"]

    def list(self):
        return "OK", [b'(\\HasNoChildren) "/" "Inbox"']

    def select(self, folder, readonly=True):
        self.selected_folder = folder
        return "OK", [b"32"]

    def uid(self, operation, *args):
        self.uid_calls.append((operation, args))
        if args == (None, "ALL"):
            return "OK", [b""]
        if args == (None, "UID", "1:*"):
            return "OK", [b"3 7 12 32"]
        raise AssertionError(f"unexpected UID call: {operation} {args}")

    def logout(self):
        self.logged_out = True


class UnsafeExamineConnection(Fake2925Connection):
    def __init__(self):
        super().__init__()
        self.select_calls = []

    def select(self, folder, readonly=True):
        self.selected_folder = folder
        self.select_calls.append((folder, readonly))
        if readonly:
            return "NO", [b"EXAMINE Unsafe Login. Please contact support@example.com for help"]
        return "OK", [b"12"]


def create_mailbox(**overrides) -> DomainImapMailbox:
    config = {
        "domain": "mail.example.com",
        "imap_host": "imap.example.com",
        "imap_port": 993,
        "imap_security": "ssl",
        "imap_username": "catchall@example.com",
        "imap_password": "test-imap-password",
        "poll_interval": 0,
        "request_timeout": 1,
    }
    config.update(overrides)
    return DomainImapMailbox(**config)


def raw_message(*, recipient_header: str, recipient: str, subject: str, body: str) -> bytes:
    return (
        f"From: Example Service <noreply@example.com>\r\n"
        f"{recipient_header}: {recipient}\r\n"
        f"To: catchall@example.com\r\n"
        f"Subject: {subject}\r\n"
        "MIME-Version: 1.0\r\n"
        "Content-Type: text/html; charset=utf-8\r\n"
        "Content-Transfer-Encoding: 8bit\r\n"
        "\r\n"
        f"<html><body>{body}</body></html>"
    ).encode("utf-8")


def test_normalize_domain_accepts_at_prefix_and_rejects_urls():
    assert normalize_domain("@Example.COM.") == "example.com"
    with pytest.raises(DomainMailboxConfigurationError):
        normalize_domain("https://example.com/inbox")


def test_get_email_generates_unique_addresses_concurrently_without_persisting_password():
    mailbox = create_mailbox(prefix="reg", prefix_length=10)

    with ThreadPoolExecutor(max_workers=10) as executor:
        accounts = list(executor.map(lambda _index: mailbox.get_email(), range(50)))

    addresses = {account.email for account in accounts}
    assert len(addresses) == 50
    assert all(address.startswith("reg-") and address.endswith("@mail.example.com") for address in addresses)
    assert all(
        account.extra["provider_account"]["credentials"] == {"email": account.email}
        for account in accounts
    )
    assert "test-imap-password" not in repr([account.extra for account in accounts])


def test_fixed_email_mode_returns_requested_address_and_rejects_concurrent_duplicate():
    mailbox = create_mailbox(domain="example.com", fixed_email="user@example.com")
    assert mailbox.get_email().email == "user@example.com"
    with pytest.raises(RuntimeError, match="不能并发重复注册"):
        mailbox.get_email()


def test_wait_for_code_uses_uid_baseline_and_exact_original_recipient(monkeypatch):
    mailbox = create_mailbox()
    account = mailbox.get_email()
    snapshots = [
        FakeImap({
            "1": raw_message(
                recipient_header="Delivered-To",
                recipient=account.email,
                subject="OpenAI old verification code",
                body="Your verification code is 111111",
            ),
        }),
        FakeImap({
            "1": raw_message(
                recipient_header="Delivered-To",
                recipient=account.email,
                subject="OpenAI old verification code",
                body="Your verification code is 111111",
            ),
            "2": raw_message(
                recipient_header="X-Original-To",
                recipient="someone-else@mail.example.com",
                subject="OpenAI verification code",
                body="Your verification code is 222222",
            ),
            "3": raw_message(
                recipient_header="X-Original-To",
                recipient=account.email.upper(),
                subject="OpenAI verification code",
                body="Your <strong>verification code</strong> is 654321",
            ),
        }),
    ]
    monkeypatch.setattr(mailbox, "_connect", lambda: snapshots.pop(0))

    before_ids = mailbox.get_current_ids(account)
    code = mailbox.wait_for_code(account, keyword="OpenAI", timeout=1, before_ids=before_ids)

    assert before_ids == {"1"}
    assert code == "654321"


def test_wait_for_link_reads_new_message_for_generated_recipient(monkeypatch):
    mailbox = create_mailbox()
    account = mailbox.get_email()
    connection = FakeImap({
        "8": raw_message(
            recipient_header="Envelope-To",
            recipient=account.email,
            subject="Verify your account",
            body='<a href="https://example.com/verify?id=8">Verify account</a>',
        ),
    })
    monkeypatch.setattr(mailbox, "_connect", lambda: connection)

    link = mailbox.wait_for_link(account, keyword="Verify", timeout=1, before_ids=set())

    assert link == "https://example.com/verify?id=8"


def test_wait_for_code_stops_before_opening_imap_when_cancelled(monkeypatch):
    mailbox = create_mailbox()
    account = mailbox.get_email()
    monkeypatch.setattr(
        mailbox,
        "_connect",
        lambda: (_ for _ in ()).throw(AssertionError("IMAP should not be opened")),
    )

    with pytest.raises(RuntimeError, match="任务已取消"):
        mailbox.wait_for_code(account, timeout=120, cancel_check=lambda: True)


def test_wait_for_code_uses_manual_code_before_opening_imap(monkeypatch):
    mailbox = create_mailbox()
    account = mailbox.get_email()
    monkeypatch.setattr(
        mailbox,
        "_connect",
        lambda: (_ for _ in ()).throw(AssertionError("IMAP should not be opened")),
    )

    assert mailbox.wait_for_code(
        account,
        timeout=120,
        manual_code_check=lambda: "482615",
    ) == "482615"


def test_wait_for_code_prefers_manual_code_when_automatic_code_is_ready(monkeypatch):
    mailbox = create_mailbox()
    account = mailbox.get_email()
    connection = FakeImap({
        "1": raw_message(
            recipient_header="Delivered-To",
            recipient=account.email,
            subject="OpenAI verification code",
            body="Your verification code is 111111",
        ),
    })
    checks = iter([None, None, "222222"])
    monkeypatch.setattr(mailbox, "_connect", lambda: connection)

    assert mailbox.wait_for_code(
        account,
        keyword="OpenAI",
        timeout=1,
        manual_code_check=lambda: next(checks, "222222"),
    ) == "222222"


def test_connect_resolves_configured_folder_case_from_server_list(monkeypatch):
    mailbox = create_mailbox(imap_folder="INBOX")
    connection = Fake2925Connection()
    monkeypatch.setattr(
        "core.domain_mailbox.imaplib.IMAP4_SSL",
        lambda *_args, **_kwargs: connection,
    )

    opened = mailbox._connect()

    assert opened is connection
    assert connection.selected_folder == "Inbox"
    assert mailbox._selected_folder(connection) == "Inbox"


def test_connect_falls_back_from_coremail_unsafe_examine_to_select(monkeypatch):
    mailbox = create_mailbox(imap_folder="INBOX")
    connection = UnsafeExamineConnection()
    monkeypatch.setattr(
        "core.domain_mailbox.imaplib.IMAP4_SSL",
        lambda *_args, **_kwargs: connection,
    )

    opened = mailbox._connect()

    assert opened is connection
    assert connection.select_calls == [("Inbox", True), ("Inbox", False)]


def test_list_uids_retries_uid_range_when_search_all_is_empty():
    mailbox = create_mailbox()
    connection = Fake2925Connection()

    assert mailbox._list_uids(connection) == ["3", "7", "12", "32"]
    assert connection.uid_calls == [
        ("search", (None, "ALL")),
        ("search", (None, "UID", "1:*")),
    ]


def test_test_connection_reports_folder_count_and_sample_address(monkeypatch):
    mailbox = create_mailbox(imap_folder="OTP")
    monkeypatch.setattr(mailbox, "_connect", lambda: FakeImap({"1": b"", "2": b""}))

    result = mailbox.test_connection()

    assert result["ok"] is True
    assert "OTP" in result["message"]
    assert result["email"].endswith("@mail.example.com")
