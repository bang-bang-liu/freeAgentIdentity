from __future__ import annotations

import base64
import json
import logging
import re
from datetime import datetime
from typing import Optional
from urllib.parse import unquote

from curl_cffi import requests

logger = logging.getLogger(__name__)
WHAM_USAGE_URL = "https://chatgpt.com/backend-api/wham/usage"
WHAM_USAGE_USER_AGENT = "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal"
PLUS_TRIAL_CHECK_URL = "https://chatgpt.com/backend-api/accounts/check/v4-2023-04-27"
PLUS_TRIAL_CHECKOUT_URL = "https://chatgpt.com/backend-api/payments/checkout"
PLUS_TRIAL_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
CHECKOUT_SESSION_RE = re.compile(
    r"(?:cs_(?:live|test)_[A-Za-z0-9_]+|oaics_[A-Za-z0-9_]+)",
    re.I,
)

# The checkout endpoint expects a country/currency pair.  Account region is
# normally a two-letter country code, but older records can contain values
# such as ``us-east-1``; those are normalized before this map is used.
CHECKOUT_CURRENCY_BY_COUNTRY = {
    "US": "USD", "GB": "GBP", "JP": "JPY", "CN": "CNY", "HK": "HKD",
    "TW": "TWD", "KR": "KRW", "IN": "INR", "BR": "BRL", "AU": "AUD",
    "CA": "CAD", "NZ": "NZD", "SG": "SGD", "MY": "MYR", "TH": "THB",
    "ID": "IDR", "PH": "PHP", "VN": "VND", "TR": "TRY", "IL": "ILS",
    "AE": "AED", "SA": "SAR", "QA": "QAR", "KW": "KWD", "BH": "BHD",
    "OM": "OMR", "ZA": "ZAR", "EG": "EGP", "NG": "NGN", "KE": "KES",
    "MX": "MXN", "AR": "ARS", "CL": "CLP", "CO": "COP", "PE": "PEN",
    "UY": "UYU", "PY": "PYG", "BO": "BOB", "CR": "CRC", "DO": "DOP",
    "CH": "CHF", "SE": "SEK", "NO": "NOK", "DK": "DKK", "PL": "PLN",
    "CZ": "CZK", "HU": "HUF", "RO": "RON", "BG": "BGN", "IS": "ISK",
    "RS": "RSD", "UA": "UAH", "GE": "GEL", "KZ": "KZT",
    "DE": "EUR", "FR": "EUR", "IE": "EUR", "NL": "EUR", "ES": "EUR",
    "IT": "EUR", "AT": "EUR", "BE": "EUR", "FI": "EUR", "PT": "EUR",
    "GR": "EUR", "LU": "EUR", "SK": "EUR", "SI": "EUR", "EE": "EUR",
    "LV": "EUR", "LT": "EUR", "CY": "EUR", "MT": "EUR", "HR": "EUR",
}


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
        getattr(account, "account_id", ""),
        extra.get("chatgpt_account_id", ""),
        extra.get("chatgptAccountId", ""),
        extra.get("account_id", ""),
    ):
        if str(value or "").strip():
            return str(value).strip()

    for token in (
        getattr(account, "id_token", "") or extra.get("id_token", ""),
        getattr(account, "access_token", "") or extra.get("access_token", ""),
    ):
        claims = token
        if isinstance(token, str) and token.strip().startswith("{"):
            try:
                claims = json.loads(token)
            except Exception:
                claims = None
        elif isinstance(token, str):
            parts = token.strip().split(".")
            if len(parts) >= 2:
                try:
                    payload = parts[1] + "=" * (-len(parts[1]) % 4)
                    claims = json.loads(base64.urlsafe_b64decode(payload).decode("utf-8"))
                except Exception:
                    claims = None
        if not isinstance(claims, dict):
            continue
        auth_claims = claims.get("https://api.openai.com/auth")
        candidates = [auth_claims, claims] if isinstance(auth_claims, dict) else [claims]
        for candidate in candidates:
            for key in ("chatgpt_account_id", "chatgptAccountId", "account_id"):
                if str(candidate.get(key) or "").strip():
                    return str(candidate[key]).strip()
    return ""


def _plus_trial_account_data(data: dict, account) -> dict:
    """Return the selected account entry from old or current check responses."""
    accounts = data.get("accounts")
    if not isinstance(accounts, (dict, list)):
        return data

    if isinstance(accounts, dict):
        records = [
            (str(key), value)
            for key, value in accounts.items()
            if isinstance(value, dict)
        ]
    else:
        records = [("", value) for value in accounts if isinstance(value, dict)]
    if not records:
        raise ValueError("accounts/check response does not include account data")

    account_id = _account_id(account)
    if account_id:
        for key, record in records:
            nested_account = record.get("account")
            nested_id = nested_account.get("id") if isinstance(nested_account, dict) else ""
            if account_id in (key, str(nested_id or "")):
                return record

    ordering = data.get("account_ordering")
    if isinstance(ordering, list):
        for ordered_id in ordering:
            for key, record in records:
                nested_account = record.get("account")
                nested_id = nested_account.get("id") if isinstance(nested_account, dict) else ""
                if str(ordered_id or "") in (key, str(nested_id or "")):
                    return record
    return records[0][1]


def _checkout_country(account) -> str:
    extra = getattr(account, "extra", {}) or {}
    candidates = (
        getattr(account, "checkout_country", ""),
        extra.get("checkout_country"),
        extra.get("country"),
        getattr(account, "region", ""),
        extra.get("region"),
    )
    for value in candidates:
        normalized = str(value or "").strip().upper()
        if normalized in CHECKOUT_CURRENCY_BY_COUNTRY:
            return normalized
        match = re.match(r"^([A-Z]{2})(?:[-_]|$)", normalized)
        if match and match.group(1) in CHECKOUT_CURRENCY_BY_COUNTRY:
            return match.group(1)
    return "US"


