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
        password="secret-password",
        poll_interval=0,
        session=FakeSession(responses),
        **overrides,
    )


def test_connection_reports_inventory_without_consuming_address():
    mailbox = create_mailbox([
        FakeResponse(200, {"username": "admin"}),
        FakeResponse(200, {"addresses": [{
            "id": "address-1", "email": "one@icloud.com", "label": "freeagent-1",
        }]}),
    ])

    result = mailbox.test_connection()

    assert result["ok"] is True
    assert result["email"] == "one@icloud.com"
    assert [call[0:2] for call in mailbox.session.calls] == [
        ("POST", "http://127.0.0.1:4173/api/auth/login"),
        ("GET", "http://127.0.0.1:4173/api/addresses"),
    ]


def test_get_email_claims_unused_address_and_persists_public_api_url():
    mailbox = create_mailbox([
        FakeResponse(200, {"username": "admin"}),
        FakeResponse(200, {"addresses": [{
            "id": "address-1", "accountId": "icloud-1", "email": "one@icloud.com", "label": "freeagent-1",
        }]}),
        FakeResponse(201, {"apiUrl": "http://127.0.0.1:4173/openapi/mail/one%40icloud.com/token/latest"}),
        FakeResponse(200, {"ok": True}),
    ])

    account = mailbox.get_email()

    assert account.email == "one@icloud.com"
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
            "id": "address-1", "email": "one@icloud.com", "label": "freeagent-1",
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
                "email": "newest@icloud.com",
                "label": "eronicaawayn-101",
                "createdAt": "2026-08-17T12:00:00.000Z",
            },
            {
                "id": "oldest",
                "email": "oldest@icloud.com",
                "label": "eronicaawayn-102",
                "createdAt": "2026-08-17T10:00:00.000Z",
            },
        ]}),
    ], label_prefix="eronicaawayn-1")

    result = mailbox.test_connection()

    assert result["email"] == "oldest@icloud.com"
    assert mailbox.session.calls[-1][2]["params"]["search"] == "eronicaawayn-1"


def test_wait_for_code_ignores_baseline_message_and_returns_new_message_code():
    mailbox = create_mailbox([
        FakeResponse(200, {"email": "one@icloud.com", "message": {"id": "old", "code": "111111"}}),
        FakeResponse(200, {"email": "one@icloud.com", "message": {"id": "old", "code": "111111"}}),
        FakeResponse(200, {"email": "one@icloud.com", "message": {
            "id": "new", "subject": "OpenAI verification code", "code": "654321", "bodyText": "Code 654321",
        }}),
    ])
    account = type("Account", (), {
        "email": "one@icloud.com",
        "extra": {"provider_account": {"credentials": {
            "api_url": "http://127.0.0.1:4173/openapi/mail/one%40icloud.com/token/latest",
        }}},
    })()

    before_ids = mailbox.get_current_ids(account)
    code = mailbox.wait_for_code(account, keyword="OpenAI", timeout=1, before_ids=before_ids)

    assert before_ids == {"old"}
    assert code == "654321"


def test_wait_for_code_stops_immediately_when_task_is_cancelled():
    mailbox = create_mailbox([])
    account = type("Account", (), {
        "email": "one@icloud.com",
        "extra": {"provider_account": {"credentials": {
            "api_url": "http://127.0.0.1:4173/openapi/mail/one%40icloud.com/token/latest",
        }}},
    })()

    try:
        mailbox.wait_for_code(account, timeout=120, cancel_check=lambda: True)
    except RuntimeError as exc:
        assert str(exc) == "任务已取消"
    else:
        raise AssertionError("验证码等待应在取消后立即结束")

    assert mailbox.session.calls == []


def test_wait_for_link_extracts_link_from_new_message_html():
    mailbox = create_mailbox([
        FakeResponse(200, {"email": "one@icloud.com", "message": {
            "id": "new", "subject": "Verify account", "bodyHtml": '<a href="https://example.com/verify?id=1">Verify</a>',
        }}),
    ])
    account = type("Account", (), {
        "email": "one@icloud.com",
        "extra": {"provider_account": {"credentials": {
            "api_url": "http://127.0.0.1:4173/openapi/mail/one%40icloud.com/token/latest",
        }}},
    })()

    assert mailbox.wait_for_link(account, keyword="Verify", timeout=1) == "https://example.com/verify?id=1"
