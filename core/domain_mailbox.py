"""Catch-all domain mailbox provider backed by IMAP."""
from __future__ import annotations

import email as email_lib
import html
import imaplib
import re
import secrets
import ssl
import string
import threading
import time
from email.header import decode_header
from email.message import Message
from email.policy import default as default_email_policy
from email.utils import getaddresses
from typing import Callable

from core.base_mailbox import (
    BaseMailbox,
    MailboxAccount,
    _extract_verification_link,
    _read_manual_code,
    _raise_if_cancelled,
    _sleep_with_cancel,
)


DEFAULT_CODE_PATTERN = r"(?<!#)(?<!\d)(\d{6})(?!\d)"
RECIPIENT_HEADERS = (
    "Delivered-To",
    "X-Original-To",
    "Envelope-To",
    "Original-Recipient",
    "Apparently-To",
    "To",
    "Cc",
)


class DomainMailboxConfigurationError(RuntimeError):
    """Raised for settings that cannot succeed without user intervention."""


def _as_float(value: object, default: float, minimum: float, maximum: float) -> float:
    try:
        result = float(default if value in (None, "") else value)
    except (TypeError, ValueError) as exc:
        raise DomainMailboxConfigurationError(f"数值配置无效: {value!r}") from exc
    return max(minimum, min(maximum, result))


def _as_int(value: object, default: int, minimum: int, maximum: int) -> int:
    try:
        result = int(default if value in (None, "") else value)
    except (TypeError, ValueError) as exc:
        raise DomainMailboxConfigurationError(f"整数配置无效: {value!r}") from exc
    return max(minimum, min(maximum, result))


def normalize_domain(value: object) -> str:
    raw = str(value or "").strip().lower().lstrip("@").rstrip(".")
    if not raw or "://" in raw or any(char.isspace() for char in raw):
        raise DomainMailboxConfigurationError("域名无效，请只填写 example.com，不要包含协议或路径")
    try:
        domain = raw.encode("idna").decode("ascii")
    except UnicodeError as exc:
        raise DomainMailboxConfigurationError("域名无法转换为有效的 IDN 域名") from exc
    labels = domain.split(".")
    if len(labels) < 2 or len(domain) > 253:
        raise DomainMailboxConfigurationError("域名无效，请填写完整域名，例如 example.com")
    if any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise DomainMailboxConfigurationError("域名格式无效，请填写完整域名，例如 example.com")
    return domain


