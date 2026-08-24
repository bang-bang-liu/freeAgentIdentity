from core.base_mailbox import MailboxAccount
from core.local_ms_mailbox import LocalMicrosoftMailboxPool, parse_local_ms_pool_rows


def test_parse_local_ms_pool_rows_accepts_gujumpgate_hotmail_format():
    rows = parse_local_ms_pool_rows(
        "\n".join(
            [
                "account----password----ID----Token",
                "user@example.com----test-password----test-client-id----test-refresh-token",
            ]
        )
    )

    assert len(rows) == 1
    entry = rows[0]
    assert entry.email == "user@example.com"
    assert entry.password == "test-password"
    assert entry.login_account == "user@example.com"
    assert entry.client_id == "test-client-id"
    assert entry.refresh_token == "test-refresh-token"
    assert entry.source_format == "gujumpgate_hotmail"
    assert entry.graph_ready is True
    assert entry.imap_ready is False


def test_parse_local_ms_pool_rows_accepts_xinlan_common_format():
    columns = [""] * 19
    columns[0] = "common@example.com"
    columns[1] = "test-password"
    columns[2] = "common@example.com"
    columns[3] = "outlook.office365.com"
    columns[4] = "993"
    columns[6] = "ssl"
    columns[16] = "common-client-id"
    columns[17] = "test-refresh-token"

    rows = parse_local_ms_pool_rows("----".join(columns))

    assert len(rows) == 1
    entry = rows[0]
    assert entry.email == "common@example.com"
    assert entry.client_id == "common-client-id"
    assert entry.refresh_token == "test-refresh-token"
    assert entry.source_format == "xinlan_common"
    assert entry.graph_ready is True
    assert entry.imap_ready is True


def test_local_ms_pool_records_gujumpgate_source_metadata(tmp_path):
    pool = LocalMicrosoftMailboxPool(
        pool_text="user@example.com----test-password----test-client-id----test-refresh-token",
        state_file=str(tmp_path / "state.json"),
    )

    account = pool.get_email()
    provider_account = account.extra["provider_account"]
    provider_resource = account.extra["provider_resource"]

    assert provider_account["credentials"]["client_id"] == "test-client-id"
    assert provider_account["credentials"]["refresh_token"] == "test-refresh-token"
    assert provider_account["metadata"]["source"] == "gujumpgate_hotmail"
    assert provider_resource["metadata"]["source"] == "gujumpgate_hotmail"


def test_local_ms_pool_allocates_six_outlook_child_addresses_per_parent(tmp_path):
    pool = LocalMicrosoftMailboxPool(
        pool_text="parent@example.com----test-password----test-client-id----test-refresh-token",
        state_file=str(tmp_path / "state.json"),
        alias_count=6,
    )

    accounts = [pool.get_email() for _ in range(6)]

    assert [item.email for item in accounts] == [
        f"parent+reg{index}@example.com" for index in range(1, 7)
    ]
    assert {item.account_id for item in accounts} == {
        f"parent@example.com#sub-{index}" for index in range(1, 7)
    }
    assert all(
        item.extra["provider_account"]["credentials"]["email"] == "parent@example.com"
        for item in accounts
    )
    assert all(
        item.extra["provider_resource"]["metadata"]["parent_email"] == "parent@example.com"
        for item in accounts
    )

    try:
        pool.get_email()
    except RuntimeError as exc:
        assert "已用尽" in str(exc)
    else:
        raise AssertionError("the seventh child address should not be allocated")


def test_local_ms_allow_reuse_still_excludes_active_addresses(tmp_path):
    pool = LocalMicrosoftMailboxPool(
        pool_text="parent@example.com----test-password----test-client-id----test-refresh-token",
        state_file=str(tmp_path / "state.json"),
        allow_reuse=True,
    )
    first = pool.get_email()
    try:
        pool.get_email()
    except RuntimeError as exc:
        assert "已用尽" in str(exc)
    else:
        raise AssertionError("an active mailbox must not be handed out twice")
    pool.release_email(first)
    assert pool.get_email().email == first.email


def test_local_ms_pool_manual_reset_removes_used_state(tmp_path):
    pool = LocalMicrosoftMailboxPool(
        pool_text="parent@example.com----test-password----test-client-id----test-refresh-token",
        state_file=str(tmp_path / "state.json"),
    )
    account = pool.get_email()
    assert pool.list_mailboxes()[0]["used"] is True
    pool.release_email(account)
    pool.set_mailbox_used(account.account_id, used=False)
    assert pool.list_mailboxes()[0]["used"] is False


def test_child_mailbox_otp_filter_matches_only_the_assigned_recipient():
    account = MailboxAccount(email="parent+reg2@example.com")

    assert LocalMicrosoftMailboxPool._message_is_for_account(
        {"toRecipients": [{"emailAddress": {"address": "parent+reg2@example.com"}}]},
        account,
    )
    assert not LocalMicrosoftMailboxPool._message_is_for_account(
        {"toRecipients": [{"emailAddress": {"address": "parent+reg1@example.com"}}]},
        account,
    )


def test_graph_access_token_tries_fallback_endpoint(monkeypatch):
    calls = []

    class FakeResponse:
        def __init__(self, status_code, payload=None, text=""):
            self.status_code = status_code
            self._payload = payload or {}
            self.text = text

        def json(self):
            return self._payload

    def fake_post(url, data, proxies=None, timeout=None):
        calls.append((url, data))
        if len(calls) == 1:
            return FakeResponse(400, text='{"error":"invalid_request"}')
        return FakeResponse(200, {"access_token": "access-token-ok"})

    monkeypatch.setattr("core.local_ms_mailbox.requests.post", fake_post)
    pool = LocalMicrosoftMailboxPool()
    account = MailboxAccount(
        email="user@example.com",
        account_id="user@example.com",
        extra={
            "provider_account": {
                "credentials": {
                    "email": "user@example.com",
                    "client_id": "client-id-123",
                    "refresh_token": "test-refresh-token",
                }
            }
        },
    )
    entry = pool._entry_for_account(account)

    assert pool._graph_access_token(entry) == "access-token-ok"
    assert len(calls) == 2
    assert calls[0][0] == "https://login.microsoftonline.com/common/oauth2/v2.0/token"
    assert calls[1][0] == "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
