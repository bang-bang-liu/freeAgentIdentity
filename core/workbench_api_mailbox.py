"""iCloud Workbench API 邮箱适配器，用于登录刷新时获取邮箱验证码。

设计依据：
- 从 provider_accounts.credentials.api_url 读取 Workbench 公开收件 API
- 轮询 /latest 端点获取最新邮件
- 通过 receivedAt 时间戳过滤历史邮件
- 直接使用 Workbench 已提取的 code 字段
"""
from __future__ import annotations

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

    def get_current_ids(self, account: Any = None) -> set:
        """返回当前最新邮件的 ID 集合（基线）。

        这里简化处理：返回空集合，通过时间戳过滤历史邮件。
        """
        return set()

    def wait_for_code(
        self,
        account: Any = None,
        *,
        timeout: int = 180,
        before_ids: set | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str:
        """轮询 Workbench API 获取验证码。

        Args:
            account: 占位参数，保持接口兼容
            timeout: 超时时间（秒）
            before_ids: 占位参数，时间戳过滤更可靠
            cancel_check: 取消检查函数

        Returns:
            验证码字符串

        Raises:
            TimeoutError: 超时未获取到验证码
            RuntimeError: API 请求失败
        """
        start_time = time.time()
        issued_after = start_time  # 只接受此时间之后的邮件
        deadline = start_time + timeout

        while time.time() < deadline:
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
                message = data.get("message")
                if not isinstance(message, dict):
                    # 没有邮件，继续等待
                    time.sleep(self.poll_interval)
                    continue

                # 检查邮件时间戳
                received_at_str = str(message.get("receivedAt") or "").strip()
                if received_at_str:
                    try:
                        # 解析 ISO 8601 时间戳
                        received_at_str_normalized = received_at_str.replace("Z", "+00:00")
                        received_at = datetime.fromisoformat(received_at_str_normalized)
                        if received_at.tzinfo is None:
                            received_at = received_at.replace(tzinfo=timezone.utc)
                        received_timestamp = received_at.timestamp()

                        # 如果是历史邮件，继续等待
                        if received_timestamp < issued_after:
                            time.sleep(self.poll_interval)
                            continue
                    except (TypeError, ValueError):
                        # 时间戳解析失败，继续处理邮件
                        pass

                # 尝试从 Workbench 已提取的 code 字段获取
                code = str(message.get("code") or "").strip()
                if code and re.match(r"^\d{6}$", code):
                    return code

                # 兜底：从邮件正文提取验证码
                body_text = str(message.get("bodyText") or "").strip()
                if body_text:
                    match = re.search(DEFAULT_CODE_PATTERN, body_text)
                    if match:
                        return match.group(1)

            except requests.RequestException as e:
                # 网络错误，继续重试
                time.sleep(self.poll_interval)
                continue
            except Exception:
                # 其他错误，继续重试
                time.sleep(self.poll_interval)
                continue

            time.sleep(self.poll_interval)

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
