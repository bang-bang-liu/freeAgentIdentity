from __future__ import annotations

import hashlib
import queue
import threading
import time
from typing import Any

from .errors import BrowserReuseRequiredError, IdentityResolutionError, RegistrationUnsupportedError
from .models import RegistrationContext


def has_reusable_oauth_browser(identity: Any) -> bool:
    return bool((getattr(identity, "chrome_user_data_dir", "") or "").strip() or (getattr(identity, "chrome_cdp_url", "") or "").strip())


def resolve_timeout(extra: dict[str, Any], keys: tuple[str, ...], default: int) -> int:
    for key in keys:
        value = extra.get(key)
        if value not in (None, ""):
            return int(value)
    return int(default)


def ensure_identity_email(ctx: RegistrationContext, message: str) -> None:
    if not getattr(ctx.identity, "email", ""):
        raise IdentityResolutionError(message)


def ensure_mailbox_identity(ctx: RegistrationContext, message: str) -> None:
    if not getattr(ctx.identity, "has_mailbox", False):
        raise IdentityResolutionError(message)


def ensure_oauth_executor_allowed(ctx: RegistrationContext, allowed_executor_types: tuple[str, ...] | None, message: str | None = None) -> None:
    if not allowed_executor_types:
        return
    if ctx.executor_type not in allowed_executor_types:
        expected = ", ".join(allowed_executor_types)
        raise RegistrationUnsupportedError(message or f"{ctx.platform_display_name} 当前 OAuth 仅支持 executor_type={expected}")


def ensure_oauth_browser_reuse(ctx: RegistrationContext, message: str) -> None:
    if not has_reusable_oauth_browser(ctx.identity):
        raise BrowserReuseRequiredError(message)


def _wait_for_code_with_manual_override(
    mailbox: Any,
    mail_acct: Any,
    *,
    kwargs: dict[str, Any],
    manual_otp_handle: Any,
    request_id: str,
    timeout: int,
) -> tuple[str, bool]:
    """Race automatic mailbox polling with a prompt manual handoff."""
    results: queue.Queue[tuple[str, Any]] = queue.Queue(maxsize=1)
    stop_event = threading.Event()
    cancel_check = kwargs.get("cancel_check")
    worker_kwargs = dict(kwargs)

    def combined_cancel_check() -> bool:
        return stop_event.is_set() or (
            callable(cancel_check) and bool(cancel_check())
        )

    worker_kwargs["cancel_check"] = combined_cancel_check

    def automatic_wait() -> None:
        try:
            results.put(("code", mailbox.wait_for_code(mail_acct, **worker_kwargs)))
        except Exception as exc:
            results.put(("error", exc))

    worker = threading.Thread(
        target=automatic_wait,
        name="automatic-mailbox-otp",
        daemon=True,
    )
    worker.start()
    deadline = time.monotonic() + max(int(timeout), 1)
    automatic_error: Exception | None = None
    try:
        while True:
            if callable(cancel_check) and cancel_check():
                raise RuntimeError("任务已取消")
            manual_code = manual_otp_handle.consume_code(request_id)
            if manual_code:
                return str(manual_code), True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if automatic_error is not None:
                    raise automatic_error
                raise TimeoutError(f"等待验证码超时 ({timeout}s)")
            try:
                result_type, value = results.get(timeout=min(0.1, remaining))
            except queue.Empty:
                continue
            if result_type == "error":
                # Keep the manual request alive when automatic IMAP/API
                # polling fails.  The operator can still submit the code from
                # the mailbox UI; surface the automatic error only if manual
                # input also times out.
                automatic_error = value if isinstance(value, Exception) else RuntimeError(str(value))
                continue
            manual_code = manual_otp_handle.consume_code(request_id)
            return str(manual_code or value or ""), bool(manual_code)
    finally:
        stop_event.set()


def _refresh_mailbox_baseline(ctx: RegistrationContext, mailbox: Any, mail_acct: Any) -> None:
    try:
        ctx.identity.before_ids = mailbox.get_current_ids(mail_acct)
    except Exception:
        pass


def _refresh_mailbox_baseline_async(
    ctx: RegistrationContext,
    mailbox: Any,
    mail_acct: Any,
) -> None:
    threading.Thread(
        target=_refresh_mailbox_baseline,
        args=(ctx, mailbox, mail_acct),
        name="refresh-mailbox-baseline",
        daemon=True,
    ).start()


def _otp_fingerprint(value: object) -> str:
    """Return a short, non-reversible diagnostic fingerprint for an OTP."""
    normalized = str(value or "").strip()
    if not normalized:
        return ""
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:12]


def _message_fingerprint(value: object) -> str:
    """Return a short diagnostic fingerprint for a mailbox message identity."""
    return _otp_fingerprint(value)


