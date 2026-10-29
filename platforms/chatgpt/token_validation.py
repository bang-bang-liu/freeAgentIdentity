"""Verify a ChatGPT bearer credential independently from session cookies."""
from __future__ import annotations

import weakref
import re
from typing import Callable

from platforms.chatgpt.login_types import ChatGPTLoginError, LoginErrorCode
from registration_core.http_client import create_http_session


def network_login_error(exc: Exception, stage: str) -> ChatGPTLoginError:
    text = str(exc).lower()
    status = re.search(r"(?:http(?: error)?\s*|(?:失败|status)\s*[:=：]?\s*)([345]\d\d)\b", text)
    http_status = getattr(exc, "status_code", None) or (int(status[1]) if status else None)
    error_code = str(getattr(exc, "error_code", "") or "")
    request_id = str(getattr(exc, "request_id", "") or "")
    suffix = f"；请求 ID={request_id}" if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", request_id) else ""
    if http_status in (400, 409) and (error_code == "invalid_state" or "invalid_state" in text):
        return ChatGPTLoginError(
            LoginErrorCode.AUTH_SESSION_INVALID,
            f"{stage}失败：HTTP {http_status}，登录会话已失效（invalid_state），请重新发起登录{suffix}",
        )
    if error_code == "cloudflare_challenge":
        return ChatGPTLoginError(
            LoginErrorCode.RISK_CONTROL,
            f"{stage}被 Cloudflare 安全验证拦截：HTTP {http_status}；请检查代理出口，确认 auth.openai.com 可正常访问后重试{suffix}",
        )
    if http_status:
        reason = f"（{error_code}）" if re.fullmatch(r"[A-Za-z0-9_.:-]{1,80}", error_code) else ""
        return ChatGPTLoginError(LoginErrorCode.API_RESPONSE_INVALID, f"{stage}接口响应异常：HTTP {http_status}{reason}{suffix}")
    code = (LoginErrorCode.NETWORK_TIMEOUT if "timeout" in text or "timed out" in text
            or "timeout" in type(exc).__name__.lower() else LoginErrorCode.NETWORK_ERROR)
    return ChatGPTLoginError(code, f"{stage}失败：{type(exc).__name__}")


def validate_chatgpt_token(
    access_token: str,
    *,
    expected_email: str,
    proxy: str | None = None,
    timeout: float = 20,
    cancel_check: Callable[[], bool] | None = None,
    register_cancel_callback: Callable | None = None,
    browser_profile=None,
    challenge_options: dict | None = None,
) -> dict:
    """Only an authenticated /me profile for the target email counts as success.

    A fresh HTTP session starts without login cookies: a valid cookie must
    never hide a rejected Bearer Token. Challenge handling may establish its
    own clearance cookies and always replays with the supplied Bearer Token.
    Trial/subscription calls belong to the independent query flow.
    """
    def check_cancelled():
        if cancel_check and cancel_check():
            raise ChatGPTLoginError(LoginErrorCode.CANCELLED, "任务已取消")

    check_cancelled()
    if not access_token:
        raise ChatGPTLoginError(LoginErrorCode.SESSION_NOT_RETURNED, "session 未返回 access_token")
    session_options = {"proxy": proxy}
    if browser_profile is not None:
        session_options["profile"] = browser_profile
    session = create_http_session(**session_options)
    if challenge_options and browser_profile is not None:
        from registration_core.login_transport import LoginChallengeSession
        session = LoginChallengeSession(session, browser_profile, **challenge_options)
    try:
        if callable(register_cancel_callback):
            session_ref = weakref.ref(session)

            def close_on_cancel():
                current = session_ref()
                if current is not None:
                    current.close()

            register_cancel_callback(close_on_cancel)
        try:
            response = session.get(
                "https://chatgpt.com/backend-api/me",
                headers={"Accept": "application/json", "Authorization": f"Bearer {access_token}"},
                timeout=max(0.1, min(float(timeout), 20)),
                allow_redirects=False,
            )
        except Exception as exc:
            check_cancelled()
            raise network_login_error(exc, "Token 业务验证") from exc
        check_cancelled()
        try:
            payload = response.json()
        except Exception:
            payload = None
        status = response.status_code
        if status == 401:
            error = payload.get("error") if isinstance(payload, dict) else None
            code = str(error.get("code") or "") if isinstance(error, dict) else ""
            if code == "token_revoked":
                raise ChatGPTLoginError(LoginErrorCode.CREDENTIAL_REVOKED, "业务接口拒绝 Token：凭据已撤销（token_revoked）")
            raise ChatGPTLoginError(LoginErrorCode.CREDENTIAL_INVALID, f"业务接口拒绝 Token：HTTP 401（{code or 'authentication_failed'}）")
        if status != 200:
            raise ChatGPTLoginError(LoginErrorCode.API_RESPONSE_INVALID, f"Token 验证接口响应异常：HTTP {status}")
        if not isinstance(payload, dict):
            raise ChatGPTLoginError(LoginErrorCode.API_RESPONSE_INVALID, "Token 验证接口未返回 JSON 对象")
        user_id = str(payload.get("id") or "").strip()
        email = str(payload.get("email") or "").strip()
        if user_id.startswith("ua-"):
            raise ChatGPTLoginError(LoginErrorCode.ANONYMOUS_PROFILE, "业务接口返回匿名访客资料，登录未验证")
        if not user_id or not email:
            raise ChatGPTLoginError(LoginErrorCode.API_RESPONSE_INVALID, "业务接口缺少用户 ID 或邮箱，无法验证账号身份")
        if not expected_email or email.casefold() != expected_email.strip().casefold():
            raise ChatGPTLoginError(LoginErrorCode.IDENTITY_MISMATCH, f"登录身份与目标账号不一致：返回 {email}")
        return payload
    finally:
        try:
            session.close()
        except Exception:
            pass
