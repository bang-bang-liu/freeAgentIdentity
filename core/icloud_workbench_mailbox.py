"""iCloud mailbox provider backed by icloud-create-workbench."""

from __future__ import annotations

import re
import threading
import time
from urllib.parse import quote, urljoin, urlsplit

import requests

from core.base_mailbox import BaseMailbox, MailboxAccount, _extract_verification_link


DEFAULT_CODE_PATTERN = r"(?<!#)(?<!\d)(\d{6})(?!\d)"


def _truthy(value: object, *, default: bool = False) -> bool:
    if value in (None, ""):
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on", "y"}


class ICloudWorkbenchMailbox(BaseMailbox):
    """Consume hidden iCloud addresses and mail from a Workbench instance."""

    _claim_lock = threading.Lock()

    def __init__(
        self,
        *,
        base_url: str = "http://127.0.0.1:4173",
        username: str = "admin",
        password: str = "",
        account_id: str = "",
        auto_generate: bool = True,
        batch_size: int | str = 1,
        label_prefix: str = "freeagent",
        poll_interval: float | str = 3,
        request_timeout: float | str = 20,
        session: requests.Session | None = None,
    ):
        self.base_url = str(base_url or "http://127.0.0.1:4173").strip().rstrip("/")
        parsed = urlsplit(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise ValueError("iCloud Workbench 地址无效，仅支持 http/https")
        self.origin = f"{parsed.scheme}://{parsed.netloc}"
        self.username = str(username or "admin").strip()
        self.password = str(password or "")
        self.account_id = str(account_id or "").strip()
        self.auto_generate = bool(auto_generate)
        self.batch_size = min(5, max(1, int(1 if batch_size in (None, "") else batch_size)))
        self.label_prefix = str(label_prefix or "freeagent").strip() or "freeagent"
        self.poll_interval = max(0.0, float(3 if poll_interval in (None, "") else poll_interval))
        self.request_timeout = max(1.0, float(20 if request_timeout in (None, "") else request_timeout))
        self.session = session or requests.Session()
        if session is None:
            # Workbench 通常运行在本机；不要让系统代理劫持本地管理与收件请求。
            self.session.trust_env = False
        self._logged_in = False

    @classmethod
    def from_config(cls, config: dict) -> "ICloudWorkbenchMailbox":
        return cls(
            base_url=config.get("icloud_workbench_base_url", "http://127.0.0.1:4173"),
            username=config.get("icloud_workbench_username", "admin"),
            password=config.get("icloud_workbench_password", ""),
            account_id=config.get("icloud_workbench_account_id", ""),
            auto_generate=_truthy(config.get("icloud_workbench_auto_generate"), default=True),
            batch_size=config.get("icloud_workbench_batch_size", 1),
            label_prefix=config.get("icloud_workbench_label_prefix", "freeagent"),
            poll_interval=config.get("icloud_workbench_poll_interval", 3),
            request_timeout=config.get("icloud_workbench_request_timeout", 20),
        )

    def _url(self, path: str) -> str:
        return urljoin(f"{self.base_url}/", str(path or "").lstrip("/"))

    @staticmethod
    def _response_error(response) -> str:
        try:
            payload = response.json()
            message = str(payload.get("error") or payload.get("message") or "").strip()
        except Exception:
            message = ""
        return message or str(getattr(response, "reason", "") or "请求失败")

    def _login(self) -> None:
        if not self.username or not self.password:
            raise RuntimeError("请配置 iCloud Workbench 管理员用户名和密码")
        response = self.session.post(
            self._url("/api/auth/login"),
            json={"username": self.username, "password": self.password},
            headers={"Accept": "application/json", "Origin": self.origin},
            timeout=self.request_timeout,
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(
                f"iCloud Workbench 登录失败 (HTTP {response.status_code}): {self._response_error(response)}"
            )
        self._logged_in = True

    def _admin_request(self, method: str, path: str, **kwargs):
        if not self._logged_in:
            self._login()
        headers = {"Accept": "application/json", **dict(kwargs.pop("headers", {}) or {})}
        if method.upper() in {"POST", "PUT", "PATCH", "DELETE"}:
            headers["Origin"] = self.origin
        response = self.session.request(
            method,
            self._url(path),
            headers=headers,
            timeout=self.request_timeout,
            **kwargs,
        )
        if response.status_code == 401:
            self._logged_in = False
            self._login()
            response = self.session.request(
                method,
                self._url(path),
                headers=headers,
                timeout=self.request_timeout,
                **kwargs,
            )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(
                f"iCloud Workbench 请求失败 (HTTP {response.status_code}): {self._response_error(response)}"
            )
        try:
            return response.json()
        except Exception as exc:
            raise RuntimeError("iCloud Workbench 返回了无效 JSON") from exc

    def _list_unused(self) -> list[dict]:
        params = {"page": 1, "pageSize": 10, "state": "unused"}
        if self.account_id:
            params["accountId"] = self.account_id
        payload = self._admin_request("GET", "/api/addresses", params=params)
        return [item for item in list(payload.get("addresses") or []) if isinstance(item, dict)]

    def _select_generation_account(self) -> str:
        if self.account_id:
            return self.account_id
        payload = self._admin_request("GET", "/api/icloud-accounts")
        accounts = [item for item in list(payload.get("accounts") or []) if isinstance(item, dict)]
        active = next((item for item in accounts if item.get("status") == "active"), None)
        if not active or not active.get("id"):
            raise RuntimeError("iCloud Workbench 中没有可用于生产邮箱的有效 iCloud 账号")
        return str(active["id"])

    def test_connection(self) -> dict:
        addresses = self._list_unused()
        if addresses:
            return {
                "ok": True,
                "message": f"连接成功！当前可用 iCloud 邮箱: {addresses[0].get('email', '')}",
                "email": str(addresses[0].get("email") or ""),
            }
        if self.auto_generate:
            account_id = self._select_generation_account()
            return {
                "ok": True,
                "message": f"连接成功！当前库存为空，注册时将通过账号 {account_id} 自动生产邮箱",
            }
        return {"ok": True, "message": "连接成功，但当前没有 unused iCloud 邮箱且自动生产已关闭"}

    def peek_email(self) -> str:
        addresses = self._list_unused()
        if not addresses:
            raise RuntimeError("iCloud Workbench 当前没有 unused 邮箱")
        return str(addresses[0].get("email") or "")

    def _claim_address(self) -> tuple[dict, str]:
        addresses = self._list_unused()
        if not addresses:
            if not self.auto_generate:
                raise RuntimeError("iCloud Workbench 邮箱库存已用尽，且自动生产已关闭")
            account_id = self._select_generation_account()
            self._admin_request(
                "POST",
                f"/api/icloud-accounts/{quote(account_id, safe='')}/generation-jobs",
                json={"count": self.batch_size, "labelPrefix": self.label_prefix},
            )
            addresses = self._list_unused()
        if not addresses:
            raise RuntimeError("iCloud Workbench 生产任务结束后仍未找到 unused 邮箱")

        address = addresses[0]
        address_id = str(address.get("id") or "").strip()
        email = str(address.get("email") or "").strip()
        if not address_id or "@" not in email:
            raise RuntimeError("iCloud Workbench 返回的邮箱记录缺少 id 或 email")
        access = self._admin_request(
            "POST", f"/api/addresses/{quote(address_id, safe='')}/public-access"
        )
        api_url = str(access.get("apiUrl") or "").strip()
        parsed = urlsplit(api_url)
        if parsed.scheme not in {"http", "https"} or not parsed.netloc:
            raise RuntimeError("iCloud Workbench 未返回有效的公开收件 API 地址")
        self._admin_request(
            "PATCH", f"/api/addresses/{quote(address_id, safe='')}/state", json={"state": "used"}
        )
        return address, api_url

    def get_email(self) -> MailboxAccount:
        with self._claim_lock:
            address, api_url = self._claim_address()
        email = str(address["email"])
        address_id = str(address["id"])
        account_id = str(address.get("accountId") or self.account_id or "")
        return MailboxAccount(
            email=email,
            account_id=address_id,
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "icloud_workbench",
                    "login_identifier": email,
                    "display_name": email,
                    "credentials": {"email": email, "api_url": api_url},
                    "metadata": {
                        "source": "icloud_workbench",
                        "workbench_account_id": account_id,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "icloud_workbench",
                    "resource_type": "mailbox",
                    "resource_identifier": address_id,
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "source": "icloud_workbench",
                        "workbench_account_id": account_id,
                        "label": str(address.get("label") or ""),
                    },
                },
            },
        )

    @staticmethod
    def _api_url_for_account(account: MailboxAccount) -> str:
        extra = dict(account.extra or {})
        provider_account = dict(extra.get("provider_account") or {})
        credentials = dict(provider_account.get("credentials") or {})
        api_url = str(credentials.get("api_url") or "").strip()
        if not api_url:
            raise RuntimeError(f"iCloud 邮箱缺少公开收件 API 地址: {account.email}")
        return api_url

    def _latest_message(self, account: MailboxAccount) -> dict | None:
        response = self.session.get(
            self._api_url_for_account(account),
            headers={"Accept": "application/json", "User-Agent": "freeAgentIdentity/icloud-workbench"},
            timeout=self.request_timeout,
        )
        if not 200 <= response.status_code < 300:
            raise RuntimeError(f"iCloud 公开收件请求失败 (HTTP {response.status_code})")
        try:
            payload = response.json()
        except Exception as exc:
            raise RuntimeError("iCloud 公开收件接口返回了无效 JSON") from exc
        message = payload.get("message") if isinstance(payload, dict) else None
        return message if isinstance(message, dict) else None

    @staticmethod
    def _message_id(message: dict | None) -> str:
        if not message:
            return ""
        return str(message.get("id") or message.get("uid") or "").strip()

    @staticmethod
    def _message_text(message: dict | None) -> str:
        if not message:
            return ""
        return "\n".join(
            str(message.get(key) or "")
            for key in ("subject", "sender", "preview", "bodyText", "bodyHtml")
        )

    def get_current_ids(self, account: MailboxAccount) -> set:
        try:
            message_id = self._message_id(self._latest_message(account))
            return {message_id} if message_id else set()
        except Exception:
            return set()

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
    ) -> str:
        seen = set(before_ids or set())
        pattern = re.compile(code_pattern or DEFAULT_CODE_PATTERN)
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                message = self._latest_message(account)
                message_id = self._message_id(message)
                text = self._message_text(message)
                if message_id and message_id not in seen and (not keyword or keyword.lower() in text.lower()):
                    code_value = str((message or {}).get("code") or "")
                    match = pattern.search(code_value) or pattern.search(text)
                    if match:
                        return match.group(1) if match.groups() else match.group(0)
                if message_id:
                    seen.add(message_id)
            except Exception as exc:
                last_error = str(exc).strip() or exc.__class__.__name__
            time.sleep(self.poll_interval)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 iCloud 邮箱验证码超时 ({timeout}s){suffix}")

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
    ) -> str:
        seen = set(before_ids or set())
        deadline = time.monotonic() + timeout
        last_error = ""
        while time.monotonic() < deadline:
            try:
                message = self._latest_message(account)
                message_id = self._message_id(message)
                if message_id and message_id not in seen:
                    link = _extract_verification_link(self._message_text(message), keyword)
                    if link:
                        return link
                if message_id:
                    seen.add(message_id)
            except Exception as exc:
                last_error = str(exc).strip() or exc.__class__.__name__
            time.sleep(self.poll_interval)
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待 iCloud 邮箱验证链接超时 ({timeout}s){suffix}")
