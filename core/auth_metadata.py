"""账户登录元数据的读写辅助，对应设计文档 §8.2 auth 命名空间。"""
from __future__ import annotations

from datetime import datetime
from typing import Any


def get_auth_metadata(summary_json: dict[str, Any]) -> dict[str, Any]:
    """从 account_overviews.summary_json 读取 auth 命名空间。"""
    return summary_json.get("auth", {})


def set_auth_metadata(summary_json: dict[str, Any], auth: dict[str, Any]) -> dict[str, Any]:
    """更新 summary_json 的 auth 命名空间并返回新的 summary_json。"""
    updated = summary_json.copy()
    updated["auth"] = auth
    return updated


def build_auth_metadata(
    *,
    last_login_at: datetime | None = None,
    last_login_method: str = "",
    access_token_obtained_at: datetime | None = None,
    access_token_expires_at: datetime | None = None,
    token_exp_source: str = "",
    last_login_status: str = "",
    last_login_error_code: str = "",
    security_setup_status: str = "",
    security_setup_requested: bool = False,
    security_setup_at: datetime | None = None,
    credential_version: int = 0,
) -> dict[str, Any]:
    """构建符合设计的 auth 元数据字典；只保存非空值。"""
    auth: dict[str, Any] = {}
    if last_login_at:
        auth["last_login_at"] = last_login_at.isoformat()
    if last_login_method:
        auth["last_login_method"] = last_login_method
    if access_token_obtained_at:
        auth["access_token_obtained_at"] = access_token_obtained_at.isoformat()
    if access_token_expires_at:
        auth["access_token_expires_at"] = access_token_expires_at.isoformat()
    if token_exp_source:
        auth["token_exp_source"] = token_exp_source
    if last_login_status:
        auth["last_login_status"] = last_login_status
    if last_login_error_code:
        auth["last_login_error_code"] = last_login_error_code
    if security_setup_status:
        auth["security_setup_status"] = security_setup_status
    if security_setup_requested:
        auth["security_setup_requested"] = security_setup_requested
    if security_setup_at:
        auth["security_setup_at"] = security_setup_at.isoformat()
    if credential_version:
        auth["credential_version"] = credential_version
    return auth


def merge_auth_metadata(
    existing: dict[str, Any],
    updates: dict[str, Any],
) -> dict[str, Any]:
    """合并现有 auth 元数据与新的更新；updates 中的非空值覆盖旧值。"""
    merged = existing.copy()
    for key, value in updates.items():
        if value is not None and value != "":
            merged[key] = value
    return merged
