"""Final credential checks shared by browser and protocol registration."""
from __future__ import annotations

from datetime import datetime, timezone

from platforms.chatgpt.login_client import login_chatgpt_web, parse_access_token_expiry
from platforms.chatgpt.login_types import ChatGPTLoginError, LoginErrorCode
from platforms.chatgpt.token_validation import validate_chatgpt_token


class PartialSecuritySetupError(RuntimeError):
    def __init__(self, error: Exception, partial_result: dict):
        super().__init__(str(error))
        self.partial_result = partial_result


class _RegistrationOtpResolver:
    """Use registration's existing mailbox/manual OTP adapter for one recovery."""
    timeout = 180

    def __init__(self, callback):
        self.callback = callback

    def baseline(self):
        return set()

    def describe(self):
        return "注册任务取码通道"

    def fetch(self, **_kwargs):
        if not callable(self.callback):
            raise ChatGPTLoginError(LoginErrorCode.EMAIL_OTP_INVALID, "没有可用的邮箱验证码通道")
        try:
            return self.callback(timeout_override=self.timeout)
        except TypeError as exc:
            if "timeout_override" not in str(exc):
                raise
            return self.callback()

    def close(self):
        pass  # The registration task owns this adapter.


def finalize_registered_credentials(
    result: dict, *, proxy=None, otp_callback=None, log=None,
    cancel_check=None, register_cancel_callback=None,
) -> dict:
    """Retain the created account even when final credential verification fails.

    An explicit rejected credential permits exactly one fresh login. Network,
    identity and unexpected API errors never trigger speculative reauth loops.
    """
    emit = log or (lambda _message: None)
    result = dict(result)
    now = datetime.now(timezone.utc).isoformat()
    recovered = False
    try:
        emit("验证注册后的 Bearer Token 与账号身份")
        try:
            profile = validate_chatgpt_token(
                result.get("access_token", ""), expected_email=result.get("email", ""),
                proxy=proxy, cancel_check=cancel_check,
                register_cancel_callback=register_cancel_callback,
            )
        except ChatGPTLoginError as exc:
            if exc.code not in {LoginErrorCode.CREDENTIAL_REVOKED, LoginErrorCode.CREDENTIAL_INVALID}:
                raise
            emit("最终凭据已失效，使用统一登录客户端重新认证一次")
            recovered = True
            login = login_chatgpt_web(
                email=result.get("email", ""), password=result.get("password", ""),
                totp_secret=result.get("totp_secret", ""), proxy=proxy,
                otp_resolver=_RegistrationOtpResolver(otp_callback),
                log=emit, cancel_check=cancel_check,
                register_cancel_callback=register_cancel_callback,
            )
            if not login.is_valid():
                raise ChatGPTLoginError(LoginErrorCode.CREDENTIAL_INVALID, "恢复登录未返回已验证的凭据")
            result.update(login.credential_updates())
            result["cookies"] = login.cookie_header
            result["expires_at"] = login.access_token_expires_at
            profile = {"id": login.verified_user_id, "email": login.verified_email}
        result["token_verified"] = True
        result["login_required"] = False
        result["profile"] = profile
        result["expires_at"] = parse_access_token_expiry(result.get("access_token", "")) or result.get("expires_at", "")
        result["account_overview"] = {
            "valid": True, "check_state": "valid", "check_error": "", "login_required": False,
            "auth": {"last_login_status": "succeeded", "last_login_error_code": "",
                     "last_login_error": "", "token_verified_at": now,
                     "access_token_expires_at": result["expires_at"], "recovery_attempted": recovered},
        }
        emit("注册后的 Token 已被业务接口接受")
    except Exception as exc:
        code = exc.code if isinstance(exc, ChatGPTLoginError) else LoginErrorCode.API_RESPONSE_INVALID
        message = exc.message if isinstance(exc, ChatGPTLoginError) else f"最终凭据验证异常：{type(exc).__name__}"
        result.update(token_verified=False, login_required=True, credential_error_code=code, credential_error=message)
        result["account_overview"] = {
            "valid": None, "check_state": "credential_invalid" if code in {
                LoginErrorCode.CREDENTIAL_REVOKED, LoginErrorCode.CREDENTIAL_INVALID, LoginErrorCode.ANONYMOUS_PROFILE,
            } else "unavailable", "check_error": message, "login_required": True,
            "auth": {"last_login_status": "cancelled" if code == LoginErrorCode.CANCELLED else "failed",
                     "last_login_error_code": code, "last_login_error": message,
                     "recovery_attempted": recovered},
        }
        emit(f"账号已注册并保留密码和安全资料；需要重新登录：{message}")
    return result
