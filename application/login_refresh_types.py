"""刷新登录任务的类型定义。"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class LoginRefreshRequest:
    """刷新登录任务请求。"""
    platform: str
    account_ids: list[int]
    proxy: str = ""
    concurrency: int = 1
    setup_security: bool = False
    email_otp_mode: str = "auto_then_manual"


@dataclass
class LoginRefreshAccountState:
    """单账号刷新能力状态。"""
    id: int
    platform: str
    email: str
    lifecycle_status: str
    password_present: bool
    totp_present: bool
    email_otp_source_present: bool
    access_token_present: bool
    access_token_expires_at: str = ""
    last_login_at: str = ""
    last_login_status: str = ""
    refresh_mode: str = ""
    security_setup_needed: bool = False
    blocking_reasons: list[str] = None

    def __post_init__(self) -> None:
        if self.blocking_reasons is None:
            self.blocking_reasons = []


@dataclass
class LoginRefreshPreviewItem:
    """单账号预检结果。"""
    account_id: int
    email: str
    ready: bool
    refresh_mode: str
    security_setup_plan: str
    blocking_reasons: list[str] = None

    def __post_init__(self) -> None:
        if self.blocking_reasons is None:
            self.blocking_reasons = []


@dataclass
class LoginRefreshPreview:
    """刷新登录任务预检结果。"""
    items: list[LoginRefreshPreviewItem]
    can_start: bool


@dataclass
class LoginRefreshAccountResult:
    """单账号刷新结果。"""
    account_id: int
    email: str
    status: str
    login_method: str = ""
    access_token_updated: bool = False
    session_token_updated: bool = False
    token_expires_at: str = ""
    security_setup_status: str = ""
    password_created: bool = False
    totp_created: bool = False
    error_code: str = ""
    error_message: str = ""


@dataclass
class LoginRefreshTaskResult:
    """刷新登录任务结果。"""
    platform: str
    requested: int
    succeeded: int
    failed: int
    security_setup_requested: bool
    security_setup_completed: int
    security_setup_partial: int
    items: list[LoginRefreshAccountResult]