def _decode_mime_header(value: object) -> str:
    parts: list[str] = []
    for raw, charset in decode_header(str(value or "")):
        if isinstance(raw, bytes):
            try:
                parts.append(raw.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                parts.append(raw.decode("utf-8", errors="replace"))
        else:
            parts.append(str(raw or ""))
    return "".join(parts)


class DomainImapMailbox(BaseMailbox):
    """Generate catch-all addresses, or use one fixed address, over IMAP."""

    def __init__(
        self,
        *,
        domain: str = "",
        fixed_email: str = "",
        imap_host: str = "",
        imap_port: int | str = 993,
        imap_security: str = "ssl",
        imap_username: str = "",
        imap_password: str = "",
        imap_folder: str = "INBOX",
        prefix: str = "fa",
        prefix_length: int | str = 12,
        poll_interval: float | str = 3,
        request_timeout: float | str = 20,
    ):
        raw_fixed_email = str(fixed_email or "").strip().lower()
        fixed_domain = ""
        if raw_fixed_email:
            fixed_local, separator, fixed_domain = raw_fixed_email.rpartition("@")
            if not separator or not fixed_local or not fixed_domain:
                raise DomainMailboxConfigurationError(
                    "固定邮箱地址无效，请填写完整地址，例如 user@example.com"
                )
            if not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", fixed_local):
                raise DomainMailboxConfigurationError("固定邮箱地址的用户名部分格式无效")
        self.domain = normalize_domain(domain or fixed_domain)
        self.fixed_email = raw_fixed_email
        if self.fixed_email and normalize_domain(fixed_domain) != self.domain:
            raise DomainMailboxConfigurationError("固定邮箱地址的域名必须与邮箱域名一致")
        self.imap_host = str(imap_host or "").strip()
        self.imap_security = str(imap_security or "ssl").strip().lower()
        if self.imap_security == "tls":
            self.imap_security = "starttls"
        if self.imap_security not in {"ssl", "starttls", "plain"}:
            raise DomainMailboxConfigurationError("IMAP 加密方式仅支持 SSL/TLS、STARTTLS 或明文")
        default_port = 993 if self.imap_security == "ssl" else 143
        self.imap_port = _as_int(imap_port, default_port, 1, 65535)
        self.imap_username = str(imap_username or "").strip()
        self.imap_password = str(imap_password or "")
        self.imap_folder = str(imap_folder or "INBOX").strip() or "INBOX"
        self.prefix = str(prefix or "").strip().lower().strip("-")
        if self.prefix and not re.fullmatch(r"[a-z0-9-]+", self.prefix):
            raise DomainMailboxConfigurationError("邮箱前缀只能包含小写字母、数字和连字符")
        if len(self.prefix) > 24:
            raise DomainMailboxConfigurationError("邮箱固定前缀最长为 24 个字符")
        self.prefix_length = _as_int(prefix_length, 12, 8, 32)
        self.poll_interval = _as_float(poll_interval, 3, 0.25, 60.0)
        self.request_timeout = _as_float(request_timeout, 20, 1.0, 120.0)
        self._allocation_lock = threading.Lock()
        self._allocated: set[str] = set()
        self._last_code_identity: dict[str, str] = {}

        if not self.imap_host:
            raise DomainMailboxConfigurationError("请填写 IMAP 服务器地址")
        if not self.imap_username:
            raise DomainMailboxConfigurationError("请填写 IMAP 登录用户名")
        if not self.imap_password:
            raise DomainMailboxConfigurationError("请填写 IMAP 登录密码或应用专用密码")

    @classmethod
    def from_config(cls, config: dict) -> "DomainImapMailbox":
        return cls(
            domain=config.get("domain_mailbox_domain") or config.get("domain") or "",
            fixed_email=config.get("domain_mailbox_fixed_email") or config.get("fixed_email") or "",
            imap_host=config.get("domain_mailbox_imap_host") or config.get("imap_host") or "",
            imap_port=config.get("domain_mailbox_imap_port") or config.get("imap_port") or 993,
            imap_security=config.get("domain_mailbox_imap_security") or config.get("imap_security") or "ssl",
            imap_username=config.get("domain_mailbox_imap_username") or config.get("imap_username") or "",
            imap_password=config.get("domain_mailbox_imap_password") or config.get("imap_password") or "",
            imap_folder=config.get("domain_mailbox_imap_folder") or config.get("imap_folder") or "INBOX",
            prefix=config.get("domain_mailbox_prefix") or config.get("prefix") or "fa",
            prefix_length=config.get("domain_mailbox_prefix_length") or config.get("prefix_length") or 12,
            poll_interval=config.get("domain_mailbox_poll_interval") or config.get("poll_interval") or 3,
            request_timeout=config.get("domain_mailbox_request_timeout") or config.get("request_timeout") or 20,
        )

    def _random_email(self) -> str:
        alphabet = string.ascii_lowercase + string.digits
        for _ in range(20):
            random_part = "".join(secrets.choice(alphabet) for _ in range(self.prefix_length))
            local_part = f"{self.prefix}-{random_part}" if self.prefix else random_part
            email = f"{local_part}@{self.domain}"
            with self._allocation_lock:
                if email not in self._allocated:
                    self._allocated.add(email)
                    return email
        raise RuntimeError("随机邮箱地址生成冲突次数过多，请重试")

    def get_email(self) -> MailboxAccount:
        if self.fixed_email:
            email = self.fixed_email
            with self._allocation_lock:
                if email in self._allocated:
                    raise RuntimeError(f"固定邮箱 {email} 已被当前任务使用，不能并发重复注册")
                self._allocated.add(email)
        else:
            email = self._random_email()
        source = "fixed_imap" if self.fixed_email else "catch_all_imap"
        return MailboxAccount(
            email=email,
            account_id=email.lower(),
            extra={
                "provider_account": {
                    "provider_type": "mailbox",
                    "provider_name": "domain_mailbox",
                    "login_identifier": self.imap_username,
                    "display_name": self.fixed_email or f"{self.domain} catch-all",
                    # The IMAP password stays only in provider settings and is
                    # deliberately not copied into persisted account metadata.
                    "credentials": {"email": email},
                    "metadata": {
                        "source": source,
                        "domain": self.domain,
                        "fixed_email": self.fixed_email,
                        "imap_host": self.imap_host,
                        "imap_folder": self.imap_folder,
                    },
                },
                "provider_resource": {
                    "provider_type": "mailbox",
                    "provider_name": "domain_mailbox",
                    "resource_type": "mailbox",
                    "resource_identifier": email.lower(),
                    "handle": email,
                    "display_name": email,
                    "metadata": {
                        "email": email,
                        "domain": self.domain,
                        "source": source,
                        "generated": True,
                        "fixed_email": self.fixed_email,
                    },
                },
            },
        )

    def release_email(self, account: MailboxAccount | None) -> None:
        # Addresses generated in one task stay unique for that pool instance;
        # this hook exists for the common mailbox lifecycle contract.
        del account

    @staticmethod
    def _status_is_ok(status: object) -> bool:
        if isinstance(status, bytes):
            status = status.decode("ascii", errors="ignore")
        return str(status or "").upper() == "OK"

    def _connect(self):
        connection = None
        try:
            if self.imap_security == "ssl":
                connection = imaplib.IMAP4_SSL(
                    self.imap_host,
                    self.imap_port,
                    ssl_context=ssl.create_default_context(),
                    timeout=self.request_timeout,
                )
            else:
                connection = imaplib.IMAP4(
                    self.imap_host,
                    self.imap_port,
                    timeout=self.request_timeout,
                )
                if self.imap_security == "starttls":
                    connection.starttls(ssl_context=ssl.create_default_context())
            connection.login(self.imap_username, self.imap_password)
            selected_folder = self._resolve_folder(connection)
            status, detail = self._select_folder(connection, selected_folder)
            if not self._status_is_ok(status):
                raise DomainMailboxConfigurationError(
                    f"IMAP 无法打开邮箱目录 {selected_folder!r}: {self._response_detail(detail)}"
                )
            try:
                connection._domain_mailbox_selected_folder = selected_folder
            except Exception:
                pass
            return connection
        except imaplib.IMAP4.abort:
            self._disconnect(connection)
            raise
        except imaplib.IMAP4.error as exc:
            self._disconnect(connection)
            raise DomainMailboxConfigurationError("IMAP 登录失败，请检查用户名、密码和加密方式") from exc
        except Exception:
            self._disconnect(connection)
            raise

    @classmethod
    def _is_unsafe_examine_error(cls, detail: object) -> bool:
        text = cls._response_detail(detail).casefold()
        return "unsafe login" in text or "unsafelogin" in text

    @classmethod
    def _select_folder(cls, connection, folder: str):
        """Open a folder, with a Coremail compatibility fallback.

        Some IMAP services reject IMAP's read-only ``EXAMINE``
        command even after successful LOGIN and return ``Unsafe Login``.  A
        normal ``SELECT`` is accepted by those servers and is safe here because
        all mailbox reads use BODY.PEEK and never mutate message flags.
        """
        try:
            status, detail = connection.select(folder, readonly=True)
        except imaplib.IMAP4.error as exc:
            if not cls._is_unsafe_examine_error(exc):
                raise
            status, detail = connection.select(folder, readonly=False)
            return status, detail
        if cls._status_is_ok(status) or not cls._is_unsafe_examine_error(detail):
            return status, detail
        return connection.select(folder, readonly=False)

    @staticmethod
    def _disconnect(connection) -> None:
        if connection is None:
            return
        try:
            connection.logout()
        except Exception:
            pass

    @staticmethod
    def _response_detail(data: object) -> str:
        if isinstance(data, (list, tuple)):
            data = next((item for item in data if item), "")
        if isinstance(data, bytes):
            data = data.decode("utf-8", errors="replace")
        return str(data or "unknown error")[:200]

    @staticmethod
    def _list_mailbox_name(value: object) -> str:
        """Extract the mailbox name from one RFC 3501 LIST response line."""
        if isinstance(value, bytes):
            value = value.decode("utf-8", errors="replace")
        line = str(value or "").strip()
        match = re.search(r'(?:"((?:\\\\.|[^"\\\\])*)"|([^\s]+))\s*$', line)
        if not match:
            return ""
        if match.group(1) is None:
            return str(match.group(2) or "")
        return re.sub(r"\\\\(.)", r"\1", match.group(1))

    def _resolve_folder(self, connection) -> str:
        """Resolve folder casing from LIST for servers where names are case-sensitive."""
        try:
            status, data = connection.list()
        except (imaplib.IMAP4.error, imaplib.IMAP4.abort, OSError):
            return self.imap_folder
        if not self._status_is_ok(status):
            return self.imap_folder
        configured = self.imap_folder.casefold()
        for item in data or []:
            folder = self._list_mailbox_name(item)
            if folder and folder.casefold() == configured:
                return folder
        return self.imap_folder

    def _selected_folder(self, connection) -> str:
        return str(
            getattr(connection, "_domain_mailbox_selected_folder", "")
            or self.imap_folder
        )

    def _list_uids(self, connection) -> list[str]:
        status, data = connection.uid("search", None, "ALL")
        if not self._status_is_ok(status):
            raise RuntimeError(f"IMAP 搜索邮件失败: {self._response_detail(data)}")
        raw = data[0] if data else b""
        if isinstance(raw, str):
            raw = raw.encode("ascii", errors="ignore")
        if not raw.strip():
            status, data = connection.uid("search", None, "UID", "1:*")
            if not self._status_is_ok(status):
                raise RuntimeError(f"IMAP 搜索邮件失败: {self._response_detail(data)}")
            raw = data[0] if data else b""
            if isinstance(raw, str):
                raw = raw.encode("ascii", errors="ignore")
        return [value.decode("ascii", errors="ignore") for value in raw.split() if value]

    def _fetch_message(self, connection, uid: str) -> Message:
        status, data = connection.uid("fetch", str(uid), "(BODY.PEEK[])")
        if not self._status_is_ok(status):
            raise RuntimeError(f"IMAP 读取邮件 UID {uid} 失败: {self._response_detail(data)}")
        raw = b""
        for item in data or []:
            if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes):
                raw = item[1]
                break
        if not raw:
            raise RuntimeError(f"IMAP 邮件 UID {uid} 没有正文")
        return email_lib.message_from_bytes(raw, policy=default_email_policy)

    @staticmethod
    def _normalize_address(value: str) -> str:
        return str(value or "").strip().strip("<>").lower()

    @classmethod
    def _message_recipients(cls, message: Message) -> set[str]:
        recipients: set[str] = set()
        for header in RECIPIENT_HEADERS:
            for value in message.get_all(header, []) or []:
                decoded = _decode_mime_header(value)
                for _, address in getaddresses([decoded]):
                    normalized = cls._normalize_address(address)
                    if normalized:
                        recipients.add(normalized)
                for match in re.findall(
                    r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-z0-9.-]+",
                    decoded,
                    flags=re.IGNORECASE,
                ):
                    recipients.add(cls._normalize_address(match))
        return recipients

    @classmethod
    def _message_is_for_account(cls, message: Message, account: MailboxAccount) -> bool:
        target = cls._normalize_address(getattr(account, "email", ""))
        return bool(target and target in cls._message_recipients(message))

    @staticmethod
    def _part_text(part: Message) -> str:
        try:
            content = part.get_content()
            if isinstance(content, str):
                return content
        except Exception:
            pass
        payload = part.get_payload(decode=True)
        if isinstance(payload, bytes):
            charset = part.get_content_charset() or "utf-8"
            try:
                return payload.decode(charset, errors="replace")
            except LookupError:
                return payload.decode("utf-8", errors="replace")
        return str(payload or "")

    @classmethod
    def _message_text(cls, message: Message) -> str:
        chunks = [
            _decode_mime_header(message.get("Subject", "")),
            _decode_mime_header(message.get("From", "")),
        ]
        parts = message.walk() if message.is_multipart() else (message,)
        for part in parts:
            if part.get_content_maintype() == "multipart":
                continue
            if str(part.get_content_disposition() or "").lower() == "attachment":
                continue
            if part.get_content_type() not in {"text/plain", "text/html"}:
                continue
            chunks.append(cls._part_text(part))
        return "\n".join(chunk for chunk in chunks if chunk)

    @staticmethod
    def _clean_search_text(text: str) -> str:
        cleaned = re.sub(r"<style[^>]*>.*?</style>", " ", str(text or ""), flags=re.I | re.S)
        cleaned = re.sub(r"<script[^>]*>.*?</script>", " ", cleaned, flags=re.I | re.S)
        cleaned = re.sub(r"<[^>]+>", " ", cleaned)
        cleaned = html.unescape(cleaned)
        cleaned = re.sub(r"[a-z0-9._%+-]+@[a-z0-9.-]+\.[a-z]{2,}", " ", cleaned, flags=re.I)
        return re.sub(r"\s+", " ", cleaned).strip()

    def get_current_ids(self, account: MailboxAccount) -> set:
        del account
        connection = self._connect()
        try:
            uids = self._list_uids(connection)
            numeric_uids = [int(uid) for uid in uids if uid.isdigit()]
            return {str(max(numeric_uids))} if numeric_uids else set()
        finally:
            self._disconnect(connection)

    def get_last_code_identity(self, account: MailboxAccount) -> str | None:
        return self._last_code_identity.get(str(account.email or "").strip().lower()) or None

    def _wait_for_value(
        self,
        account: MailboxAccount,
        *,
        keyword: str,
        timeout: int,
        before_ids: set | None,
        cancel_check: Callable[[], bool] | None,
        manual_code_check: Callable[[], str | None] | None,
        extractor: Callable[[str], str | None],
        timeout_label: str,
    ) -> str:
        seen = {str(value) for value in (before_ids or set())}
        baseline_uid = max((int(value) for value in seen if value.isdigit()), default=0)
        deadline = time.monotonic() + max(int(timeout), 1)
        last_error = ""
        while time.monotonic() < deadline:
            _raise_if_cancelled(cancel_check)
            manual_code = _read_manual_code(manual_code_check)
            if manual_code:
                return manual_code
            connection = None
            try:
                connection = self._connect()
                pending_uids = [
                    uid
                    for uid in self._list_uids(connection)
                    if uid not in seen and (not uid.isdigit() or int(uid) > baseline_uid)
                ]
                for uid in reversed(pending_uids):
                    _raise_if_cancelled(cancel_check)
                    manual_code = _read_manual_code(manual_code_check)
                    if manual_code:
                        return manual_code
                    message = self._fetch_message(connection, uid)
                    seen.add(uid)
                    if not self._message_is_for_account(message, account):
                        continue
                    raw_text = self._message_text(message)
                    cleaned_text = self._clean_search_text(raw_text)
                    if keyword and keyword.casefold() not in cleaned_text.casefold():
                        continue
                    value = extractor(raw_text)
                    if value:
                        self._last_code_identity[str(account.email or "").strip().lower()] = str(uid)
                        return _read_manual_code(manual_code_check) or value
            except DomainMailboxConfigurationError:
                raise
            except (imaplib.IMAP4.abort, OSError, TimeoutError, RuntimeError) as exc:
                last_error = str(exc).strip() or exc.__class__.__name__
            finally:
                self._disconnect(connection)
            remaining = deadline - time.monotonic()
            if remaining > 0:
                manual_code = _sleep_with_cancel(
                    min(self.poll_interval, remaining),
                    cancel_check,
                    manual_code_check,
                )
                if manual_code:
                    return manual_code
        manual_code = _read_manual_code(manual_code_check)
        if manual_code:
            return manual_code
        suffix = f"，最后错误: {last_error}" if last_error else ""
        raise TimeoutError(f"等待域名邮箱{timeout_label}超时 ({timeout}s){suffix}")

    def wait_for_code(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        code_pattern: str | None = None,
        cancel_check: Callable[[], bool] | None = None,
        manual_code_check: Callable[[], str | None] | None = None,
    ) -> str:
        pattern = re.compile(code_pattern or DEFAULT_CODE_PATTERN)

        def extract(raw_text: str) -> str | None:
            match = pattern.search(self._clean_search_text(raw_text))
            if not match:
                return None
            return match.group(1) if match.groups() else match.group(0)

        return self._wait_for_value(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
            cancel_check=cancel_check,
            manual_code_check=manual_code_check,
            extractor=extract,
            timeout_label="验证码",
        )

    def wait_for_link(
        self,
        account: MailboxAccount,
        keyword: str = "",
        timeout: int = 120,
        before_ids: set | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> str:
        return self._wait_for_value(
            account,
            keyword=keyword,
            timeout=timeout,
            before_ids=before_ids,
            cancel_check=cancel_check,
            manual_code_check=None,
            extractor=lambda raw_text: _extract_verification_link(raw_text, keyword),
            timeout_label="验证链接",
        )

    def test_connection(self) -> dict:
        connection = self._connect()
        try:
            selected_folder = self._selected_folder(connection)
            message_count = len(self._list_uids(connection))
        finally:
            self._disconnect(connection)
        sample = self._random_email()
        return {
            "ok": True,
            "message": f"IMAP 连接成功，目录 {selected_folder} 当前有 {message_count} 封邮件；示例地址: {sample}",
            "email": sample,
        }
