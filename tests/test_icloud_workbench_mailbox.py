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
        FakeResponse(200, {"addresses": [{"id": "address-1", "email": "one@icloud.com"}]}),
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


def test_get_email_generates_when_inventory_is_empty():
    mailbox = create_mailbox([
        FakeResponse(200, {"username": "admin"}),
        FakeResponse(200, {"addresses": []}),
        FakeResponse(200, {"accounts": [{"id": "icloud-1", "status": "active"}]}),
        FakeResponse(201, {"jobId": "job-1", "generated": [{"email": "new@icloud.com"}]}),
        FakeResponse(200, {"addresses": [{
            "id": "address-2", "accountId": "icloud-1", "email": "new@icloud.com", "label": "freeagent-1",
        }]}),
        FakeResponse(201, {"apiUrl": "http://127.0.0.1:4173/openapi/mail/new%40icloud.com/token/latest"}),
        FakeResponse(200, {"ok": True}),
    ], batch_size=2)

    account = mailbox.get_email()

    generation_call = next(call for call in mailbox.session.calls if call[1].endswith("/generation-jobs"))
    assert generation_call[2]["json"] == {"count": 2, "labelPrefix": "freeagent"}
    assert account.email == "new@icloud.com"


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
