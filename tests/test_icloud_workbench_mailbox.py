from __future__ import annotations

import json

from core.icloud_workbench_mailbox import ICloudWorkbenchMailbox


class FakeResponse:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self.payload = payload
        self.text = json.dumps(payload)
        self.reason = "fake error"

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def _next(self, method, url, kwargs):
        self.calls.append((method, url, kwargs))
        if not self.responses:
            raise AssertionError(f"unexpected request: {method} {url}")
        return self.responses.pop(0)

    def post(self, url, **kwargs):
        return self._next("POST", url, kwargs)

    def request(self, method, url, **kwargs):
        return self._next(method.upper(), url, kwargs)

    def get(self, url, **kwargs):
        return self._next("GET", url, kwargs)


def create_mailbox(responses, **overrides):
    return ICloudWorkbenchMailbox(
        base_url="http://127.0.0.1:4173",
        username="admin",
        password="test-password",
        poll_interval=0,
        session=FakeSession(responses),
        **overrides,
    )


def test_connection_reports_inventory_without_consuming_address():
    mailbox = create_mailbox([
        FakeResponse(200, {"username": "admin"}),
        FakeResponse(200, {"addresses": [{
            "id": "address-1", "email": "one@example.com", "label": "freeagent-1",
        }]}),
    ])

    result = mailbox.test_connection()

    assert result["ok"] is True
    assert result["email"] == "one@example.com"
    assert [call[0:2] for call in mailbox.session.calls] == [
        ("POST", "http://127.0.0.1:4173/api/auth/login"),
        ("GET", "http://127.0.0.1:4173/api/addresses"),
    ]


def test_get_email_claims_unused_address_and_persists_public_api_url():
    mailbox = create_mailbox([
        FakeResponse(200, {"username": "admin"}),
        FakeResponse(200, {"addresses": [{
            "id": "address-1", "accountId": "icloud-1", "email": "one@example.com", "label": "freeagent-1",
        }]}),
        FakeResponse(201, {"apiUrl": "http://127.0.0.1:4173/openapi/mail/one%40example.com/token/latest"}),
        FakeResponse(200, {"ok": True}),
    ])

    account = mailbox.get_email()

    assert account.email == "one@example.com"
    assert account.account_id == "address-1"
    assert account.extra["provider_account"]["credentials"]["api_url"].endswith("/token/latest")
    assert account.extra["provider_resource"]["metadata"]["workbench_account_id"] == "icloud-1"
    assert mailbox.session.calls[-1][0] == "PATCH"
    assert mailbox.session.calls[-1][2]["json"] == {"state": "used"}
    assert mailbox.session.calls[-1][2]["headers"]["Origin"] == "http://127.0.0.1:4173"


def test_get_email_does_not_start_generation_when_inventory_is_empty():
    mailbox = create_mailbox([
        FakeResponse(200, {"username": "admin"}),
        FakeResponse(200, {"addresses": []}),
    ])

    try:
        mailbox.get_email()
    except RuntimeError as exc:
        assert "没有匹配标签前缀" in str(exc)
    else:
        raise AssertionError("iCloud Provider 不应在库存为空时启动生产")

    assert not any(call[1].endswith("/generation-jobs") for call in mailbox.session.calls)


def test_account_email_is_resolved_to_workbench_internal_id_before_inventory_filter():
    mailbox = create_mailbox([
        FakeResponse(200, {"username": "admin"}),
        FakeResponse(200, {"accounts": [{
            "id": "icloud-1", "appleId": "owner@example.com", "status": "active",
        }]}),
        FakeResponse(200, {"addresses": [{
            "id": "address-1", "email": "one@example.com", "label": "freeagent-1",
        }]}),
    ], account_id="owner@example.com")

    result = mailbox.test_connection()

    assert result["ok"] is True
    assert mailbox.session.calls[-1][2]["params"]["accountId"] == "icloud-1"


def test_inventory_is_filtered_and_sorted_by_creation_time():
    mailbox = create_mailbox([
        FakeResponse(200, {"username": "admin"}),
        FakeResponse(200, {"addresses": [
            {
                "id": "newest",
                "email": "newest@example.com",
                "label": "test-user-101",
                "createdAt": "2026-08-17T12:00:00.000Z",
            },
            {
                "id": "oldest",
                "email": "oldest@example.com",
                "label": "test-user-102",
                "createdAt": "2026-08-17T10:00:00.000Z",
            },
        ]}),
    ], label_prefix="test-user-1")

    result = mailbox.test_connection()

    assert result["email"] == "oldest@example.com"
    assert mailbox.session.calls[-1][2]["params"]["search"] == "test-user-1"


def test_wait_for_code_ignores_baseline_message_and_returns_new_message_code():
    mailbox = create_mailbox([
        FakeResponse(200, {"email": "one@example.com", "message": {"id": "old", "code": "111111"}}),
        FakeResponse(200, {"email": "one@example.com", "message": {"id": "old", "code": "111111"}}),
        FakeResponse(200, {"email": "one@example.com", "message": {
            "id": "new", "subject": "OpenAI verification code", "code": "654321", "bodyText": "Code 654321",
        }}),
    ])
    account = type("Account", (), {
        "email": "one@example.com",
        "extra": {"provider_account": {"credentials": {
            "api_url": "http://127.0.0.1:4173/openapi/mail/one%40example.com/token/latest",
        }}},
    })()

    before_ids = mailbox.get_current_ids(account)
    code = mailbox.wait_for_code(account, keyword="OpenAI", timeout=1, before_ids=before_ids)

    assert before_ids == {"old"}
    assert code == "654321"


