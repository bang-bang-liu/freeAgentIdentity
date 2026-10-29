"""刷新登录 API 路由。

设计依据：docs/CHATGPT_LOGIN_REFRESH_DESIGN.md 7.1 / 7.2 / 9.1。

所有响应模型都只含脱敏字段，任何 secret（token / password / totp_secret）
都不允许进入 API 响应。
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.login_refresh import (
    create_login_refresh_task,
    get_login_refresh_account_overview,
    preview_login_refresh,
)
from application.login_refresh_types import LoginRefreshRequest

router = APIRouter()


# ---------------------------------------------------------------- 请求模型

class LoginRefreshCreateRequest(BaseModel):
    """创建刷新登录任务的请求体。

    只接受安全的公开参数，所有 secret 都通过后端从数据库读取。
    """

    platform: str = Field(..., description="平台名称，目前只支持 chatgpt")
    ids: list[int] = Field(default_factory=list, description="要刷新的账号 ID 列表")
    select_all: bool = Field(False, description="是否选择全部账号")
    status_filter: str | None = Field(None, description="状态筛选")
    search_filter: str | None = Field(None, description="搜索筛选")
    proxy: str = Field("", description="代理地址（可选），格式如 http://ip:port")
    concurrency: int = Field(1, ge=1, le=3, description="并发数，1-3")
    setup_security: bool = Field(False, description="是否在登录后补设密码/TOTP")
    email_otp_mode: str = Field("auto_then_manual", description="邮箱 OTP 取码模式：auto / manual / auto_then_manual")


class LoginRefreshPreviewRequest(BaseModel):
    """预检刷新登录能力的请求体。"""

    platform: str = Field(..., description="平台名称")
    ids: list[int] = Field(default_factory=list, description="要预检的账号 ID 列表")
    select_all: bool = Field(False, description="是否选择全部账号")
    status_filter: str | None = Field(None, description="状态筛选")
    search_filter: str | None = Field(None, description="搜索筛选")
    setup_security: bool = Field(False, description="是否计划补设密码/TOTP")


# ---------------------------------------------------------------- API 路由

@router.post("/login-refresh/preview")
def login_refresh_preview(req: LoginRefreshPreviewRequest):
    """预检账号刷新能力：检查本地数据，返回每个账号的刷新模式和阻断原因。

    不发起任何网络请求，只读本地数据库。
    """
    try:
        from domain.accounts import AccountExportSelection
        from infrastructure.accounts_repository import AccountsRepository

        repository = AccountsRepository()
        records = repository.select_for_export(
            AccountExportSelection(
                platform=req.platform,
                ids=req.ids,
                select_all=req.select_all,
                status_filter=req.status_filter or "",
                search_filter=req.search_filter or "",
            )
        )

        account_ids = [rec.id for rec in records]

        result = preview_login_refresh(
            platform=req.platform,
            account_ids=account_ids,
            setup_security=req.setup_security,
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    return {
        "accounts": [
            {
                "account_id": item.account_id,
                "email": item.email,
                "ready": item.ready,
                "refresh_mode": item.refresh_mode,
                "security_setup_plan": item.security_setup_plan,
                "blocking_reasons": item.blocking_reasons,
                "can_refresh": item.ready,
                "reason": ", ".join(item.blocking_reasons) if item.blocking_reasons else "",
            }
            for item in result.items
        ],
        "can_start": result.can_start,
    }


@router.post("/login-refresh/create")
def login_refresh_create(req: LoginRefreshCreateRequest):
    """创建刷新登录任务并返回任务 ID。

    任务创建后立即入队，由 TaskRuntime 异步执行。前端轮询 /tasks/{task_id}
    获取进度。
    """
    if req.platform != "chatgpt":
        raise HTTPException(status_code=400, detail="目前只支持 chatgpt 平台")

    try:
        from domain.accounts import AccountExportSelection
        from infrastructure.accounts_repository import AccountsRepository

        repository = AccountsRepository()
        records = repository.select_for_export(
            AccountExportSelection(
                platform=req.platform,
                ids=req.ids,
                select_all=req.select_all,
                status_filter=req.status_filter or "",
                search_filter=req.search_filter or "",
            )
        )

        account_ids = [rec.id for rec in records]

        if not account_ids:
            raise HTTPException(status_code=400, detail="没有选中任何账号")

        if len(account_ids) > 100:
            raise HTTPException(status_code=400, detail="单次任务账号数量不能超过 100")

        task_dict = create_login_refresh_task(
            platform=req.platform,
            account_ids=account_ids,
            proxy=req.proxy,
            concurrency=req.concurrency,
            setup_security=req.setup_security,
            email_otp_mode=req.email_otp_mode,
        )
    except HTTPException:
        raise
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    return {
        "task_id": task_dict["task_id"],
        "status": task_dict["status"],
        "progress": task_dict["progress"],
    }


@router.get("/login-refresh/accounts")
def login_refresh_accounts(
    platform: str = "chatgpt",
    page: int = 1,
    page_size: int = 20,
    email: str = "",
):
    """查询账号列表及其刷新能力摘要。

    返回字段包括：账号 ID、邮箱（脱敏）、是否有密码、是否有 TOTP、
    access_token 是否存在、推荐的刷新模式、阻断原因等。
    """
    if page < 1:
        page = 1
    if page_size < 1 or page_size > 100:
        page_size = 20

    try:
        result = get_login_refresh_account_overview(
            platform=platform,
            page=page,
            page_size=page_size,
            email=email,
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e)) from e

    return result
