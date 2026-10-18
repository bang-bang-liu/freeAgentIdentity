"""iCloud Workbench API 邮箱适配器，用于登录刷新时获取邮箱验证码。

设计依据：
- 从 provider_accounts.credentials.api_url 读取 Workbench 公开收件 API
- 轮询 /latest 端点获取最新邮件
- 通过 receivedAt 时间戳过滤历史邮件
- 直接使用 Workbench 已提取的 code 字段
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import datetime, timezone
from typing import Any, Callable

import requests


DEFAULT_CODE_PATTERN = r"(?<!#)(?<!\d)(\d{6})(?!\d)"


class WorkbenchApiMailbox:
    """Workbench API 邮箱适配器，兼容 EmailOtpResolver 的接口。"""

    def __init__(
        self,
        *,
        api_url: str,
        email: str,
        poll_interval: float = 3.0,
        request_timeout: float = 20.0,
        session: requests.Session | None = None,
    ):
        self.api_url = str(api_url or "").strip()
        self.email = str(email or "").strip()
        self.poll_interval = max(float(poll_interval), 0.5)
        self.request_timeout = max(float(request_timeout), 1.0)
        self.session = session or requests.Session()
        if session is None:
            # 可能是远程 Workbench，需要遵守系统代理
            self.session.trust_env = True

    @staticmethod
    def _message_id(message: dict[str, Any] | None) -> str:
        """Return a stable identity for a Workbench message."""
        if not isinstance(message, dict):
            return ""
        for key in (
            "id",
            "uid",
            "uuid",
            "messageId",
            "message_id",
            "internetMessageId",
        ):
            value = str(message.get(key) or "").strip()
            if value:
                return value
        try:
            material = json.dumps(
                message,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        except Exception:
            material = repr(message)
        return "body:" + hashlib.sha256(material.encode("utf-8")).hexdigest()

    @staticmethod
    def _messages_from_payload(payload: Any) -> list[dict[str, Any]]:
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        if not isinstance(payload, dict):
            return []
        messages = payload.get("messages")
        if isinstance(messages, list) and messages:
            return [item for item in messages if isinstance(item, dict)]
        message = payload.get("message")
        if isinstance(message, dict):
            return [message]
        # A few lightweight mailbox proxies return the message fields at the
        # top level instead of wrapping them in ``message``.
        if any(
            payload.get(key) not in (None, "")
            for key in ("code", "bodyText", "bodyHtml", "subject")
        ):
            return [payload]
        for key in ("data", "result", "payload", "latest"):
            nested = payload.get(key)
            nested_messages = WorkbenchApiMailbox._messages_from_payload(nested)
            if nested_messages:
                return nested_messages
        return []

    @staticmethod
    def _message_timestamp(message: dict[str, Any] | None) -> float | None:
        if not isinstance(message, dict):
            return None
        for key in (
            "receivedAt",
            "received_at",
            "receivedDateTime",
            "date",
            "createdAt",
            "created_at",
            "timestamp",
            "sentAt",
        ):
            raw = message.get(key)
            if raw in (None, ""):
                continue
            if isinstance(raw, (int, float)) and not isinstance(raw, bool):
                try:
                    numeric = float(raw)
                    # Accept common Unix second, millisecond and microsecond
                    # encodings without treating a 13-digit value as a date
                    # thousands of years in the future.
                    if abs(numeric) >= 100_000_000_000_000:
                        numeric /= 1_000_000.0
                    elif abs(numeric) >= 100_000_000_000:
                        numeric /= 1_000.0
                    return numeric
                except (TypeError, ValueError, OverflowError):
                    continue
            value = str(raw).strip()
            if not value:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError, OverflowError):
                numeric = None
            if numeric is not None:
                if abs(numeric) >= 100_000_000_000_000:
                    numeric /= 1_000_000.0
                elif abs(numeric) >= 100_000_000_000:
                    numeric /= 1_000.0
                return numeric
            try:
                parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=timezone.utc)
                return parsed.timestamp()
            except (TypeError, ValueError, OverflowError):
                continue
        return None

    @staticmethod
    def _message_code(message: dict[str, Any] | None) -> str:
        if not isinstance(message, dict):
            return ""
        direct = str(message.get("code") or "").strip()
        if re.fullmatch(r"\d{6}", direct):
            return direct
        chunks = [
            message.get(key)
            for key in (
                "code",
                "bodyText",
                "bodyHtml",
                "bodyPreview",
                "preview",
                "text",
                "body",
                "content",
                "subject",
            )
        ]
        text = "\n".join(str(chunk or "") for chunk in chunks)
        match = re.search(DEFAULT_CODE_PATTERN, text)
        return match.group(1) if match else ""

    def close(self) -> None:
        close = getattr(self.session, "close", None)
        if callable(close):
            close()

    @staticmethod
    def _sleep_with_cancel(
        seconds: float,
        cancel_check: Callable[[], bool] | None,
    ) -> None:
        deadline = time.monotonic() + max(float(seconds or 0), 0.0)
        while True:
            if cancel_check and cancel_check():
                raise RuntimeError("任务已取消")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(0.1, remaining))

    def get_current_ids(self, account: Any = None) -> set:
        """返回当前最新邮件的 ID 集合（基线）。

        Workbench 的 latest 接口通常只返回一封邮件，但它仍然带有稳定
        ``id``。读取这个 ID 作为基线可以避免在发码前已经存在的邮件被误
        当成新验证码。
        """
        del account
        try:
            response = self.session.get(self.api_url, timeout=self.request_timeout)
            if response.status_code != 200:
                return set()
            payload = response.json()
            return {
                message_id
                for message in self._messages_from_payload(payload)
                if (message_id := self._message_id(message))
            }
        except Exception:
            # A baseline is an optimization/safety filter.  If the public
            # endpoint is temporarily unavailable, the resolver can still
            # fall back to issued_after/manual handling.
            return set()

    def wait_for_code(
        self,
        account: Any = None,
        *,
        timeout: int = 180,
        before_ids: set | None = None,
        cancel_check: Callable[[], bool] | None = None,
        issued_after: float | None = None,
    ) -> str:
        """轮询 Workbench API 获取验证码。

        Args:
            account: 占位参数，保持接口兼容
            timeout: 超时时间（秒）
            before_ids: 发码前已有邮件 ID，用于过滤旧邮件
            cancel_check: 取消检查函数
            issued_after: 发码流程开始时间（Unix 秒）。Workbench 邮件时间
                通常只有秒级精度，因此会保留一个很小的时钟/精度容差。

        Returns:
            验证码字符串

        Raises:
            TimeoutError: 超时未获取到验证码
            RuntimeError: API 请求失败
        """
        start_time = time.time()
        try:
            issued_timestamp = float(issued_after) if issued_after is not None else start_time
        except (TypeError, ValueError):
            issued_timestamp = start_time
        # ``receivedAt`` is commonly truncated to whole seconds.  The login
        # flow supplies an explicit pre-send ``issued_after`` and a baseline
        # ID set, so a small tolerance fixes that boundary race without
        # weakening the legacy standalone call (which remains strict).
        timestamp_tolerance = 2.0 if issued_after is not None else 0.0
        minimum_timestamp = issued_timestamp - timestamp_tolerance
        seen = {str(value) for value in (before_ids or set()) if str(value).strip()}
        deadline = time.monotonic() + max(float(timeout or 0), 0.0)

        while time.monotonic() < deadline:
            if cancel_check and cancel_check():
                raise RuntimeError("任务已取消")

            try:
                response = self.session.get(
                    self.api_url,
                    timeout=self.request_timeout,
                )
                if response.status_code != 200:
                    raise RuntimeError(
                        f"Workbench API 请求失败: HTTP {response.status_code}"
                    )

                data = response.json()
                messages = self._messages_from_payload(data)
                if not messages:
                    # 没有邮件，继续等待
                    self._sleep_with_cancel(self.poll_interval, cancel_check)
                    continue

                for message in messages:
                    message_id = self._message_id(message)
                    if message_id and message_id in seen:
                        continue
                    received_timestamp = self._message_timestamp(message)
                    if (
                        received_timestamp is not None
                        and received_timestamp < minimum_timestamp
                    ):
                        if message_id:
                            seen.add(message_id)
                        continue
                    code = self._message_code(message)
                    if code:
                        return code
                    # Workbench may expose a message row before its body/code
                    # extraction finishes.  Keep revisiting a new code-less
                    # message so the same ID can become usable on a later poll.

            except requests.RequestException as e:
                # 网络错误，继续重试
                self._sleep_with_cancel(self.poll_interval, cancel_check)
                continue
            except RuntimeError as exc:
                if cancel_check and cancel_check():
                    raise
                # 其他 API 错误继续重试
                self._sleep_with_cancel(self.poll_interval, cancel_check)
                continue
            except Exception:
                # 其他错误，继续重试
                self._sleep_with_cancel(self.poll_interval, cancel_check)
                continue

            self._sleep_with_cancel(self.poll_interval, cancel_check)

        raise TimeoutError(
            f"Workbench API 超时 {timeout} 秒未获取到验证码"
        )


class WorkbenchApiRebuiltMailbox:
    """包装 WorkbenchApiMailbox，兼容 EmailOtpResolver 的 rebuilt 接口。"""

    def __init__(self, api_url: str, email: str):
        self.ok = True
        self.email = email
        self.account = None  # 占位，EmailOtpResolver 会传入
        self.mailbox = WorkbenchApiMailbox(api_url=api_url, email=email)

    def close(self) -> None:
        close = getattr(self.mailbox, "close", None)
        if callable(close):
            close()