def test_wait_for_code_stops_immediately_when_task_is_cancelled():
    mailbox = create_mailbox([])
    account = type("Account", (), {
        "email": "one@example.com",
        "extra": {"provider_account": {"credentials": {
            "api_url": "http://127.0.0.1:4173/openapi/mail/one%40example.com/token/latest",
        }}},
    })()

    try:
        mailbox.wait_for_code(account, timeout=120, cancel_check=lambda: True)
    except RuntimeError as exc:
        assert str(exc) == "任务已取消"
    else:
        raise AssertionError("验证码等待应在取消后立即结束")

    assert mailbox.session.calls == []


def test_wait_for_code_uses_workbench_history_and_accepts_same_code_from_new_messages():
    mailbox = create_mailbox([
        FakeResponse(200, {
            "messages": [
                {
                    "id": "newer",
                    "receivedAt": "2026-08-22T15:28:51.000Z",
                    "subject": "OpenAI verification code",
                    "code": "492799",
                    "preview": "492799",
                },
                {
                    "id": "older",
                    "receivedAt": "2026-08-22T15:27:46.000Z",
                    "subject": "OpenAI verification code",
                    "code": "492799",
                    "preview": "492799",
                },
            ],
            "pagination": {"total": 2, "totalPages": 1},
        }),
    ])
    mailbox._logged_in = True
    account = type("Account", (), {
        "email": "one@example.com",
        "account_id": "address-1",
        "extra": {},
    })()

    first = mailbox.wait_for_code(account, keyword="OpenAI", timeout=1)
    assert first == "492799"
    assert mailbox.get_last_code_identity(account) == "newer"
    assert mailbox.get_last_code_metadata(account)["received_at"].endswith("15:28:51.000Z")
    assert mailbox.get_last_code_metadata(account)["source"] == "history"

    mailbox.session.responses.append(FakeResponse(200, {
        "messages": [
            {
                "id": "newest",
                "receivedAt": "2026-08-22T15:29:53.000Z",
                "subject": "OpenAI verification code",
                "code": "492799",
                "preview": "492799",
            },
            {
                "id": "newer",
                "receivedAt": "2026-08-22T15:28:51.000Z",
                "subject": "OpenAI verification code",
                "code": "492799",
                "preview": "492799",
            },
        ],
        "pagination": {"total": 2, "totalPages": 1},
    }))
    second = mailbox.wait_for_code(
        account,
        keyword="OpenAI",
        timeout=1,
        before_ids={"newer"},
    )
    assert second == "492799"
    assert mailbox.get_last_code_identity(account) == "newest"


def test_history_endpoint_falls_back_to_public_latest_when_unavailable():
    mailbox = create_mailbox([
        FakeResponse(404, {"error": "not found"}),
        FakeResponse(200, {"message": {
            "id": "latest",
            "subject": "OpenAI verification code",
            "code": "123456",
        }}),
    ])
    mailbox._logged_in = True
    account = type("Account", (), {
        "email": "one@example.com",
        "account_id": "address-1",
        "extra": {"provider_account": {"credentials": {
            "api_url": "http://127.0.0.1:4173/openapi/mail/token/latest",
        }}},
    })()

    assert mailbox.wait_for_code(account, keyword="OpenAI", timeout=1) == "123456"
    assert mailbox._history_supported is False


def test_wait_for_code_syncs_source_inbox_before_reading_history():
    mailbox = create_mailbox([
        FakeResponse(200, {"scanned": 1, "added": 1}),
        FakeResponse(200, {
            "messages": [{
                "id": "synced-message",
                "receivedAt": "2026-08-22T16:11:23.000Z",
                "subject": "OpenAI verification code",
                "code": "807632",
            }],
            "pagination": {"total": 1, "totalPages": 1},
        }),
    ], account_id="icloud-account-1")
    mailbox._logged_in = True
    mailbox._resolved_account_id = "icloud-account-1"
    account = type("Account", (), {
        "email": "one@example.com",
        "account_id": "address-1",
        "extra": {},
    })()

    assert mailbox.wait_for_code(account, keyword="OpenAI", timeout=1) == "807632"
    assert [call[0:2] for call in mailbox.session.calls] == [
        ("POST", "http://127.0.0.1:4173/api/inbox/sync"),
        ("GET", "http://127.0.0.1:4173/api/addresses/address-1/messages"),
    ]
    assert mailbox.session.calls[0][2]["json"] == {"accountId": "icloud-account-1"}


def test_wait_for_link_extracts_link_from_new_message_html():
    mailbox = create_mailbox([
        FakeResponse(200, {"email": "one@example.com", "message": {
            "id": "new", "subject": "Verify account", "bodyHtml": '<a href="https://example.com/verify?id=1">Verify</a>',
        }}),
    ])
    account = type("Account", (), {
        "email": "one@example.com",
        "extra": {"provider_account": {"credentials": {
            "api_url": "http://127.0.0.1:4173/openapi/mail/one%40example.com/token/latest",
        }}},
    })()

    assert mailbox.wait_for_link(account, keyword="Verify", timeout=1) == "https://example.com/verify?id=1"