def build_otp_callback(
    ctx: RegistrationContext,
    *,
    keyword: str = "",
    timeout: int | None = None,
    code_pattern: str | None = None,
    wait_message: str = "等待验证码...",
    success_label: str = "验证码",
):
    mailbox = getattr(ctx.platform, "mailbox", None)
    mail_acct = getattr(ctx.identity, "mailbox_account", None)
    if not mailbox or not mail_acct:
        return None

    extra = getattr(ctx, "extra", {}) or {}
    manual_otp_handle = extra.get("_manual_otp_handle") if isinstance(extra, dict) else None

    def otp_cb(*, timeout_override: int | None = None):
        ctx.log(wait_message)
        # The security flow inspects these attributes to distinguish a newly
        # delivered message from a stale response with the same numeric OTP.
        # Clear them before every mailbox request so a timeout/manual entry
        # cannot accidentally inherit the previous message identity.
        otp_cb.last_message_identity = None
        otp_cb.last_message_manual = False
        otp_cb.last_code_fingerprint = ""
        otp_cb.last_message_fingerprint = ""
        otp_cb.last_message_received_at = ""
        kwargs = {
            "keyword": keyword,
            "before_ids": getattr(ctx.identity, "before_ids", set()),
            "cancel_check": getattr(ctx.platform, "is_cancel_requested", None),
        }
        effective_timeout = timeout if timeout_override is None else max(int(timeout_override), 1)
        if effective_timeout is not None:
            kwargs["timeout"] = effective_timeout
        if code_pattern:
            kwargs["code_pattern"] = code_pattern
        request_id = ""
        manual_override_used = False
        if manual_otp_handle and callable(getattr(manual_otp_handle, "open_request", None)):
            request_id = manual_otp_handle.open_request(getattr(mail_acct, "email", ""))
        try:
            if request_id:
                code, manual_override_used = _wait_for_code_with_manual_override(
                    mailbox,
                    mail_acct,
                    kwargs=kwargs,
                    manual_otp_handle=manual_otp_handle,
                    request_id=request_id,
                    timeout=int(effective_timeout or 120),
                )
            else:
                code = mailbox.wait_for_code(mail_acct, **kwargs)
        finally:
            if request_id:
                manual_otp_handle.close_request(request_id)
        if code:
            if not manual_override_used:
                try:
                    otp_cb.last_message_identity = mailbox.get_last_code_identity(mail_acct)
                except Exception:
                    otp_cb.last_message_identity = None
            otp_cb.last_message_manual = bool(manual_override_used)
            otp_cb.last_code_fingerprint = _otp_fingerprint(code)
            otp_cb.last_message_fingerprint = _message_fingerprint(
                otp_cb.last_message_identity
            )
            try:
                metadata = mailbox.get_last_code_metadata(mail_acct)
            except Exception:
                metadata = {}
            if isinstance(metadata, dict):
                otp_cb.last_message_received_at = str(
                    metadata.get("received_at") or metadata.get("receivedAt") or ""
                ).strip()
            # A later security re-auth must wait for a genuinely new email.
            # Advance the identity baseline after this code has been consumed
            # so the same message cannot be returned a second time.
            if manual_override_used:
                _refresh_mailbox_baseline_async(ctx, mailbox, mail_acct)
            else:
                _refresh_mailbox_baseline(ctx, mailbox, mail_acct)
            source = "手动输入" if manual_override_used else "邮箱"
            message_tag = otp_cb.last_message_fingerprint or "unknown"
            code_tag = otp_cb.last_code_fingerprint or "unknown"
            received_at = otp_cb.last_message_received_at or "unknown"
            metadata_source = ""
            try:
                metadata_source = str(
                    (metadata if isinstance(metadata, dict) else {}).get("source") or ""
                ).strip()
            except Exception:
                metadata_source = ""
            ctx.log(
                f"{success_label}已获取（来源={source}，"
                f"验证码={str(code).strip()}，邮件时间={received_at}，"
                f"邮件接口={metadata_source or 'unknown'}，"
                f"消息指纹={message_tag}，"
                f"验证码指纹={code_tag}）"
            )
        return code

    otp_cb.supports_timeout_override = True
    otp_cb.last_message_identity = None
    otp_cb.last_message_manual = False
    otp_cb.last_code_fingerprint = ""
    otp_cb.last_message_fingerprint = ""
    otp_cb.last_message_received_at = ""
    return otp_cb


def build_link_callback(
    ctx: RegistrationContext,
    *,
    keyword: str = "",
    timeout: int | None = None,
    wait_message: str = "等待验证链接邮件...",
    success_label: str = "验证链接",
    preview_chars: int = 80,
):
    mailbox = getattr(ctx.platform, "mailbox", None)
    mail_acct = getattr(ctx.identity, "mailbox_account", None)
    if not mailbox or not mail_acct:
        return None

    def link_cb():
        ctx.log(wait_message)
        before_ids = mailbox.get_current_ids(mail_acct)
        kwargs = {
            "keyword": keyword,
            "before_ids": before_ids,
            "cancel_check": getattr(ctx.platform, "is_cancel_requested", None),
        }
        if timeout is not None:
            kwargs["timeout"] = timeout
        link = mailbox.wait_for_link(mail_acct, **kwargs)
        if link:
            preview = link if len(link) <= preview_chars else f"{link[:preview_chars]}..."
            ctx.log(f"{success_label}: {preview}")
        return link

    return link_cb
