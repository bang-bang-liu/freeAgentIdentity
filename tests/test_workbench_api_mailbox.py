from __future__ import annotations

from core.workbench_api_mailbox import WorkbenchApiMailbox


class _Response:
    status_code = 200

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class _Session:
    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        payload = self.payloads.pop(0)
        return _Response(payload)


def test_baseline_ids_filter_old_message_and_timestamp_precision_accepts_new_code():
    session = _Session(
        [
            {
                "message": {
                    "id": "old-message",
                    "receivedAt": "2026-09-15T13:01:20.000Z",
                    "code": "111111",
                }
            },
            {
                "message": {
                    "id": "new-message",
                    # Workbench timestamps are second precision.  The message
                    # was delivered after the request, but rounds below the
                    # caller's fractional issued_after value.
                    "receivedAt": "2026-09-15T13:01:21.000Z",
                    "bodyText": "Your OpenAI verification code is 482615.",
                }
            },
        ]
    )
    mailbox = WorkbenchApiMailbox(
        api_url="https://mail.example/latest",
        email="user@example.com",
        poll_interval=0.5,
        session=session,
    )

    before_ids = mailbox.get_current_ids()
    assert before_ids == {"old-message"}
    assert mailbox.wait_for_code(
        timeout=1,
        before_ids=before_ids,
        issued_after=1789477281.75,
    ) == "482615"


def test_wait_for_code_stops_without_request_when_cancelled():
    session = _Session([])
    mailbox = WorkbenchApiMailbox(
        api_url="https://mail.example/latest",
        email="user@example.com",
        session=session,
    )

    try:
        mailbox.wait_for_code(timeout=120, cancel_check=lambda: True)
    except RuntimeError as exc:
        assert str(exc) == "任务已取消"
    else:
        raise AssertionError("cancelled mailbox polling should stop immediately")
    assert session.calls == []


def test_nested_payload_and_code_field_text_are_supported():
    session = _Session(
        [
            {
                "data": {
                    "message": {
                        "id": "nested",
                        "receivedAt": "2026-09-15T13:01:22Z",
                        "code": "verification code: 793104",
                    }
                }
            }
        ]
    )
    mailbox = WorkbenchApiMailbox(
        api_url="https://mail.example/latest",
        email="user@example.com",
        session=session,
    )

    assert mailbox.wait_for_code(timeout=1, issued_after=1789477280) == "793104"


def test_same_message_is_rechecked_when_workbench_populates_code_later():
    session = _Session(
        [
            {
                "message": {
                    "id": "pending-extraction",
                    "receivedAt": "2026-09-15T13:01:22Z",
                    "subject": "OpenAI verification",
                }
            },
            {
                "message": {
                    "id": "pending-extraction",
                    "receivedAt": "2026-09-15T13:01:22Z",
                    "subject": "OpenAI verification",
                    "code": "246810",
                }
            },
        ]
    )
    mailbox = WorkbenchApiMailbox(
        api_url="https://mail.example/latest",
        email="user@example.com",
        poll_interval=0.5,
        session=session,
    )

    assert mailbox.wait_for_code(timeout=2, issued_after=1789477280) == "246810"


def test_legacy_poll_without_issue_time_keeps_strict_timestamp_filter():
    session = _Session(
        [
            {
                "message": {
                    "id": "just-before",
                    "receivedAt": "2026-09-15T13:01:21Z",
                    "code": "135790",
                }
            }
        ]
    )
    mailbox = WorkbenchApiMailbox(
        api_url="https://mail.example/latest",
        email="user@example.com",
        poll_interval=0.5,
        session=session,
    )

    # Use a fixed timestamp just after the message.  Without the new login
    # flow's explicit issued_after, old callers retain the strict behavior.
    try:
        mailbox.wait_for_code(
            timeout=1,
            issued_after=None,
        )
    except TimeoutError:
        pass
    else:
        raise AssertionError("a historical message must not satisfy a strict poll")
