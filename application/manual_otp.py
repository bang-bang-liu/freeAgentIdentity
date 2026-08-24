"""Volatile, task-scoped manual OTP handoff for registration workers."""
from __future__ import annotations

import re
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timezone


class ManualOtpRequestNotFound(LookupError):
    pass


class ManualOtpRequestAmbiguous(RuntimeError):
    pass


class InvalidManualOtp(ValueError):
    pass


@dataclass
class _PendingOtpRequest:
    request_id: str
    task_id: str
    worker_id: str
    worker_label: str
    email: str
    created_at: str
    code: str = ""

    def public_dict(self) -> dict[str, str]:
        return {
            "request_id": self.request_id,
            "worker_id": self.worker_id,
            "worker_label": self.worker_label,
            "email": self.email,
            "created_at": self.created_at,
        }


class ManualOtpRegistry:
    """Thread-safe OTP exchange that never persists or exposes submitted codes."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._requests: dict[str, _PendingOtpRequest] = {}

    @staticmethod
    def _now_iso() -> str:
        return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    @staticmethod
    def _normalize_code(code: object) -> str:
        value = str(code or "").strip()
        if not re.fullmatch(r"\d{4,10}", value):
            raise InvalidManualOtp("验证码必须是 4 到 10 位数字")
        return value

    def open_request(
        self,
        *,
        task_id: str,
        worker_id: str,
        worker_label: str,
        email: str,
    ) -> str:
        request = _PendingOtpRequest(
            request_id=secrets.token_urlsafe(16),
            task_id=str(task_id or ""),
            worker_id=str(worker_id or "main"),
            worker_label=str(worker_label or worker_id or "Main"),
            email=str(email or ""),
            created_at=self._now_iso(),
        )
        with self._lock:
            stale_ids = [
                request_id
                for request_id, current in self._requests.items()
                if current.task_id == request.task_id
                and current.worker_id == request.worker_id
            ]
            for request_id in stale_ids:
                self._requests.pop(request_id, None)
            self._requests[request.request_id] = request
        return request.request_id

    def list_pending(self, task_id: str) -> list[dict[str, str]]:
        with self._lock:
            requests = [
                request
                for request in self._requests.values()
                if request.task_id == task_id and not request.code
            ]
            requests.sort(key=lambda item: (item.created_at, item.worker_id, item.request_id))
            return [request.public_dict() for request in requests]

    def submit_code(
        self,
        *,
        task_id: str,
        code: object,
        request_id: str = "",
    ) -> str:
        normalized_code = self._normalize_code(code)
        with self._lock:
            if request_id:
                request = self._requests.get(str(request_id))
                if not request or request.task_id != task_id or request.code:
                    raise ManualOtpRequestNotFound("验证码请求不存在或已被处理")
            else:
                candidates = [
                    item
                    for item in self._requests.values()
                    if item.task_id == task_id and not item.code
                ]
                if not candidates:
                    raise ManualOtpRequestNotFound("当前任务没有等待中的验证码请求")
                if len(candidates) != 1:
                    raise ManualOtpRequestAmbiguous("当前有多个账号等待验证码，请指定 request_id")
                request = candidates[0]
            request.code = normalized_code
            return request.request_id

    def consume_code(self, request_id: str) -> str | None:
        with self._lock:
            request = self._requests.get(str(request_id or ""))
            if not request or not request.code:
                return None
            code = request.code
            self._requests.pop(request.request_id, None)
            return code

    def close_request(self, request_id: str) -> None:
        with self._lock:
            self._requests.pop(str(request_id or ""), None)

    def clear_task(self, task_id: str) -> None:
        with self._lock:
            request_ids = [
                request_id
                for request_id, request in self._requests.items()
                if request.task_id == task_id
            ]
            for request_id in request_ids:
                self._requests.pop(request_id, None)


manual_otp_registry = ManualOtpRegistry()


class ManualOtpHandle:
    """Worker-bound view used by registration code without importing application APIs."""

    def __init__(
        self,
        *,
        task_id: str,
        worker_id: str,
        worker_label: str,
        registry: ManualOtpRegistry | None = None,
    ) -> None:
        self.task_id = str(task_id or "")
        self.worker_id = str(worker_id or "main")
        self.worker_label = str(worker_label or worker_id or "Main")
        self.registry = registry or manual_otp_registry

    def open_request(self, email: str) -> str:
        return self.registry.open_request(
            task_id=self.task_id,
            worker_id=self.worker_id,
            worker_label=self.worker_label,
            email=email,
        )

    def consume_code(self, request_id: str) -> str | None:
        return self.registry.consume_code(request_id)

    def close_request(self, request_id: str) -> None:
        self.registry.close_request(request_id)