def _extract_checkout_session_id(data) -> str:
    """Extract a cs_* or oaics_* ID from the checkout API response."""

    if isinstance(data, dict):
        for key in (
            "checkout_session_id",
            "checkoutSessionId",
            "stripe_checkout_session_id",
            "stripeCheckoutSessionId",
            "stripe_session_id",
            "stripeSessionId",
            "session_id",
            "id",
            "client_secret",
            "url",
            "checkout_url",
            "checkoutUrl",
        ):
            found = _extract_checkout_session_id(data.get(key))
            if found:
                return found
        for value in data.values():
            found = _extract_checkout_session_id(value)
            if found:
                return found
        return ""
    if isinstance(data, list):
        for value in data:
            found = _extract_checkout_session_id(value)
            if found:
                return found
        return ""
    if not isinstance(data, str):
        return ""

    for candidate in (data.strip(), unquote(data.strip())):
        cleaned = candidate.split("_secret_", 1)[0].strip()
        if CHECKOUT_SESSION_RE.fullmatch(cleaned):
            return cleaned
        match = CHECKOUT_SESSION_RE.search(candidate)
        if match:
            return match.group(0)
    return ""


def _checkout_chain(session_id: str) -> str:
    value = str(session_id or "").strip().lower()
    if value.startswith("oaics_"):
        return "oaics"
    if value.startswith("cs_"):
        return "cs"
    return ""


def fetch_plus_trial_checkout_chain(account, proxy: Optional[str] = None) -> dict:
    """Create one checkout session and return only its chain type.

    ``account.checkout_with_promo`` controls whether the promotional campaign
    is included.  The caller can therefore inspect the checkout chain even
    when the separate promotion-eligibility endpoint says the account is not
    eligible.  This intentionally stops after ChatGPT creates the checkout
    session: it does not initialize Stripe or submit a payment method, and it
    never stores the ephemeral checkout session ID in the account overview.
    """
    access_token = str(getattr(account, "access_token", "") or "").strip()
    if not access_token:
        return {
            "plus_trial_checkout_state": "unavailable",
            "plus_trial_checkout_error": "account access_token is empty",
        }

    country = _checkout_country(account)
    currency = CHECKOUT_CURRENCY_BY_COUNTRY.get(country, "USD")
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
        "Accept": "*/*",
        "Accept-Language": "en-US,en;q=0.9",
        "Origin": "https://chatgpt.com",
        "Referer": "https://chatgpt.com/",
        "User-Agent": PLUS_TRIAL_USER_AGENT,
        "sec-ch-ua": '"Not)A;Brand";v="8", "Chromium";v="124", "Google Chrome";v="124"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
        "sec-fetch-dest": "empty",
        "sec-fetch-mode": "cors",
        "sec-fetch-site": "same-origin",
        "OAI-Language": "en-US",
    }
    account_id = _account_id(account)
    if account_id:
        headers["Chatgpt-Account-Id"] = account_id

    checkout_payload = {
        "entry_point": "all_plans_pricing_modal",
        "plan_name": "chatgptplusplan",
        "billing_details": {"country": country, "currency": currency},
        "checkout_ui_mode": "custom",
        "check_card_proxy": True,
    }
    if getattr(account, "checkout_with_promo", True):
        checkout_payload["promo_campaign"] = {
            "promo_campaign_id": "plus-1-month-free",
            "is_coupon_from_query_param": False,
        }

    request_kwargs = {
        "json": checkout_payload,
        "headers": headers,
        "proxies": _proxies(proxy),
        "timeout": 30,
        "impersonate": "chrome124",
    }
    cookies = _parse_cookie_header(str(getattr(account, "cookies", "") or ""))
    if cookies:
        request_kwargs["cookies"] = cookies

    try:
        response = requests.post(PLUS_TRIAL_CHECKOUT_URL, **request_kwargs)
    except Exception as exc:
        return {
            "plus_trial_checkout_state": "unavailable",
            "plus_trial_checkout_error": f"request_error: {type(exc).__name__}: {exc}",
        }

    status_code = int(getattr(response, "status_code", 0) or 0)
    if status_code != 200:
        return {
            "plus_trial_checkout_state": "unavailable",
            "plus_trial_checkout_error": f"HTTP {status_code}",
        }
    try:
        payload = response.json()
    except Exception:
        payload = None
    session_id = _extract_checkout_session_id(payload)
    if not session_id:
        session_id = _extract_checkout_session_id(str(getattr(response, "text", "") or ""))
    chain = _checkout_chain(session_id)
    if not chain:
        return {
            "plus_trial_checkout_state": "unavailable",
            "plus_trial_checkout_error": "checkout response did not include a supported session ID",
        }
    return {
        "plus_trial_checkout_state": "available",
        "plus_trial_checkout_chain": chain,
    }


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

    account_data = _plus_trial_account_data(data, account)
    nested_account = account_data.get("account")
    if not isinstance(nested_account, dict):
        nested_account = {}
    entitlement = account_data.get("entitlement")
    if not isinstance(entitlement, dict):
        entitlement = {}
    plan_type = str(
        account_data.get("plan_type")
        or nested_account.get("plan_type")
        or ""
    ).strip().lower()
    subscription_plan = str(
        account_data.get("subscription_plan")
        or entitlement.get("subscription_plan")
        or ""
    ).strip().lower()
    is_free = plan_type == "free" or subscription_plan == "chatgptfreeplan"
    eligible_campaigns = account_data.get("eligible_promo_campaigns")
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
