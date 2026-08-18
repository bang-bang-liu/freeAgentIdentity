"""Cursor remote account, billing, usage, and checkout APIs."""

import logging

logger = logging.getLogger(__name__)


def _cursor_headers(token: str) -> dict:
    return {
        "Cookie": f"WorkosCursorSessionToken={token}",
        "user-agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/145.0.0.0 Safari/537.36",
    }


def get_cursor_user_info(token: str) -> dict | None:
    """通过 token 获取用户信息"""
    from curl_cffi import requests as curl_req
    
    try:
        r = curl_req.get(
            "https://cursor.com/api/auth/me",
            headers=_cursor_headers(token),
            impersonate="chrome124",
            timeout=15,
        )
        
        if r.status_code == 200:
            return r.json()
        return None
    
    except Exception as e:
        logger.error(f"获取 Cursor 用户信息失败: {e}")
        return None


def get_cursor_billing_info(token: str) -> dict | None:
    """获取 Cursor 套餐、试用与账单状态。"""
    from curl_cffi import requests as curl_req

    try:
        r = curl_req.get(
            "https://cursor.com/api/auth/stripe",
            headers={
                **_cursor_headers(token),
                "accept": "application/json",
                "content-type": "application/json",
            },
            impersonate="chrome124",
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
        logger.error("获取 Cursor 套餐状态失败: HTTP %s %s", r.status_code, r.text[:200])
        return None
    except Exception as e:
        logger.error(f"获取 Cursor 套餐状态失败: {e}")
        return None


def has_cursor_valid_payment_method(token: str) -> bool | None:
    """查询 Cursor 是否已绑定有效支付方式。"""
    from curl_cffi import requests as curl_req

    try:
        r = curl_req.get(
            "https://cursor.com/api/auth/has_valid_payment_method",
            headers={"accept": "application/json", **_cursor_headers(token)},
            impersonate="chrome124",
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json()
            return bool(data.get("hasValidPaymentMethod"))
        logger.error("获取 Cursor 支付方式状态失败: HTTP %s %s", r.status_code, r.text[:200])
        return None
    except Exception as e:
        logger.error(f"获取 Cursor 支付方式状态失败: {e}")
        return None


def get_cursor_usage(token: str, user_id: str) -> dict | None:
    """查询 Cursor usage 数据。"""
    from curl_cffi import requests as curl_req

    if not user_id:
        return None

    try:
        r = curl_req.get(
            f"https://cursor.com/api/usage?user={user_id}",
            headers={"accept": "application/json", **_cursor_headers(token)},
            impersonate="chrome124",
            timeout=15,
        )
        if r.status_code == 200:
            return r.json()
        logger.error("获取 Cursor usage 失败: HTTP %s %s", r.status_code, r.text[:200])
        return None
    except Exception as e:
        logger.error(f"获取 Cursor usage 失败: {e}")
        return None


def summarize_cursor_usage(usage_data: dict | None) -> dict | None:
    """提炼更适合 UI 展示的 Cursor usage 摘要。"""
    if not usage_data:
        return None

    summary = {
        "start_of_month": usage_data.get("startOfMonth"),
        "models": {},
        "has_any_limit": False,
    }
    for model_name, value in usage_data.items():
        if model_name == "startOfMonth" or not isinstance(value, dict):
            continue
        max_token_usage = value.get("maxTokenUsage")
        max_request_usage = value.get("maxRequestUsage")
        model_summary = {
            "num_requests": value.get("numRequests"),
            "num_requests_total": value.get("numRequestsTotal"),
            "num_tokens": value.get("numTokens"),
            "max_request_usage": max_request_usage,
            "max_token_usage": max_token_usage,
            "remaining_requests": None,
            "remaining_tokens": None,
        }
        if isinstance(max_request_usage, (int, float)) and isinstance(value.get("numRequests"), (int, float)):
            model_summary["remaining_requests"] = max_request_usage - value["numRequests"]
        if isinstance(max_token_usage, (int, float)) and isinstance(value.get("numTokens"), (int, float)):
            model_summary["remaining_tokens"] = max_token_usage - value["numTokens"]
        if max_token_usage is not None or max_request_usage is not None:
            summary["has_any_limit"] = True
        summary["models"][model_name] = model_summary
    return summary


def generate_cursor_checkout_link(
    token: str,
    *,
    tier: str = "pro",
    allow_trial: bool = True,
    allow_automatic_payment: bool = False,
    yearly: bool = False,
) -> str | None:
    """生成 Cursor Pro 结账链接，可用于 7 天试用入口。"""
    from curl_cffi import requests as curl_req

    try:
        r = curl_req.post(
            "https://cursor.com/api/checkout",
            headers={
                "accept": "application/json",
                "content-type": "application/json",
                "origin": "https://cursor.com",
                "referer": "https://cursor.com/dashboard",
                **_cursor_headers(token),
            },
            json={
                "tier": tier,
                "allowTrial": allow_trial,
                "allowAutomaticPayment": allow_automatic_payment,
                "yearly": yearly,
            },
            impersonate="chrome124",
            timeout=20,
        )
        if r.status_code == 200:
            try:
                payload = r.json()
            except Exception:
                payload = r.text
            if isinstance(payload, str) and payload.startswith("https://"):
                return payload
        logger.error("生成 Cursor 结账链接失败: HTTP %s %s", r.status_code, r.text[:300])
        return None
    except Exception as e:
        logger.error(f"生成 Cursor 结账链接失败: {e}")
        return None
