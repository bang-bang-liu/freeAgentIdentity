from __future__ import annotations

from fastapi import APIRouter, Body, HTTPException
from pydantic import BaseModel, Field

from application.account_checks import AccountChecksService

router = APIRouter(prefix="/accounts", tags=["account-checks"])
service = AccountChecksService()


class AccountCheckAllRequest(BaseModel):
    """批量状态/订阅检测的目标范围。

    ``ids`` 为空表示检查指定平台的全部账号；保留 query 参数 ``platform``
    兼容旧版前端和脚本调用。
    """

    platform: str | None = None
    ids: list[int] = Field(default_factory=list)
    proxy: str | None = None
    check_chain: bool | None = None
    country: str | None = None


@router.post("/check-all")
def check_all_accounts(
    platform: str = "chatgpt",
    body: AccountCheckAllRequest | None = Body(default=None),
):
    request_platform = str(
        (body.platform if body and body.platform is not None else platform)
        or "chatgpt"
    )
    account_ids = body.ids if body is not None else []
    proxy = body.proxy if body is not None else None
    check_chain = body.check_chain if body is not None else None
    try:
        kwargs = {"account_ids": account_ids, "proxy": proxy}
        if check_chain is not None:
            kwargs["check_chain"] = check_chain
        return service.check_all_async(request_platform, **kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/check-gcash")
def check_gcash_accounts(
    platform: str = "chatgpt",
    body: AccountCheckAllRequest | None = Body(default=None),
):
    request_platform = str(
        (body.platform if body and body.platform is not None else platform)
        or "chatgpt"
    )
    account_ids = body.ids if body is not None else []
    proxy = body.proxy if body is not None else None
    try:
        return service.check_gcash_async(
            request_platform,
            account_ids=account_ids,
            proxy=proxy,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/check-trial")
def check_trial_accounts(
    platform: str = "chatgpt",
    body: AccountCheckAllRequest | None = Body(default=None),
):
    request_platform = str(
        (body.platform if body and body.platform is not None else platform)
        or "chatgpt"
    )
    account_ids = body.ids if body is not None else []
    proxy = body.proxy if body is not None else None
    country = body.country if body is not None and body.country else "PH"
    check_chain = bool(body.check_chain) if body is not None else False
    try:
        return service.check_trial_async(
            request_platform,
            account_ids=account_ids,
            proxy=proxy,
            country=country,
            check_chain=check_chain,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
