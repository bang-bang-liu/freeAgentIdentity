"""ChatGPT 登录与安全设置的公共结果模型和错误码。

本模块只定义数据结构和错误码，不发起任何网络请求，也不触碰数据库。
刷新登录（login_refresh）与既有的换绑/注册流程共用这里的模型，避免各
自维护一套结构。

安全边界：``ChatGPTLoginResult`` 和 ``SecuritySetupResult`` 里的 token /
cookie / totp_secret 只允许在平台层与持久化层之间的受控内存边界中出现。
禁止把这两个对象整体塞进任务结果、事件日志或前端 JSON —— 对外一律走
``public_dict()`` 的脱敏视图。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------- 错误码

class LoginErrorCode:
    """任务级错误码。与设计文档 9.5 保持一致。"""

    ACCOUNT_NOT_FOUND = "ACCOUNT_NOT_FOUND"
    PLATFORM_UNSUPPORTED = "PLATFORM_UNSUPPORTED"
    MISSING_EMAIL = "MISSING_EMAIL"
    INVALID_PASSWORD = "INVALID_PASSWORD"
    CREDENTIAL_REVOKED = "CREDENTIAL_REVOKED"
    CREDENTIAL_INVALID = "CREDENTIAL_INVALID"
    ANONYMOUS_PROFILE = "ANONYMOUS_PROFILE"
    IDENTITY_MISMATCH = "IDENTITY_MISMATCH"
    API_RESPONSE_INVALID = "API_RESPONSE_INVALID"
    AUTH_SESSION_INVALID = "AUTH_SESSION_INVALID"
    NETWORK_ERROR = "NETWORK_ERROR"
    MFA_REQUIRED = "MFA_REQUIRED"
    MFA_INVALID = "MFA_INVALID"
    EMAIL_OTP_INVALID = "EMAIL_OTP_INVALID"
    MANUAL_OTP_TIMEOUT = "MANUAL_OTP_TIMEOUT"
    SESSION_NOT_RETURNED = "SESSION_NOT_RETURNED"
    TOKEN_FETCH_TIMEOUT = "TOKEN_FETCH_TIMEOUT"
    SECURITY_SETUP_FAILED = "SECURITY_SETUP_FAILED"
    PERSIST_FAILED = "PERSIST_FAILED"
    NETWORK_TIMEOUT = "NETWORK_TIMEOUT"
    RISK_CONTROL = "RISK_CONTROL"
    CONFLICT = "CONFLICT"
    CANCELLED = "CANCELLED"


class PreviewErrorCode:
    """函数层/预检错误码。与设计文档 9.5 上半表保持一致。"""

    INVALID_INPUT = "INVALID_INPUT"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    UNSUPPORTED_PATH = "UNSUPPORTED_PATH"
    RATE_LIMITED = "RATE_LIMITED"


class ChatGPTLoginError(RuntimeError):
    """带错误码的登录失败。``message`` 必须已脱敏。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = str(code or LoginErrorCode.NETWORK_TIMEOUT)
        self.message = str(message or "")


# ------------------------------------------------------------ 登录方式常量

class LoginMethod:
    PASSWORD = "password"
    PASSWORD_TOTP = "password_totp"
    EMAIL_OTP = "email_otp"
    EMAIL_OTP_TOTP = "email_otp_totp"
    EXISTING_SESSION = "existing_session"


class SecuritySetupStatus:
    SKIPPED = "skipped"
    NOT_NEEDED = "not_needed"
    COMPLETED = "completed"
    PARTIAL = "partial"
    FAILED = "failed"


# ---------------------------------------------------------------- 结果模型

@dataclass
class ChatGPTLoginResult:
    """一次登录得到的凭据快照。

    含 secret，禁止整体进入任务结果/日志/前端 JSON，对外用 ``public_dict()``。
    """

    access_token: str = ""
    session_token: str = ""
    refresh_token: str = ""
    id_token: str = ""
    cookie_header: str = ""
    auth_session_json: str = ""
    device_id: str = ""
    account_id: str = ""
    login_method: str = ""
    access_token_expires_at: str = ""
    token_verified: bool = False
    verified_email: str = ""
    verified_user_id: str = ""

    def is_valid(self) -> bool:
        return bool(self.access_token and self.token_verified and self.verified_user_id)

    def credential_updates(self) -> dict[str, str]:
        """返回可直接交给 ``patch_account_graph(credential_updates=...)`` 的字典。

        只包含非空值。空值由调用方保留旧值（见设计 10.2），这里不填充。
        主 token 固定写规范的 snake_case ``access_token``（见设计 10.2.1）。
        """
        candidates = {
            "access_token": self.access_token,
            "session_token": self.session_token,
            "refresh_token": self.refresh_token,
            "id_token": self.id_token,
            "cookies": self.cookie_header,
            "auth_session_json": self.auth_session_json,
            "device_id": self.device_id,
            "account_id": self.account_id,
            "access_token_expires_at": self.access_token_expires_at,
        }
        return {key: value for key, value in candidates.items() if str(value or "").strip()}

    def public_dict(self) -> dict[str, Any]:
        """脱敏视图：只有布尔和时间，没有任何 secret。"""
        return {
            "access_token_present": bool(self.access_token),
            "session_token_present": bool(self.session_token),
            "refresh_token_present": bool(self.refresh_token),
            "login_method": self.login_method,
            "access_token_expires_at": self.access_token_expires_at,
            "account_id_present": bool(self.account_id),
            "token_verified": self.token_verified,
        }


@dataclass
class SecuritySetupResult:
    """补设密码/TOTP 的结果。``totp_secret`` 含 secret，同上。"""

    password_set: bool = False
    password_created: bool = False
    password_persisted: bool = False
    totp_set: bool = False
    totp_created: bool = False
    totp_secret: str = ""
    otpauth: str = ""
    # 本次新建的密码明文。只在平台层与持久化层之间流转，落库后应清空。
    new_password: str = ""
    security_setup_status: str = SecuritySetupStatus.SKIPPED
    error_code: str = ""
    error_message: str = ""

    def public_dict(self) -> dict[str, Any]:
        return {
            "security_setup_status": self.security_setup_status,
            "password_created": self.password_created,
            "password_persisted": self.password_persisted,
            "totp_created": self.totp_created,
            "error_code": self.error_code,
            "error_message": self.error_message,
        }


@dataclass
class AuthCapability:
    """账号当前的登录能力，全部由本地已保存数据推导，不发网络请求。"""

    account_id: int = 0
    email: str = ""
    has_password: bool = False
    has_totp_secret: bool = False
    has_email_otp_source: bool = False
    has_access_token: bool = False
    mailbox_provider: str = ""
    mailbox_blocking_reason: str = ""
    blocking_reasons: list[str] = field(default_factory=list)

    @property
    def ready(self) -> bool:
        """硬阻断只有四种（见设计 4.4），邮箱来源缺失不算。"""
        return not self.blocking_reasons

    @property
    def refresh_mode(self) -> str:
        if self.blocking_reasons:
            return "blocked"
        if self.has_password and self.has_totp_secret:
            return LoginMethod.PASSWORD_TOTP
        if self.has_password:
            return LoginMethod.PASSWORD
        if self.has_totp_secret:
            return LoginMethod.EMAIL_OTP_TOTP
        if self.has_email_otp_source:
            return LoginMethod.EMAIL_OTP
        return "manual_required"
