"""
ChatGPT / Codex 本地桌面端切号与状态查询。

当前实现面向本机 Electron 客户端 `Codex`，通过写入其 Chromium Cookies 数据库
完成 best-effort 本地登录态切换。
"""

from __future__ import annotations

import logging
import os
import platform
import sqlite3
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Optional

from curl_cffi import requests as curl_requests

logger = logging.getLogger(__name__)


def _build_proxies(proxy: Optional[str]) -> dict | None:
    if not proxy:
        return None
    return {"http": proxy, "https": proxy}


def _mask_secret(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 12:
        return value
    return f"{value[:6]}...{value[-4:]}"


def _chromium_utc(dt: datetime) -> int:
    chromium_epoch = datetime(1601, 1, 1, tzinfo=timezone.utc)
    delta = dt.astimezone(timezone.utc) - chromium_epoch
    return int(delta.total_seconds() * 1_000_000)


def _cookie_targets(name: str) -> list[tuple[str, int]]:
    if name == "__Secure-next-auth.session-token":
        return [
            (".chatgpt.com", 1),
            ("chatgpt.com", 1),
            (".chat.openai.com", 1),
            ("chat.openai.com", 1),
        ]
    return [
        (".chatgpt.com", 0),
        ("chatgpt.com", 0),
    ]


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


def _get_codex_support_dir() -> str:
    system = platform.system()
    home = os.path.expanduser("~")
    if system == "Darwin":
        return os.path.join(home, "Library", "Application Support", "Codex")
    if system == "Windows":
        appdata = os.environ.get("APPDATA", "")
        return os.path.join(appdata, "Codex")
    config_home = os.environ.get("XDG_CONFIG_HOME", os.path.join(home, ".config"))
    return os.path.join(config_home, "Codex")


def _get_codex_cookies_path() -> str:
    return os.path.join(_get_codex_support_dir(), "Cookies")


def close_codex_app() -> tuple[bool, str]:
    system = platform.system()
    try:
        if system == "Darwin":
            subprocess.run(["osascript", "-e", 'quit app "Codex"'], capture_output=True, timeout=5)
            time.sleep(1.5)
            return True, "已尝试关闭 Codex"
        if system == "Windows":
            subprocess.run(
                ["taskkill", "/IM", "Codex.exe", "/F"],
                capture_output=True,
                creationflags=0x08000000,
                timeout=5,
            )
            time.sleep(1.5)
            return True, "已尝试关闭 Codex"
        subprocess.run(["pkill", "-f", "codex"], capture_output=True, timeout=5)
        time.sleep(1.5)
        return True, "已尝试关闭 Codex"
    except Exception as exc:
        logger.warning("关闭 Codex 失败: %s", exc)
        return False, f"关闭 Codex 失败: {exc}"


def restart_codex_app() -> tuple[bool, str]:
    system = platform.system()
    try:
        if system == "Darwin":
            if os.path.exists("/Applications/Codex.app"):
                subprocess.Popen(["open", "-a", "Codex"])
                return True, "Codex 已重启"
            return True, "未找到 /Applications/Codex.app，请手动启动 Codex"
        if system == "Windows":
            localappdata = os.environ.get("LOCALAPPDATA", "")
            for exe in (
                os.path.join(localappdata, "Programs", "Codex", "Codex.exe"),
                os.path.join(localappdata, "Codex", "Codex.exe"),
            ):
                if os.path.exists(exe):
                    subprocess.Popen([exe])
                    return True, "Codex 已重启"
            return True, "未找到 Codex.exe，请手动启动 Codex"
        for binary in ("/usr/bin/codex", os.path.expanduser("~/.local/bin/codex")):
            if os.path.exists(binary):
                subprocess.Popen([binary])
                return True, "Codex 已重启"
        subprocess.Popen(["codex"])
        return True, "Codex 已重启"
    except Exception as exc:
        logger.warning("启动 Codex 失败: %s", exc)
        return False, f"启动 Codex 失败: {exc}"


def switch_codex_account(session_token: str = "", cookies: str = "") -> tuple[bool, dict]:
    resolved_session = extract_session_token(session_token, cookies)
    if not resolved_session:
        return False, {"error": "缺少 __Secure-next-auth.session-token，无法切换本地 Codex 桌面端账号"}

    cookies_path = _get_codex_cookies_path()
    if not os.path.exists(cookies_path):
        return False, {"error": f"未找到 Codex Cookies 数据库: {cookies_path}"}

    cookie_map = _parse_cookie_header(cookies)
    cookie_map["__Secure-next-auth.session-token"] = resolved_session

    now = datetime.now(timezone.utc)
    creation_utc = _chromium_utc(now)
    expires_utc = _chromium_utc(now + timedelta(days=30))

    try:
        conn = sqlite3.connect(cookies_path, timeout=10)
        try:
            cursor = conn.cursor()
            for name, value in cookie_map.items():
                if not value:
                    continue
                for host_key, http_only in _cookie_targets(name):
                    cursor.execute(
                        """
                        DELETE FROM cookies
                        WHERE host_key = ? AND name = ? AND path = '/'
                        """,
                        (host_key, name),
                    )
                    cursor.execute(
                        """
                        INSERT INTO cookies (
                            creation_utc, host_key, top_frame_site_key, name, value, encrypted_value,
                            path, expires_utc, is_secure, is_httponly, last_access_utc, has_expires,
                            is_persistent, priority, samesite, source_scheme, source_port,
                            last_update_utc, source_type, has_cross_site_ancestor
                        ) VALUES (?, ?, '', ?, ?, ?, '/', ?, 1, ?, ?, 1, 1, 1, 0, 2, 443, ?, 1, 1)
                        """,
                        (
                            creation_utc,
                            host_key,
                            name,
                            value,
                            b"",
                            expires_utc,
                            http_only,
                            creation_utc,
                            creation_utc,
                        ),
                    )
            conn.commit()
        finally:
            conn.close()
    except Exception as exc:
        logger.error("写入 Codex Cookies 失败: %s", exc)
        return False, {"error": f"写入 Codex Cookies 失败: {exc}"}

    return True, {
        "message": "已写入 Codex 本地 Cookies，准备重启桌面端",
        "cookies_path": cookies_path,
        "cookie_names": sorted(cookie_map.keys()),
        "session_token_preview": _mask_secret(resolved_session),
    }


def read_current_codex_account() -> dict:
    cookies_path = _get_codex_cookies_path()
    if not os.path.exists(cookies_path):
        return {"available": False, "cookies_path": cookies_path}

    try:
        conn = sqlite3.connect(cookies_path, timeout=10)
        try:
            cursor = conn.cursor()
            rows = cursor.execute(
                """
                SELECT host_key, name, value
                FROM cookies
                WHERE name IN ('__Secure-next-auth.session-token', 'oai-did')
                ORDER BY host_key, name
                """
            ).fetchall()
        finally:
            conn.close()
    except Exception as exc:
        logger.warning("读取 Codex Cookies 失败: %s", exc)
        return {
            "available": True,
            "cookies_path": cookies_path,
            "error": str(exc),
        }

    session_token = ""
    cookies_found = []
    for host_key, name, value in rows:
        if name == "__Secure-next-auth.session-token" and value and not session_token:
            session_token = value
        cookies_found.append({
            "host": host_key,
            "name": name,
            "value_preview": _mask_secret(value),
        })
    return {
        "available": True,
        "cookies_path": cookies_path,
        "session_token_present": bool(session_token),
        "session_token_preview": _mask_secret(session_token),
        "cookies": cookies_found,
    }


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
        "desktop_app": "Codex",
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
