from application.manual_otp import (
    InvalidManualOtp,
    ManualOtpRegistry,
    ManualOtpRequestAmbiguous,
    ManualOtpRequestNotFound,
    manual_otp_registry,
)
from application.tasks import create_register_task


def test_registry_keeps_concurrent_workers_isolated_and_hides_codes():
    registry = ManualOtpRegistry()
    first = registry.open_request(
        task_id="task-1",
        worker_id="worker_1",
        worker_label="Worker 1",
        email="first@example.com",
    )
    second = registry.open_request(
        task_id="task-1",
        worker_id="worker_2",
        worker_label="Worker 2",
        email="second@example.com",
    )

    pending = registry.list_pending("task-1")
    assert {item["request_id"] for item in pending} == {first, second}
    assert all("code" not in item for item in pending)

    registry.submit_code(task_id="task-1", request_id=second, code="654321")

    assert registry.consume_code(first) is None
    assert registry.consume_code(second) == "654321"
    assert {item["request_id"] for item in registry.list_pending("task-1")} == {first}


def test_registry_requires_request_id_when_multiple_workers_are_waiting():
    registry = ManualOtpRegistry()
    for worker in ("worker_1", "worker_2"):
        registry.open_request(
            task_id="task-many",
            worker_id=worker,
            worker_label=worker,
            email=f"{worker}@example.com",
        )

    try:
        registry.submit_code(task_id="task-many", code="123456")
    except ManualOtpRequestAmbiguous:
        pass
    else:
        raise AssertionError("multiple pending requests must require request_id")


def test_registry_validates_codes_and_rejects_unknown_requests():
    registry = ManualOtpRegistry()

    try:
        registry.submit_code(task_id="task-1", code="12ab")
    except InvalidManualOtp:
        pass
    else:
        raise AssertionError("non-numeric OTP must be rejected")

    try:
        registry.submit_code(task_id="task-1", request_id="missing", code="123456")
    except ManualOtpRequestNotFound:
        pass
    else:
        raise AssertionError("unknown OTP request must be rejected")


def test_manual_otp_api_lists_and_accepts_pending_request_without_echoing_code(client):
    task = create_register_task({"count": 1})
    task_id = str(task["id"])
    request_id = manual_otp_registry.open_request(
        task_id=task_id,
        worker_id="worker_1",
        worker_label="Worker 1",
        email="generated@example.com",
    )
    try:
        listed = client.get(f"/api/tasks/{task_id}/otp-requests")
        assert listed.status_code == 200
        assert listed.json() == {
            "items": [{
                "request_id": request_id,
                "worker_id": "worker_1",
                "worker_label": "Worker 1",
                "email": "generated@example.com",
                "created_at": listed.json()["items"][0]["created_at"],
            }]
        }

        submitted = client.post(
            f"/api/tasks/{task_id}/otp",
            json={"request_id": request_id, "code": "482615"},
        )
        assert submitted.status_code == 200
        assert submitted.json() == {"accepted": True, "request_id": request_id}
        assert "482615" not in submitted.text
        assert client.get(f"/api/tasks/{task_id}/otp-requests").json() == {"items": []}
        assert manual_otp_registry.consume_code(request_id) == "482615"
    finally:
        manual_otp_registry.clear_task(task_id)
