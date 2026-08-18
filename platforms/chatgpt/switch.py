"""ChatGPT account state queries."""

from __future__ import annotations

import logging
from typing import Optional

from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)


def _build_proxies(proxy: Optional[str]) -> dict | None:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _parse_cookie_header(cookies: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for part in (cookies or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if not name:
            continue
        parsed[name] = value.strip()
    return parsed


def extract_session_token(session_token: str = "", cookies: str = "") -> str:
    token = (session_token or "").strip()
    if token:
        return token
    cookie_map = _parse_cookie_header(cookies)
    token = cookie_map.get("__Secure-next-auth.session-token", "")
    if token:
        return token

    # NextAuth stores long session values in numbered cookies.  Keep their
    # numeric order when reconstructing the value from an exported header.
    chunks: list[tuple[int, str]] = []
    prefix = "__Secure-next-auth.session-token."
    for name, value in cookie_map.items():
        if not name.startswith(prefix) or not value:
            continue
        suffix = name[len(prefix):]
        if suffix.isdigit():
            chunks.append((int(suffix), value))
    return "".join(value for _, value in sorted(chunks))


def _fetch_profile(
    access_token: str = "",
    cookies: str = "",
    proxy: str | None = None,
) -> tuple[bool, dict, str]:
    """Fetch /backend-api/me with one authentication method per request."""
    url = "https://chatgpt.com/backend-api/me"
    headers = {
        "accept": "application/json",
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
    }
    session_cookies = _parse_cookie_header(cookies)
    last_error: dict = {}

    # A current browser session is the native authentication mechanism for
    # this endpoint. Do not mix it with a Bearer token in the same request.
    if session_cookies:
        try:
            response = curl_requests.get(
                url,
                headers=headers,
                cookies=session_cookies,
                proxies=_build_proxies(proxy),
                timeout=20,
                impersonate="chrome124",
            )
            if response.status_code == 200:
                return True, response.json(), "cookies"
            last_error = {"status_code": response.status_code, "body": response.text[:400]}
        except Exception as exc:
            last_error = {"error": str(exc)}

    if access_token:
        try:
            response = curl_requests.get(
                url,
                headers={**headers, "authorization": f"Bearer {access_token}"},
                proxies=_build_proxies(proxy),
                timeout=20,
                impersonate="chrome124",
            )
            if response.status_code == 200:
                return True, response.json(), "access_token"
            last_error = {"status_code": response.status_code, "body": response.text[:400]}
        except Exception as exc:
            last_error = {"error": str(exc)}

    return False, last_error, ""


def fetch_chatgpt_account_state(
    *,
    access_token: str = "",
    session_token: str = "",
    cookies: str = "",
    proxy: str | None = None,
) -> dict:
    state = {
        "platform": "chatgpt",
        "session_token_present": bool(extract_session_token(session_token, cookies)),
        "quota_note": "ChatGPT 未公开稳定的剩余额度接口，当前返回订阅状态和账号 profile 信息。",
    }

    resolved_session = extract_session_token(session_token, cookies)
    resolved_access = access_token

    if resolved_access or cookies:
        ok, profile, profile_auth_method = _fetch_profile(
            access_token=resolved_access,
            cookies=cookies,
            proxy=proxy,
        )
        state["valid"] = ok
        if ok:
            state["profile"] = profile
            state["profile_auth_method"] = profile_auth_method
            try:
                from platforms.chatgpt.subscription import fetch_subscription_status_details

                class _A:
                    pass

                account = _A()
                account.access_token = resolved_access
                account.cookies = cookies
                # The subscription and Plus-promotion endpoints require the
                # saved bearer token.  A cookie-authenticated profile can
                # still be valid, so keep that result independent from this
                # optional enrichment.
                if resolved_access:
                    details = fetch_subscription_status_details(account, proxy=proxy)
                    state["subscription_status"] = details.get("status")
                    if "plus_trial_eligible" in details:
                        state["plus_trial_eligible"] = details.get("plus_trial_eligible")
                    if details.get("plus_trial_check_state"):
                        state["plus_trial_check_state"] = details.get("plus_trial_check_state")
                    if details.get("plus_trial_error"):
                        state["plus_trial_error"] = details.get("plus_trial_error")
            except Exception as exc:
                state["subscription_error"] = str(exc)
                state["plus_trial_eligible"] = None
                state["plus_trial_check_state"] = "unavailable"
                state["plus_trial_error"] = str(exc)
        else:
            state["profile_error"] = profile
            state["plus_trial_eligible"] = None
            state["plus_trial_check_state"] = "unavailable"
            state["plus_trial_error"] = "账号 profile 查询失败，无法查询 Plus 试用资格"
    else:
        state["valid"] = False
        state["profile_error"] = "缺少 access_token，且无法通过 session_token 刷新"
        state["plus_trial_eligible"] = None
        state["plus_trial_check_state"] = "unavailable"
        state["plus_trial_error"] = "缺少 access_token，无法查询 Plus 试用资格"

    return state
