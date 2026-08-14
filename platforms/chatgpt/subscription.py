from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional

from curl_cffi import requests

logger = logging.getLogger(__name__)
WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
WHAM_USAGE_USER_AGENT = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"
PLUS_TRIAL_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
PLUS_TRIAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def _proxies(proxy: Optional[str]) -> Optional[dict]:
    return {"http": proxy, "https": proxy} if proxy else None


def _parse_cookie_header(cookies: str) -> dict[str, str]:
    parsed: dict[str, str] = {}
    for part in (cookies or "").split(";"):
        part = part.strip()
        if not part or "=" not in part:
            continue
        name, value = part.split("=", 1)
        name = name.strip()
        if name:
            parsed[name] = value.strip()
    return parsed


def _timezone_offset_min() -> str:
    """Return the browser-compatible ``Date#getTimezoneOffset`` value."""
    offset = datetime.now().astimezone().utcoffset()
    if offset is None:
        return "0"
    return str(-int(offset.total_seconds() // 60))


def _account_id(account) -> str:
    extra = getattr(account, "extra", {}) or {}
    for value in (
        getattr(account, "chatgpt_account_id", ""),
        extra.get("chatgpt_account_id", ""),
        extra.get("chatgptAccountId", ""),
    ):
        if str(value or "").strip():
            return str(value).strip()
    id_token = getattr(account, "id_token", "") or extra.get("id_token", "")
    if isinstance(id_token, str) and id_token.strip().startswith("{"):
        try:
            id_token = json.loads(id_token)
        except Exception:
            id_token = None
    if isinstance(id_token, dict):
        for key in ("chatgpt_account_id", "chatgptAccountId", "account_id"):
            if str(id_token.get(key) or "").strip():
                return str(id_token[key]).strip()
    return ""


def _plan(value: str) -> str:
    raw = str(value or "").strip().lower()
    if any(token in raw for token in ("team", "enterprise", "business")):
        return "team"
    if any(token in raw for token in ("plus", "pro", "premium", "paid")):
        return "plus"
    return "free"


def _status_from_me(data: dict) -> str:
    status = _plan(data.get("plan_type"))
    if status != "free":
        return status
    for org in data.get("orgs", {}).get("data", []):
        status = _plan(org.get("settings", {}).get("workspace_plan_type"))
        if status != "free":
            return status
    return "free"


def _usage(account, proxy: Optional[str]) -> dict:
    headers = {"Authorization": f"Bearer {account.access_token}", "User-Agent": WHAM_USAGE_USER_AGENT}
    account_id = _account_id(account)
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id
    response = requests.get(
        WHAM_USAGE_URL,
        headers=headers,
        proxies=_proxies(proxy),
        timeout=20,
        impersonate="chrome124",
    )
    response.raise_for_status()
    data = response.json()
    if not isinstance(data, dict):
        raise ValueError("wham/usage response format is invalid")
    return data


def fetch_plus_trial_eligibility(account, proxy: Optional[str] = None) -> dict:
    """Query ChatGPT's internal promotion endpoint for Plus trial eligibility.

    This endpoint is an enrichment of the normal account check.  Callers can
    distinguish a definitive ``False`` from an unavailable check by looking at
    ``plus_trial_eligible`` and ``check_state`` respectively.
    """
    access_token = str(getattr(account, "access_token", "") or "").strip()
    if not access_token:
        raise ValueError("account access_token is empty")

    cookie_map = _parse_cookie_header(str(getattr(account, "cookies", "") or ""))
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Accept": "application/json, text/plain, */*",
        "User-Agent": PLUS_TRIAL_USER_AGENT,
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
    }
    if cookie_map.get("oai-did"):
        headers["oai-device-id"] = cookie_map["oai-did"]

    # The value in copied examples is often shown as a bare '-'.  The web
    # client sends the actual Date#getTimezoneOffset value, so try that first.
    # Some deployments accept the endpoint without the optional query at all.
    request_variants: list[tuple[dict[str, str], bool]] = [
        ({"timezone_offset_min": _timezone_offset_min()}, False),
        ({}, False),
    ]
    if cookie_map:
        request_variants.extend([
            ({"timezone_offset_min": _timezone_offset_min()}, True),
            ({}, True),
        ])
    # Retain compatibility with the literal URL supplied by older clients.
    request_variants.append(({"timezone_offset_min": "-"}, bool(cookie_map)))

    last_response = None
    for params, with_cookies in request_variants:
        request_kwargs = {
            "params": params,
            "headers": headers,
            "proxies": _proxies(proxy),
            "timeout": 20,
            "impersonate": "chrome124",
        }
        if with_cookies:
            request_kwargs["cookies"] = cookie_map
        response = requests.get(PLUS_TRIAL_CHECK_URL, **request_kwargs)
        if int(getattr(response, "status_code", 0) or 0) == 403:
            last_response = response
            continue
        response.raise_for_status()
        data = response.json()
        if not isinstance(data, dict):
            raise ValueError("accounts/check response format is invalid")
        break
    else:
        if last_response is not None:
            last_response.raise_for_status()
        raise RuntimeError("accounts/check request did not return a response")

    plan_type = str(data.get("plan_type") or "").strip().lower()
    subscription_plan = str(data.get("subscription_plan") or "").strip().lower()
    is_free = plan_type == "free" or subscription_plan == "chatgptfreeplan"
    eligible_campaigns = data.get("eligible_promo_campaigns")
    if not isinstance(eligible_campaigns, dict):
        eligible_campaigns = {}

    return {
        "plus_trial_eligible": bool(is_free and eligible_campaigns.get("plus")),
        "plus_trial_check_state": "available",
        "plus_trial_plan_type": plan_type or None,
        "plus_trial_subscription_plan": subscription_plan or None,
        "plus_trial_raw": data,
    }


def _attach_plus_trial_eligibility(details: dict, account, proxy: Optional[str]) -> dict:
    """Add Plus trial data without allowing enrichment failures to fail checks."""
    try:
        details.update(fetch_plus_trial_eligibility(account, proxy=proxy))
    except Exception as exc:
        logger.info("Plus trial eligibility check unavailable: %s", exc)
        details.update({
            "plus_trial_eligible": None,
            "plus_trial_check_state": "unavailable",
            "plus_trial_error": str(exc),
        })
    return details


def fetch_subscription_status_details(account, proxy: Optional[str] = None) -> dict:
    if not account.access_token:
        raise ValueError("account access_token is empty")
    try:
        response = requests.get(
            "https://chatgpt.com/backend-api/me",
            headers={"Authorization": f"Bearer {account.access_token}", "Content-Type": "application/json"},
            proxies=_proxies(proxy),
            timeout=20,
            impersonate="chrome110",
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, dict):
            usage = None
            try:
                usage = _usage(account, proxy)
            except Exception as exc:
                logger.info("usage enrichment failed: %s", exc)
            return _attach_plus_trial_eligibility(
                {"status": _status_from_me(data), "source": "backend-api/me", "me": data, "usage": usage},
                account,
                proxy,
            )
    except Exception as exc:
        logger.info("subscription status fallback to usage: %s", exc)
    usage = _usage(account, proxy)
    return _attach_plus_trial_eligibility(
        {
            "status": _plan(usage.get("plan_type")),
            "source": "backend-api/wham/usage",
            "me": None,
            "usage": usage,
        },
        account,
        proxy,
    )


def check_subscription_status(account, proxy: Optional[str] = None) -> str:
    return fetch_subscription_status_details(account, proxy=proxy)["status"]
