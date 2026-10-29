"""ChatGPT 刷新登录客户端：密码登录、邮箱 OTP 登录、TOTP 二次验证。

设计依据：docs/CHATGPT_LOGIN_REFRESH_DESIGN.md 6.2 / 6.3 / 11.1.1。

邮箱 OTP 登录不是新写的协议交互。``registration_core/auth_flow.py`` 里已经
具备全部原语：

- ``login_password_verify()``  密码登录
- ``kickoff_otp_delivery()`` / ``send_otp()``  发码（区分新注册与已有账号）
- ``verify_otp()``  验码
- ``get_auth_session()``  取 session

本模块做的是把这些原语按刷新场景重新编排，并接入手动 OTP 兜底。

安全边界：返回 ``ChatGPTLoginResult``，其中 token/cookie 只在平台层与持久化层
之间流转。本模块自身的日志只输出布尔与脱敏值。
"""
from __future__ import annotations

import base64
import json
import re
import time
import threading
import weakref
from urllib.parse import urljoin, urlsplit
from datetime import datetime, timezone
from typing import Any, Callable

from platforms.chatgpt.login_types import (
    ChatGPTLoginError,
    ChatGPTLoginResult,
    LoginErrorCode,
    LoginMethod,
)
from platforms.chatgpt.token_validation import network_login_error, validate_chatgpt_token
from registration_core.auth_errors import AuthFlowError


# 单个认证阶段的默认上限（秒）。任何网络等待都必须有界，见设计 12.3。
DEFAULT_ACCOUNT_DEADLINE = 300
DEFAULT_EMAIL_OTP_TIMEOUT = 180


def _text(value: Any) -> str:
    return str(value or "").strip()


def mask_email(email: str) -> str:
    """``a***@example.com``。日志里的账号标识统一用这个形式。"""
    raw = _text(email)
    if "@" not in raw:
        return "***" if raw else ""
    local, _, domain = raw.partition("@")
    head = local[:1] if local else ""
    return f"{head}***@{domain}"


