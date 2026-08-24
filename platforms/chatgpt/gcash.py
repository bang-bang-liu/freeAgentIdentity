"""GCash qualification check for ChatGPT accounts.

The check follows the standalone ``qualification-test`` flow: create a
Philippines/PHP custom Checkout, read the published custom payment methods,
and stop before any payment is confirmed.  A method is considered GCash only
when it carries OpenAI's known GCash custom-payment-method id.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any
from urllib.parse import quote

from curl_cffi import requests

from platforms.chatgpt.fingerprint import FIREFOX_ACCEPT_LANGUAGE, FIREFOX_UA
from platforms.chatgpt.protocol_register import OpenAISentinelClient


GCASH_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
GCASH_CHECKOUT_STATE_URL = "https://chatgpt.com/backend-api/payments/checkout/{processor}/{session_id}"
KNOWN_GCASH_METHOD_IDS = {"cpmt_1TOgstC6h1nxGoI3WUVEY2cJ"}
GCASH_FLOW = "chatgpt_checkout"


class GCashQualificationError(RuntimeError):
    """Raised when the GCash qualification request cannot be completed."""


class GCashCheckContext:
    """Task-scoped Sentinel/Camoufox runtime.

    The browser runtime is expensive to start, while the account checkout
    session must remain isolated per account.  Keep the Sentinel client alive
    for the task and create a fresh HTTP session for each account.
    """

    def __init__(self, proxy: str):
        self.proxy = _normalize_proxy(proxy)
        self.sentinel_http = _session(self.proxy)
        self.sentinel = OpenAISentinelClient(
            self.sentinel_http,
            user_agent=FIREFOX_UA,
            proxy=self.proxy,
            use_browser_runtime=True,
        )

    def build_headers(self, device_id: str) -> dict[str, str]:
        return self.sentinel.build_headers(device_id, GCASH_FLOW)

    def close(self) -> None:
        try:
            self.sentinel.close()
        finally:
            try:
                self.sentinel_http.close()
            except Exception:
                pass


def _parse_cookie_header(cookies: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for part in str(cookies or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip():
            parsed[key.strip()] = value.strip()
    return parsed


def _extract_access_token(value: Any) -> str:
    """Accept a raw JWT or the common ``email----...----JWT`` export form."""
    text = str(value or "").strip()
    if not text:
        return ""
    for part in reversed(text.split("----")):
        candidate = part.strip()
        if re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", candidate):
            return candidate
    match = re.search(r"ey[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", text)
    return match.group(0) if match else text


def _normalize_proxy(value: str) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise GCashQualificationError("GCash 检测需要菲律宾出口代理")
    if "://" not in raw:
        fields = raw.split(":")
        if len(fields) == 4:
            host, port, user, password = fields
            raw = (
                f"http://{quote(user, safe='')}:{quote(password, safe='')}"
                f"@{host}:{port}"
            )
        else:
            raw = f"http://{raw}"
    if not re.match(r"^(https?|socks5h?)://[^\s]+$", raw, re.I):
        raise GCashQualificationError("GCash 检测代理格式无效")
    return raw


def _session(proxy: str):
    http = requests.Session(impersonate="firefox144")
    http.trust_env = False
    http.proxies = {"http": proxy, "https": proxy}
    return http


def _headers(token: str, device_id: str, sentinel: dict[str, str], account_id: str = "") -> dict[str, str]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": FIREFOX_ACCEPT_LANGUAGE,
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": FIREFOX_UA,
        "OAI-Language": "en-US",
        "OAI-Device-Id": device_id,
    }
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    headers.update(sentinel or {})
    return headers


def _extract_session_id(value: Any) -> str:
    if isinstance(value, dict):
        for item in value.values():
            found = _extract_session_id(item)
            if found:
                return found
        return ""
    if isinstance(value, list):
        for item in value:
            found = _extract_session_id(item)
            if found:
                return found
        return ""
    if not isinstance(value, str):
        return ""
    match = re.search(r"(?:oaics_[A-Za-z0-9_]+|cs_(?:live|test)_[A-Za-z0-9_]+)", value)
    return match.group(0) if match else ""


def _gcash_method(methods: Any) -> str:
    if not isinstance(methods, list):
        return ""
    for method in methods:
        if not isinstance(method, dict):
            continue
        method_id = str(method.get("id") or "")
        if method_id in KNOWN_GCASH_METHOD_IDS:
            return method_id
    return ""


def _channel_details(methods: Any) -> list[dict[str, Any]]:
    if not isinstance(methods, list):
        return []
    details: list[dict[str, Any]] = []
    for method in methods:
        if isinstance(method, dict):
            method_id = str(method.get("id") or "")
            name = "gcash" if method_id in KNOWN_GCASH_METHOD_IDS else str(
                method.get("type")
                or method.get("name")
                or method.get("display_name")
                or method.get("payment_method_type")
                or method_id
                or ""
            )
            if name:
                details.append({"name": name, "id": method_id, "raw_type": method.get("type") or ""})
        elif isinstance(method, str) and method.strip():
            details.append({"name": method.strip(), "id": method.strip(), "raw_type": ""})
    return details


def check_gcash(
    account: Any,
    proxy: str | None,
    *,
    context: GCashCheckContext | None = None,
) -> dict[str, Any]:
    """Return GCash qualification details for one ChatGPT account.

    ``proxy`` is intentionally required.  The reference implementation uses
    a Philippine egress for both Checkout creation and payment-method
    publication; a direct connection cannot establish a reliable result.
    """
    token = _extract_access_token(
        getattr(account, "access_token", "") or getattr(account, "token", "")
    )
    if not token:
        raise GCashQualificationError("缺少 Access Token")
    normalized_proxy = _normalize_proxy(proxy or "")
    cookies = _parse_cookie_header(str(getattr(account, "cookies", "") or ""))
    device_id = str(uuid.uuid4())
    owns_context = context is None
    context = context or GCashCheckContext(normalized_proxy)
    http = _session(normalized_proxy)
    if cookies:
        http.cookies.update(cookies)
    try:
        try:
            http.cookies.set("oai-did", device_id, domain="chatgpt.com")
            http.get(
                "https://chatgpt.com/api/auth/csrf",
                headers={"User-Agent": FIREFOX_UA},
                timeout=15,
            )
        except Exception:
            pass

        try:
            sentinel_headers = context.build_headers(device_id)
            payload = {
                "entry_point": "all_plans_pricing_modal",
                "plan_name": "chatgptplusplan",
                "billing_details": {"country": "PH", "currency": "PHP"},
                "cancel_url": "https://chatgpt.com/",
                "checkout_ui_mode": "custom",
                "check_card_proxy": True,
            }
            response = http.post(
                GCASH_CHECKOUT_URL,
                json=payload,
                headers=_headers(
                    token,
                    device_id,
                    sentinel_headers,
                    str(getattr(account, "account_id", "") or "").strip(),
                ),
                timeout=45,
            )
            if int(getattr(response, "status_code", 0) or 0) != 200:
                raise GCashQualificationError(
                    f"Checkout HTTP {int(getattr(response, 'status_code', 0) or 0)}"
                )
            try:
                checkout = response.json() or {}
            except Exception as exc:
                raise GCashQualificationError("Checkout 返回非 JSON") from exc
            session_id = _extract_session_id(checkout) or _extract_session_id(str(getattr(response, "text", "") or ""))
            if not session_id:
                raise GCashQualificationError("Checkout 未返回有效会话")
            processor = str(checkout.get("processor_entity") or "openai_ie")

            state: dict[str, Any] = {}
            for attempt in range(4):
                state_response = http.get(
                    GCASH_CHECKOUT_STATE_URL.format(processor=processor, session_id=session_id),
                    headers=_headers(
                        token,
                        device_id,
                        {},
                        str(getattr(account, "account_id", "") or "").strip(),
                    ),
                    timeout=30,
                )
                if int(getattr(state_response, "status_code", 0) or 0) != 200:
                    raise GCashQualificationError(
                        f"读取 Checkout HTTP {int(getattr(state_response, 'status_code', 0) or 0)}"
                    )
                state = state_response.json() or {}
                if _gcash_method(state.get("custom_payment_methods")):
                    break
                if attempt < 3:
                    time.sleep(0.8)

            methods = state.get("custom_payment_methods")
            method_id = _gcash_method(methods)
            checkout_chain = "oaics" if str(session_id).lower().startswith("oaics_") else (
                "cs" if str(session_id).lower().startswith("cs_") else None
            )
            return {
                "gcash_qualified": bool(method_id),
                "gcash_eligible": bool(method_id),
                "gcash_available": bool(method_id),
                "gcash_check_state": "available",
                "gcash_country": "PH",
                "gcash_currency": "PHP",
                "gcash_method_id": method_id or None,
                "gcash_evidence": "gcash channel published" if method_id else "PH checkout 未发布 gcash",
                "gcash_channel_details": _channel_details(methods),
                "gcash_error": None,
                "plus_trial_checkout_chain": checkout_chain,
                "plus_trial_checkout_state": "available" if checkout_chain else "unavailable",
                "plus_trial_checkout_error": None if checkout_chain else "Checkout session chain unavailable",
            }
        finally:
            pass
    finally:
        try:
            http.close()
        except Exception:
            pass
        if owns_context:
            context.close()
