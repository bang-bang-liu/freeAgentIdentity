"""Kiro token refresh and remote portal account-state APIs."""

import logging
import re
from typing import Tuple
from datetime import datetime, timezone

import cbor2
from curl_cffi import requests as cffi_requests

logger = logging.getLogger(__name__)

OIDC_ENDPOINT = "https://oidc.us-east-1.amazonaws.com"
DEFAULT_PROFILE_ARN = "arn:aws:codewhisperer:us-east-1:699475941385:profile/EHGA3GRVQMUK"


def refresh_kiro_token(
    refresh_token: str,
    client_id: str,
    client_secret: str,
) -> Tuple[bool, dict]:
    """刷新 Kiro OIDC token，返回 (ok, {accessToken, refreshToken, expiresIn})"""
    if not refresh_token or not client_id or not client_secret:
        return False, {"error": "缺少 refreshToken / clientId / clientSecret"}
    try:
        r = cffi_requests.post(
            f"{OIDC_ENDPOINT}/token",
            json={
                "grantType": "refresh_token",
                "clientId": client_id,
                "clientSecret": client_secret,
                "refreshToken": refresh_token,
            },
            headers={
                "content-type": "application/json",
                "user-agent": "aws-sdk-rust/1.3.9 os/macOS lang/rust",
            },
            impersonate="chrome131",
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            return True, {
                "accessToken": data.get("accessToken", ""),
                "refreshToken": data.get("refreshToken", refresh_token),
                "expiresIn": data.get("expiresIn", 3600),
            }
        return False, {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        return False, {"error": str(e)}


def _kiro_portal_headers(access_token: str) -> dict:
    return {
        "Accept": "application/cbor",
        "Content-Type": "application/cbor",
        "smithy-protocol": "rpc-v2-cbor",
        "Origin": "https://app.kiro.dev",
        "Referer": "https://app.kiro.dev/account/usage",
        "x-amz-user-agent": "aws-sdk-js/1.0.0 ua/2.1 os/macOS lang/js md/browser#Google-Chrome_146 m/N,M,E",
        "Authorization": f"Bearer {access_token}",
    }


def _serialize_kiro_portal_value(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, dict):
        return {key: _serialize_kiro_portal_value(val) for key, val in value.items()}
    if isinstance(value, list):
        return [_serialize_kiro_portal_value(item) for item in value]
    return value


def _fetch_kiro_portal_user_id(access_token: str, session_token: str) -> str:
    if not access_token or not session_token:
        return ""
    try:
        page = cffi_requests.get(
            "https://app.kiro.dev/account/usage",
            headers={"user-agent": "Mozilla/5.0"},
            cookies={
                "AccessToken": access_token,
                "SessionToken": session_token,
                "Idp": "BuilderId",
            },
            impersonate="chrome124",
            timeout=20,
        )
        if page.status_code != 200:
            logger.error("获取 Kiro account/usage 页面失败: HTTP %s", page.status_code)
            return ""
        user_id = page.cookies.get("UserId", "")
        if user_id:
            return user_id
        match = re.search(r'<meta name="user-id" content="([^"]+)"', page.text)
        return match.group(1) if match else ""
    except Exception as e:
        logger.error(f"获取 Kiro portal user id 失败: {e}")
        return ""


def _call_kiro_portal_operation(
    operation: str,
    body: dict,
    *,
    access_token: str,
    session_token: str,
    user_id: str,
) -> dict | None:
    if not access_token or not session_token or not user_id:
        return None
    try:
        response = cffi_requests.post(
            f"https://app.kiro.dev/service/KiroWebPortalService/operation/{operation}",
            headers=_kiro_portal_headers(access_token),
            cookies={
                "AccessToken": access_token,
                "SessionToken": session_token,
                "Idp": "BuilderId",
                "UserId": user_id,
            },
            data=cbor2.dumps(body),
            impersonate="chrome124",
            timeout=20,
        )
        if response.status_code == 200:
            return _serialize_kiro_portal_value(cbor2.loads(response.content))
        try:
            payload = _serialize_kiro_portal_value(cbor2.loads(response.content))
        except Exception:
            payload = response.text[:200]
        logger.error("Kiro %s 失败: HTTP %s %s", operation, response.status_code, payload)
        return None
    except Exception as e:
        logger.error("Kiro %s 异常: %s", operation, e)
        return None


def get_kiro_portal_state(
    access_token: str,
    session_token: str,
    *,
    profile_arn: str = "",
) -> dict | None:
    """查询 Kiro Web Portal 的账号、套餐与 usage 信息。"""
    if not access_token or not session_token:
        return None

    actual_profile_arn = profile_arn or DEFAULT_PROFILE_ARN
    user_id = _fetch_kiro_portal_user_id(access_token, session_token)
    if not user_id:
        return {
            "available": False,
            "error": "无法从 Kiro Web Portal 会话中解析 UserId",
            "profile_arn": actual_profile_arn,
        }

    user_info = _call_kiro_portal_operation(
        "GetUserInfo",
        {"origin": "KIRO_IDE", "profileArn": actual_profile_arn},
        access_token=access_token,
        session_token=session_token,
        user_id=user_id,
    ) or {}
    usage_limits = _call_kiro_portal_operation(
        "GetUserUsageAndLimits",
        {"origin": "KIRO_IDE", "isEmailRequired": True, "profileArn": actual_profile_arn},
        access_token=access_token,
        session_token=session_token,
        user_id=user_id,
    ) or {}
    subscription_plans = _call_kiro_portal_operation(
        "GetAvailableSubscriptionPlans",
        {"profileArn": actual_profile_arn},
        access_token=access_token,
        session_token=session_token,
        user_id=user_id,
    ) or {}
    return {
        "available": bool(user_info or usage_limits or subscription_plans),
        "user_id": user_id,
        "profile_arn": actual_profile_arn,
        "user_info": user_info,
        "usage_limits": usage_limits,
        "available_subscription_plans": subscription_plans,
    }


def summarize_kiro_usage(portal_state: dict | None) -> dict | None:
    """提炼 Kiro Portal 返回，便于前端直接展示。"""
    if not portal_state:
        return None

    usage_limits = portal_state.get("usage_limits") or {}
    user_info = portal_state.get("user_info") or {}
    subscription_info = usage_limits.get("subscriptionInfo") or {}
    breakdowns = []
    for item in usage_limits.get("usageBreakdownList") or []:
        free_trial_info = item.get("freeTrialInfo") or {}
        current_usage = item.get("currentUsage")
        usage_limit = item.get("usageLimit")
        trial_usage_limit = free_trial_info.get("usageLimit")
        breakdowns.append({
            "resource_type": item.get("resourceType"),
            "display_name": item.get("displayName"),
            "display_name_plural": item.get("displayNamePlural"),
            "unit": item.get("unit"),
            "current_usage": current_usage,
            "usage_limit": usage_limit,
            "remaining_usage": (usage_limit - current_usage) if isinstance(current_usage, (int, float)) and isinstance(usage_limit, (int, float)) else None,
            "current_overages": item.get("currentOverages"),
            "overage_cap": item.get("overageCap"),
            "overage_rate": item.get("overageRate"),
            "next_reset_at": item.get("nextDateReset"),
            "trial_status": free_trial_info.get("freeTrialStatus"),
            "trial_expiry": free_trial_info.get("freeTrialExpiry"),
            "trial_current_usage": free_trial_info.get("currentUsage"),
            "trial_usage_limit": trial_usage_limit,
            "trial_remaining_usage": (
                trial_usage_limit - free_trial_info.get("currentUsage")
                if isinstance(free_trial_info.get("currentUsage"), (int, float)) and isinstance(trial_usage_limit, (int, float))
                else None
            ),
        })

    plans = []
    for item in (portal_state.get("available_subscription_plans") or {}).get("subscriptionPlans") or []:
        description = item.get("description") or {}
        pricing = item.get("pricing") or {}
        plans.append({
            "name": item.get("name"),
            "title": description.get("title"),
            "billing_interval": description.get("billingInterval"),
            "features": description.get("features") or [],
            "amount": pricing.get("amount"),
            "currency": pricing.get("currency"),
            "subscription_type": item.get("qSubscriptionType"),
        })

    return {
        "user_email": user_info.get("email"),
        "user_status": user_info.get("status"),
        "user_id": portal_state.get("user_id"),
        "plan_title": subscription_info.get("subscriptionTitle"),
        "subscription_type": subscription_info.get("type"),
        "upgrade_capability": subscription_info.get("upgradeCapability"),
        "overage_capability": subscription_info.get("overageCapability"),
        "overage_enabled": (usage_limits.get("overageConfiguration") or {}).get("overageEnabled"),
        "next_reset_at": usage_limits.get("nextDateReset"),
        "days_until_reset": usage_limits.get("daysUntilReset"),
        "breakdowns": breakdowns,
        "plans": plans,
    }