def parse_access_token_expiry(access_token: str) -> str:
    """从 JWT ``exp`` 解析过期时间，返回 ISO8601 UTC 串。

    解析不出来时返回空串。**不能**因为解析失败就硬编码七天——见设计 8.2
    对 ``access_token_expires_at`` 的约定和 13.1 的对应测试项。
    """
    token = _text(access_token)
    if not token or token.count(".") != 2:
        return ""
    payload_segment = token.split(".")[1]
    padding = "=" * (-len(payload_segment) % 4)
    try:
        raw = base64.urlsafe_b64decode(payload_segment + padding)
        payload = json.loads(raw)
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    exp = payload.get("exp")
    if not isinstance(exp, (int, float)) or isinstance(exp, bool) or exp <= 0:
        return ""
    try:
        moment = datetime.fromtimestamp(float(exp), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return ""
    return moment.isoformat().replace("+00:00", "Z")


class EmailOtpResolver:
    """按"自动邮箱 -> 手动输入"的顺序取邮箱验证码。

    ``mode`` 取 ``auto`` / ``manual`` / ``auto_then_manual``（默认）。

    只读取登录请求发起之后出现的新邮件：每次取码前先用 ``get_current_ids()``
    建立基线，避免复用注册阶段或上一次 re-auth 的旧验证码（设计 6.4）。
    """

    def __init__(
        self,
        *,
        rebuilt: Any = None,
        manual_handle: Any = None,
        mode: str = "auto_then_manual",
        timeout: int = DEFAULT_EMAIL_OTP_TIMEOUT,
        log: Callable[[str], None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
        email: str = "",
    ) -> None:
        self.rebuilt = rebuilt
        self.manual_handle = manual_handle
        self.mode = _text(mode) or "auto_then_manual"
        self.timeout = max(int(timeout or DEFAULT_EMAIL_OTP_TIMEOUT), 1)
        self.log = log or (lambda _message: None)
        self.cancel_check = cancel_check
        self.email = _text(email)
        self.last_source = ""

    @property
    def _auto_available(self) -> bool:
        return bool(
            self.mode in {"auto", "auto_then_manual"}
            and self.rebuilt is not None
            and getattr(self.rebuilt, "ok", False)
            and getattr(self.rebuilt, "mailbox", None) is not None
        )

    @property
    def _manual_available(self) -> bool:
        return bool(
            self.mode in {"manual", "auto_then_manual"}
            and self.manual_handle is not None
        )

    def describe(self) -> str:
        """给日志用的能力摘要，不含任何 secret。"""
        return (
            f"auto={'yes' if self._auto_available else 'no'}, "
            f"manual={'yes' if self._manual_available else 'no'}"
        )

    def close(self) -> None:
        """Release an automatically rebuilt mailbox connection, if any."""
        close = getattr(self.rebuilt, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass

    def _check_cancelled(self) -> None:
        if self.cancel_check and self.cancel_check():
            raise ChatGPTLoginError(LoginErrorCode.CANCELLED, "任务已取消")

    def baseline(self) -> set:
        """取码前的邮件基线。失败返回空集合，不影响后续流程。"""
        if not self._auto_available:
            return set()
        self._check_cancelled()
        try:
            ids = self.rebuilt.mailbox.get_current_ids(self.rebuilt.account)
            self._check_cancelled()
            return set(ids or set())
        except ChatGPTLoginError:
            raise
        except Exception:
            self._check_cancelled()
            return set()

    def fetch(
        self,
        *,
        purpose: str = "email_login",
        before_ids: set | None = None,
        issued_after: float | None = None,
    ) -> str:
        """返回验证码。取不到抛 ``ChatGPTLoginError``。

        ``purpose`` 只用于手动请求的展示，取 ``email_login`` /
        ``mfa_login`` / ``security_reauth``。
        """
        # The caller may establish the baseline before triggering the OTP
        # delivery request.  This is important for existing-account login:
        # authorize/continue can deliver the message before control returns to
        # this method.  Keeping the old default preserves the standalone
        # resolver API used by registration and tests.
        if before_ids is None:
            before_ids = self.baseline()
        if self._auto_available:
            code = self._fetch_auto(before_ids, issued_after=issued_after)
            if code:
                self.last_source = "auto"
                self.log("邮箱验证码已自动获取")
                return code
            self._check_cancelled()
            self.log("自动取码未成功，转手动输入")

        if self._manual_available:
            code = self._fetch_manual(purpose=purpose)
            if code:
                self.last_source = "manual"
                return code

        self._check_cancelled()
        raise ChatGPTLoginError(
            LoginErrorCode.MANUAL_OTP_TIMEOUT
            if self._manual_available
            else LoginErrorCode.EMAIL_OTP_INVALID,
            "未能获取邮箱验证码",
        )

    def _fetch_auto(self, before_ids: set, *, issued_after: float | None = None) -> str:
        try:
            self._check_cancelled()
            kwargs = {
                "timeout": self.timeout,
                "before_ids": before_ids,
                "cancel_check": self.cancel_check,
            }
            if issued_after is not None:
                kwargs["issued_after"] = issued_after
            try:
                code = self.rebuilt.mailbox.wait_for_code(self.rebuilt.account, **kwargs)
            except TypeError as exc:
                # Third-party/test mailbox adapters may still implement the
                # older wait_for_code signature.  Retry without the optional
                # timestamp only when that is the incompatibility.
                if "issued_after" not in kwargs or "issued_after" not in str(exc):
                    raise
                kwargs.pop("issued_after", None)
                code = self.rebuilt.mailbox.wait_for_code(self.rebuilt.account, **kwargs)
        except Exception as exc:
            try:
                self._check_cancelled()
            except ChatGPTLoginError as cancelled:
                raise cancelled from exc
            self.log(f"自动取码失败: {type(exc).__name__}")
            return ""
        self._check_cancelled()
        return _text(code)

    def _fetch_manual(self, *, purpose: str) -> str:
        handle = self.manual_handle
        email = self.email or (getattr(self.rebuilt, "email", "") if self.rebuilt else "")
        try:
            request_id = handle.open_request(email)
        except Exception as exc:
            self.log(f"创建手动验证码请求失败: {type(exc).__name__}")
            return ""
        self.log(f"等待手动输入邮箱验证码: {mask_email(email)} · 用途 {purpose}")
        deadline = time.monotonic() + self.timeout
        try:
            while time.monotonic() < deadline:
                self._check_cancelled()
                code = handle.consume_code(request_id)
                if code:
                    self.log("已收到手动输入的邮箱验证码")
                    return _text(code)
                sleep_deadline = min(deadline, time.monotonic() + 0.5)
                while time.monotonic() < sleep_deadline:
                    self._check_cancelled()
                    remaining = sleep_deadline - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(0.1, remaining))
        finally:
            try:
                handle.close_request(request_id)
            except Exception:
                pass
        return ""


# ---------------------------------------------------------------- 登录编排

def _page_type(auth: Any, step: dict | None) -> str:
    try:
        return _text(auth._extract_page_type(step)).lower()
    except Exception:
        return ""


def _continue_url(auth: Any, step: dict | None) -> str:
    try:
        value = _text(auth._extract_continue_url_from_step(step))
        # AuthFlow 的通用 normalize 还会提交 workspace/select；此处只解析地址。
        return urljoin("https://auth.openai.com", value) if value.startswith("/") else value
    except Exception:
        return ""


def _is_email_otp_page(page_type: str, continue_url: str) -> bool:
    return page_type == "email_otp_verification" or "/email-verification" in (continue_url or "")


def _is_password_page(page_type: str, continue_url: str) -> bool:
    return page_type == "login_password" or "/log-in/password" in (continue_url or "")


def _is_rejected_otp(exc: Exception) -> bool:
    detail = f"{getattr(exc, 'error_code', '')} {exc}".lower()
    return bool(re.search(
        r"(?:invalid|incorrect|wrong|expired)[_\s-]*(?:email[_\s-]*)?(?:totp|otp|code)"
        r"|(?:totp|otp|code)[_\s-]*(?:invalid|incorrect|wrong|expired)", detail,
    ))


def _login_landing_step(auth: Any) -> dict:
    """按实际 OAuth 落地页继续，避免再次提交已推进过的 authorize/continue。"""
    landed = _text(getattr(auth, "_oauth_landed_url", ""))
    parsed = urlsplit(landed)
    path = parsed.path.rstrip("/")
    if parsed.hostname == "auth.openai.com":
        page_type = ""
        if path == "/log-in/password":
            page_type = "login_password"
        elif path == "/email-verification":
            page_type = "email_otp_verification"
        elif path.startswith("/mfa-challenge/"):
            page_type = "mfa_challenge"
        elif path.startswith("/workspace"):
            page_type = "workspace"
        if page_type:
            return {"page": {"type": page_type}, "continue_url": landed}
        # 只有真正停在邮箱输入页时才继续提交邮箱。
        if path in {"/log-in", "/login", "/u/login", "/create-account", "/log-in-or-create-account"}:
            return {}
    elif parsed.hostname == "chatgpt.com" and (
        path.startswith("/api/auth/callback/") or path == ""
    ):
        return {"page": {"type": "existing_session"}, "continue_url": landed}
    raise ChatGPTLoginError(LoginErrorCode.API_RESPONSE_INVALID,
                           f"OAuth 返回未识别的登录页：{path or '/'}")


def _finish_session(auth: Any, continue_url: str) -> None:
    """Consume the web callback before asking NextAuth for its session."""
    if continue_url:
        parsed = urlsplit(continue_url)
        if parsed.hostname == "chatgpt.com" and parsed.path.startswith("/api/auth/callback/"):
            callback_url = continue_url
        else:
            callback_url, _final_url = auth.follow_redirect_chain(continue_url)
        if callback_url:
            auth._consume_callback_for_session(callback_url)
    auth.get_auth_session()
    if not _text(getattr(auth.result, "access_token", "")):
        time.sleep(0.5)
        auth.get_auth_session()


def _select_workspace(auth: Any, step: dict, continue_url: str, emit: Callable) -> str:
    # Adapted from toSub2's selectChatgptLoginWorkspaceIfNeeded (MIT).
    if "workspace" not in _page_type(auth, step) and "/workspace" not in continue_url:
        return continue_url
    step = step if isinstance(step, dict) else {}
    data = step.get("oai-client-auth-session") or step.get("data") or {}
    data = data if isinstance(data, dict) else {}
    workspaces = data.get("workspaces") or step.get("workspaces") or []
    workspace_id = str(data.get("workspace_id") or step.get("workspace_id") or "")
    if not workspace_id:
        selectable = [w for w in workspaces if isinstance(w, dict) and w.get("id")]
        preferred = next((w for w in selectable if w.get("kind") == "organization"), None)
        workspace_id = str((preferred or next(iter(selectable), {})).get("id") or "")
    workspace_id = workspace_id or auth._extract_workspace_id()
    if not workspace_id:
        # follow_redirect_chain also extracts workspace IDs from SSR HTML.
        return continue_url
    emit("选择 ChatGPT 工作区")
    next_url = auth._workspace_select(workspace_id)
    if not next_url:
        raise ChatGPTLoginError(LoginErrorCode.API_RESPONSE_INVALID, "工作区选择未返回下一步地址")
    return urljoin("https://auth.openai.com", next_url)


def _decode_account_id(access_token: str) -> str:
    """从 AT 的 JWT payload 解析 account id，解析不出返回空串。"""
    token = _text(access_token)
    if token.count(".") != 2:
        return ""
    segment = token.split(".")[1]
    padding = "=" * (-len(segment) % 4)
    try:
        payload = json.loads(base64.urlsafe_b64decode(segment + padding))
    except Exception:
        return ""
    if not isinstance(payload, dict):
        return ""
    auth_claim = payload.get("https://api.openai.com/auth")
    if isinstance(auth_claim, dict):
        found = _text(auth_claim.get("chatgpt_account_id"))
        if found:
            return found
    for key in ("chatgpt_account_id", "account_id", "sub"):
        value = _text(payload.get(key))
        if value:
            return value
    return ""


def login_chatgpt_web(
    *,
    email: str,
    password: str = "",
    totp_secret: str = "",
    proxy: str | None = None,
    otp_resolver: EmailOtpResolver | None = None,
    manual_totp_handle: Any = None,
    log: Callable[[str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    register_cancel_callback: Callable[[Callable[[], None]], None] | None = None,
    deadline_seconds: int = DEFAULT_ACCOUNT_DEADLINE,
    _proxy_attempt: int = 0,
    _deadline: float | None = None,
) -> ChatGPTLoginResult:
    """重新登录 ChatGPT web，返回新的凭据快照。

    登录方式按已有资料自动选择（设计 6.2）：有密码走密码登录，无密码走邮箱
    OTP 登录；服务端要求 MFA 时用已保存的 TOTP，没有则转手动输入。

    所有网络等待都受 ``deadline_seconds`` 约束。
    """
    from registration_core.auth_flow import AuthFlow
    from registration_core.config import Config
    from registration_core.mfa_totp_protocol import totp_code_candidates
    from rebind_core.mfa_login import (
        MfaLoginError,
        _cookie_value,
        _parse_client_auth_session,
        extract_factor_id,
        issue_mfa_challenge,
        verify_mfa_totp,
    )

    emit = log or (lambda _message: None)
    email = _text(email)
    password = _text(password)
    totp_secret = _text(totp_secret).replace(" ", "").upper()
    if not email:
        raise ChatGPTLoginError(LoginErrorCode.MISSING_EMAIL, "账号邮箱为空")

    deadline = _deadline or time.monotonic() + max(int(deadline_seconds or DEFAULT_ACCOUNT_DEADLINE), 1)

    def _check_budget() -> None:
        if cancel_check and cancel_check():
            raise ChatGPTLoginError(LoginErrorCode.CANCELLED, "任务已取消")
        if time.monotonic() > deadline:
            raise ChatGPTLoginError(LoginErrorCode.NETWORK_TIMEOUT, "单账号认证超时")

    _check_budget()
    auth = AuthFlow(Config(proxy=proxy or None, registration_mode="protocol"))
    auth.enable_login_challenge_handling(log=emit, check_budget=_check_budget, deadline=deadline)
    # A running HTTP request may otherwise keep the task worker alive for the
    # request timeout after the cancel endpoint has returned.  Register a
    # weak-reference callback so the task can close the auth session promptly
    # without making the cancellation registry retain the session forever.
    auth_cancel_callback = None
    resolver_close = getattr(otp_resolver, "close", None)
    if callable(register_cancel_callback):
        try:
            auth_ref = weakref.ref(auth)
        except TypeError:
            # Small test/third-party AuthFlow adapters may use slots without
            # weak-reference support.  The task callback registry is cleared
            # at task completion, so a short-lived strong fallback is safe.
            auth_ref = lambda: auth

        def _close_auth_on_cancel() -> None:
            if callable(resolver_close):
                try:
                    resolver_close()
                except Exception:
                    pass
            current = auth_ref()
            if current is not None:
                try:
                    current.close()
                except Exception:
                    pass

        auth_cancel_callback = _close_auth_on_cancel
        try:
            register_cancel_callback(auth_cancel_callback)
        except Exception:
            auth_cancel_callback = None
    def close_at_deadline():
        try:
            auth.close()
        except Exception:
            pass
        if callable(resolver_close):
            try:
                resolver_close()
            except Exception:
                pass

    timeout_timer = threading.Timer(max(0.01, deadline - time.monotonic()), close_at_deadline)
    timeout_timer.daemon = True
    timeout_timer.start()
    try:
        auth.result.email = email
        if password:
            auth.result.password = password

        emit("初始化 ChatGPT 协议登录")
        # OAuth 文档跳转可能直接发码，必须先建立邮件基线。
        otp_before_ids: set = set()
        otp_issued_after: float | None = None
        try:
            for bootstrap_attempt in range(2):
                auth.open_chatgpt_home()
                _check_budget()
                auth.get_auth_providers()
                _check_budget()
                csrf = auth.get_csrf_token()
                if not auth._get_cookie_value_by_name("__Host-next-auth.csrf-token"):
                    raise ChatGPTLoginError(LoginErrorCode.API_RESPONSE_INVALID, "登录引导未建立 CSRF Cookie")
                auth_url = auth.get_auth_url(csrf, email=email, screen_hint="login_or_signup", prompt="login")
                if otp_resolver is not None:
                    otp_before_ids = otp_resolver.baseline()
                otp_issued_after = time.time()
                _check_budget()
                device_id = auth.auth_oauth_init(auth_url)
                step = _login_landing_step(auth)
                if not step:
                    emit("OAuth 停在邮箱输入页，提交登录邮箱")
                    sentinel = auth.get_sentinel_token(device_id)
                    try:
                        step = auth.authorize_continue(
                            email=email, sentinel_token=sentinel, screen_hint="login",
                            referer=_text(auth._oauth_landed_url).split("?")[0],
                            trace_step="authorize_continue_login_refresh", allow_retry=False,
                        )
                    except AuthFlowError as exc:
                        if (bootstrap_attempt == 0 and exc.status_code in (400, 409)
                                and exc.error_code == "invalid_state"):
                            emit("登录会话已失效，重新初始化并按新的 OAuth 落地页继续")
                            continue
                        raise
                break
            auth._is_existing_account = True
        except ChatGPTLoginError:
            raise
        except Exception as exc:
            if cancel_check and cancel_check():
                raise ChatGPTLoginError(LoginErrorCode.CANCELLED, "任务已取消") from exc
            raise network_login_error(exc, "登录引导") from exc

        _check_budget()
        page_type = _page_type(auth, step)
        continue_url = _continue_url(auth, step)
        login_method = ""

        # ---- 第一段认证：密码 或 邮箱 OTP
        if password and _is_password_page(page_type, continue_url):
            emit("提交密码验证")
            _check_budget()
            try:
                auth.get_sentinel_token(device_id, flow="password_verify")
            except ChatGPTLoginError:
                raise
            except AuthFlowError as exc:
                raise network_login_error(exc, "密码安全验证") from exc
            except Exception:
                _check_budget()
            try:
                step = auth.login_password_verify(password)
            except Exception as exc:
                if cancel_check and cancel_check():
                    raise ChatGPTLoginError(LoginErrorCode.CANCELLED, "任务已取消") from exc
                low = str(exc).lower()
                if "invalid_username_or_password" in low or "401" in low:
                    raise ChatGPTLoginError(
                        LoginErrorCode.INVALID_PASSWORD, "密码被服务端拒绝"
                    ) from exc
                raise network_login_error(exc, "密码验证") from exc
            login_method = LoginMethod.PASSWORD
            page_type = _page_type(auth, step)
            continue_url = _continue_url(auth, step)
        if _is_email_otp_page(page_type, continue_url) or (not password and _is_password_page(page_type, continue_url)):
            if otp_resolver is None:
                raise ChatGPTLoginError(
                    LoginErrorCode.EMAIL_OTP_INVALID, "需要邮箱验证码但未提供取码途径"
                )
            emit(f"进入邮箱验证码登录: {otp_resolver.describe()}")
            _check_budget()
            try:
                if not _is_email_otp_page(page_type, continue_url):
                    if not auth.kickoff_otp_delivery("existing_passwordless_login"):
                        auth.send_otp("https://auth.openai.com/email-verification")
            except Exception as exc:
                if cancel_check and cancel_check():
                    raise ChatGPTLoginError(LoginErrorCode.CANCELLED, "任务已取消") from exc
                raise network_login_error(exc, "发送邮箱验证码") from exc

            otp_resolver.timeout = min(otp_resolver.timeout, max(1, int(deadline - time.monotonic())))
            code = otp_resolver.fetch(
                purpose="email_login",
                before_ids=otp_before_ids,
                issued_after=otp_issued_after,
            )
            _check_budget()
            emit("提交邮箱验证码")
            try:
                step = auth.verify_otp(code)
            except ChatGPTLoginError:
                raise
            except Exception as exc:
                _check_budget()
                if _is_rejected_otp(exc):
                    raise ChatGPTLoginError(LoginErrorCode.EMAIL_OTP_INVALID, "邮箱验证码被服务端拒绝") from exc
                raise network_login_error(exc, "邮箱验证码校验") from exc
            login_method = LoginMethod.EMAIL_OTP
            page_type = _page_type(auth, step)
            continue_url = _continue_url(auth, step)
        elif ("workspace" in page_type or "/workspace" in continue_url
              or "/api/auth/callback/" in continue_url or "mfa" in page_type
              or page_type == "existing_session"):
            login_method = login_method or LoginMethod.EXISTING_SESSION
        elif not login_method:
            raise ChatGPTLoginError(
                LoginErrorCode.API_RESPONSE_INVALID,
                f"未进入可识别的登录页 page_type={page_type or '(empty)'}",
            )

        # ---- 第二段认证：可选 TOTP
        client_auth = _parse_client_auth_session(
            _cookie_value(auth.session, "oai-client-auth-session")
        )
        factor_id = extract_factor_id(continue_url, step, client_auth)
        if factor_id:
            _check_budget()
            emit("服务端要求 MFA 验证")
            try:
                issue_mfa_challenge(auth, factor_id)
            except MfaLoginError as exc:
                if cancel_check and cancel_check():
                    raise ChatGPTLoginError(LoginErrorCode.CANCELLED, "任务已取消") from exc
                raise ChatGPTLoginError(LoginErrorCode.MFA_INVALID, exc.message) from exc
            except Exception as exc:
                _check_budget()
                if isinstance(exc, ChatGPTLoginError):
                    raise
                raise network_login_error(exc, "MFA challenge") from exc

            verified = False
            if totp_secret:
                emit("提交 TOTP 验证")
                for window in (1, 2):
                    if verified:
                        break
                    for candidate in totp_code_candidates(totp_secret, window=window):
                        _check_budget()
                        try:
                            verify_resp = verify_mfa_totp(auth, factor_id, candidate)
                        except Exception as exc:
                            _check_budget()
                            if isinstance(exc, ChatGPTLoginError):
                                raise
                            if _is_rejected_otp(exc):
                                continue
                            raise network_login_error(exc, "TOTP 验证") from exc
                        verified = True
                        step = verify_resp
                        continue_url = _continue_url(auth, verify_resp) or continue_url
                        break
                if not verified:
                    raise ChatGPTLoginError(LoginErrorCode.MFA_INVALID, "TOTP 验证未通过")
            elif manual_totp_handle is not None:
                emit("无已保存 TOTP，等待手动输入动态码")
                remaining = int(max(deadline - time.monotonic(), 1))
                manual_resolver = EmailOtpResolver(
                    manual_handle=manual_totp_handle,
                    mode="manual",
                    timeout=min(remaining, DEFAULT_EMAIL_OTP_TIMEOUT),
                    log=emit,
                    cancel_check=cancel_check,
                    email=email,
                )
                manual_code = manual_resolver.fetch(purpose="mfa_login")
                try:
                    verify_resp = verify_mfa_totp(auth, factor_id, manual_code)
                except Exception as exc:
                    _check_budget()
                    if isinstance(exc, ChatGPTLoginError):
                        raise
                    if _is_rejected_otp(exc):
                        raise ChatGPTLoginError(LoginErrorCode.MFA_INVALID, "手动输入的动态码被服务端拒绝") from exc
                    raise network_login_error(exc, "TOTP 验证") from exc
                step = verify_resp
                continue_url = _continue_url(auth, verify_resp) or continue_url
            else:
                raise ChatGPTLoginError(
                    LoginErrorCode.MFA_REQUIRED, "服务端要求 MFA，但账号无可用 TOTP"
                )
            login_method = (
                LoginMethod.PASSWORD_TOTP
                if login_method == LoginMethod.PASSWORD
                else LoginMethod.EMAIL_OTP_TOTP
            )

        # ---- 取 session
        _check_budget()
        try:
            continue_url = _select_workspace(auth, step, continue_url, emit)
        except ChatGPTLoginError:
            raise
        except Exception as exc:
            _check_budget()
            raise network_login_error(exc, "工作区选择") from exc
        emit("获取 ChatGPT session")
        try:
            _finish_session(auth, continue_url)
        except ChatGPTLoginError:
            raise
        except AuthFlowError as exc:
            raise network_login_error(exc, "session 获取") from exc
        except Exception as exc:
            if cancel_check and cancel_check():
                raise ChatGPTLoginError(LoginErrorCode.CANCELLED, "任务已取消") from exc
            raise ChatGPTLoginError(
                LoginErrorCode.TOKEN_FETCH_TIMEOUT, f"session 获取失败: {type(exc).__name__}"
            ) from exc

        result = auth.result
        access_token = _text(getattr(result, "access_token", ""))
        if not access_token:
            raise ChatGPTLoginError(
                LoginErrorCode.SESSION_NOT_RETURNED, "登录完成但未返回 access_token"
            )

        _check_budget()
        emit("使用 Bearer Token 验证业务接口与账号身份")
        profile = validate_chatgpt_token(
            access_token, expected_email=email, proxy=proxy,
            timeout=deadline - time.monotonic(), cancel_check=cancel_check,
            register_cancel_callback=register_cancel_callback,
            browser_profile=getattr(auth, "_browser_profile", None),
            challenge_options=getattr(auth, "_login_challenge_options", None),
        )
        _check_budget()
        from platforms.chatgpt.switch import extract_session_token
        cookie_header = _text(getattr(result, "cookie_header", ""))
        built = ChatGPTLoginResult(
            access_token=access_token,
            session_token=extract_session_token(_text(getattr(result, "session_token", "")), cookie_header),
            refresh_token=_text(getattr(result, "refresh_token", "")),
            id_token=_text(getattr(result, "id_token", "")),
            cookie_header=cookie_header,
            auth_session_json=_text(getattr(result, "auth_session_json", "")),
            device_id=_text(getattr(result, "device_id", "")) or _text(device_id),
            account_id=_decode_account_id(access_token) or _text(profile.get("account_id")) or _text(profile.get("id")),
            login_method=login_method or LoginMethod.PASSWORD,
            access_token_expires_at=parse_access_token_expiry(access_token),
            token_verified=True,
            verified_email=profile["email"],
            verified_user_id=profile["id"],
        )
        emit(
            "Token 已被业务接口接受，账号身份一致: accessToken=yes, "
            f"sessionToken={'yes' if built.session_token else 'no'}, "
            f"expires_at={built.access_token_expires_at or '未知'}"
        )
        return built
    except ChatGPTLoginError as exc:
        _check_budget()
        if exc.code == LoginErrorCode.RISK_CONTROL and _proxy_attempt < 9:
            from registration_core.login_transport import rotate_proxy_session
            next_proxy = rotate_proxy_session(proxy or "")
            if next_proxy:
                emit(f"Cloudflare 挑战仍未通过，使用新的代理会话重新登录（{_proxy_attempt + 2}/10）")
                timeout_timer.cancel()
                auth.close()
                return login_chatgpt_web(
                    email=email, password=password, totp_secret=totp_secret, proxy=next_proxy,
                    otp_resolver=otp_resolver, manual_totp_handle=manual_totp_handle,
                    log=emit, cancel_check=cancel_check, register_cancel_callback=register_cancel_callback,
                    deadline_seconds=deadline_seconds, _proxy_attempt=_proxy_attempt + 1, _deadline=deadline,
                )
        raise
    except Exception:
        _check_budget()
        raise
    finally:
        timeout_timer.cancel()
        try:
            auth.close()
        except Exception:
            pass
        if callable(resolver_close):
            try:
                resolver_close()
            except Exception:
                pass
