"""Authentication diagnostics shared by ChatGPT account queries."""

from __future__ import annotations


def http_error_details(response) -> dict:
    status_code = int(getattr(response, "status_code", 0) or 0)
    details = {"status_code": status_code}
    if status_code != 401:
        details["body"] = str(getattr(response, "text", "") or "")[:400]
        return details

    try:
        payload = response.json()
    except Exception:
        payload = None
    error = payload.get("error") if isinstance(payload, dict) else None
    code = str(error.get("code") or "") if isinstance(error, dict) else ""
    reasons = {
        "token_revoked": "登录凭据已被撤销",
        "token_expired": "登录凭据已过期",
    }
    reason = reasons.get(code, "登录凭据认证失败")
    # Keep diagnostics useful without copying arbitrary response bodies into
    # task results, which can otherwise expose credential material.
    detail = f"HTTP 401，{code}" if code in reasons else "HTTP 401"
    details["error_code"] = code or "authentication_failed"
    details["error"] = f"ChatGPT {reason}（{detail}），请先执行“重新登录”获取新凭据后再查询。"
    return details


def raise_for_status(response) -> None:
    if int(getattr(response, "status_code", 0) or 0) == 401:
        raise RuntimeError(http_error_details(response)["error"])
    response.raise_for_status()


def read_profile_response(response) -> tuple[bool, dict]:
    if response.status_code != 200:
        return False, http_error_details(response)
    profile = response.json()
    if isinstance(profile, dict):
        if str(profile.get("id") or "").startswith("ua-"):
            return False, {
                "status_code": 200,
                "error_code": "anonymous_profile",
                "error": "ChatGPT 返回的是匿名访客资料，未验证账号登录状态，请先执行“重新登录”。",
            }
        if profile.get("id") or profile.get("email"):
            return True, profile
    return False, {
        "status_code": 200,
        "error_code": "invalid_profile",
        "error": "ChatGPT profile 响应缺少用户身份信息",
    }
