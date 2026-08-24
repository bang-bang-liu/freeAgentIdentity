from __future__ import annotations

from typing import Literal, Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from application.task_commands import TaskCommandsService
from application.manual_otp import (
    InvalidManualOtp,
    ManualOtpRequestAmbiguous,
    ManualOtpRequestNotFound,
    manual_otp_registry,
)
from application.tasks_query import TasksQueryService

router = APIRouter(prefix="/tasks", tags=["task-commands"])
command_service = TaskCommandsService()
query_service = TasksQueryService()


class RegisterTaskRequest(BaseModel):
    email: Optional[str] = None
    password: Optional[str] = None
    count: int = 1
    concurrency: int = 1
    proxy: Optional[str] = None
    executor_type: Literal["protocol", "headless", "headed"] = "headless"
    captcha_solver: str = "auto"
    sub2api_url: Optional[str] = None
    sub2api_api_key: Optional[str] = None
    extra: dict = Field(default_factory=dict)


class SubmitOtpRequest(BaseModel):
    code: str
    request_id: str = ""


@router.post("/register")
def create_register_task(body: RegisterTaskRequest):
    payload = body.model_dump(exclude={"sub2api_url", "sub2api_api_key"})
    extra = dict(body.extra or {})
    upload_config = None
    if bool(extra.get("auto_upload_sub2api_agent_identity")):
        sub2api_url = str(body.sub2api_url or "").strip()
        sub2api_api_key = str(body.sub2api_api_key or "").strip()
        if not sub2api_url or not sub2api_api_key:
            raise HTTPException(400, "自动上传 Sub2API 需要地址和 Admin API Key")
        upload_config = {
            "sub2api_url": sub2api_url,
            "api_key": sub2api_api_key,
        }
    extra["identity_provider"] = "mailbox"
    mail_provider = str(extra.get("mail_provider") or "").strip()
    pool_text = str(extra.get("local_ms_pool_text") or "").strip()
    pool_file = str(extra.get("local_ms_pool_file") or "").strip()
    if not mail_provider:
        if pool_text or pool_file:
            # Backward compatibility for callers that supplied an inline
            # Outlook pool before mailbox providers were selectable here.
            mail_provider = "local_ms_pool"
        else:
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            mail_provider = ProviderSettingsRepository().get_default_provider_key("mailbox")
    if body.executor_type == "protocol":
        if not mail_provider:
            raise HTTPException(400, "协议注册需要选择邮箱 Provider（或填写 Outlook 账号池）")
        if mail_provider == "local_ms_pool":
            from core.local_ms_mailbox import MAX_OUTLOOK_SUBADDRESS_COUNT
            from infrastructure.provider_settings_repository import ProviderSettingsRepository

            resolved = ProviderSettingsRepository().resolve_runtime_settings(
                "mailbox", "local_ms_pool", extra
            )
            pool_text = str(resolved.get("local_ms_pool_text") or "").strip()
            pool_file = str(resolved.get("local_ms_pool_file") or "").strip()
            if not pool_text and not pool_file:
                raise HTTPException(400, "协议注册选择本地微软邮箱池时，需要配置 Outlook 账号池文本或文件")

            # Outlook can provide six independently routed +reg aliases per
            # parent. Other mailbox providers, including domain IMAP, allocate
            # their own addresses and do not need this override.
            extra["local_ms_pool_alias_count"] = MAX_OUTLOOK_SUBADDRESS_COUNT
            if pool_text:
                from core.local_ms_mailbox import parse_local_ms_pool_rows

                rows = parse_local_ms_pool_rows(pool_text)
                if not rows:
                    raise HTTPException(400, "Outlook 账号池未解析到有效账号，请检查输入格式")
                allow_reuse = str(resolved.get("local_ms_pool_allow_reuse") or "").strip().lower() in {
                    "1", "true", "yes", "on"
                }
                capacity = len(rows) * MAX_OUTLOOK_SUBADDRESS_COUNT
                if not allow_reuse and capacity < body.count:
                    raise HTTPException(
                        400,
                        f"Outlook 子邮箱容量 {capacity} 少于注册数量 {body.count}（每个母邮箱最多 6 个）",
                    )
    payload["extra"] = extra
    if mail_provider:
        extra["mail_provider"] = mail_provider
    if upload_config:
        return command_service.create_register_task(
            payload,
            sub2api_upload=upload_config,
        )
    return command_service.create_register_task(payload)


@router.post("/{task_id}/cancel")
def cancel_task(task_id: str):
    task = command_service.cancel_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/{task_id}/otp-requests")
def list_otp_requests(task_id: str):
    if not query_service.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    return {"items": manual_otp_registry.list_pending(task_id)}


@router.post("/{task_id}/otp")
def submit_otp(task_id: str, body: SubmitOtpRequest):
    if not query_service.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    try:
        request_id = manual_otp_registry.submit_code(
            task_id=task_id,
            request_id=body.request_id,
            code=body.code,
        )
    except InvalidManualOtp as exc:
        raise HTTPException(400, str(exc)) from exc
    except ManualOtpRequestNotFound as exc:
        raise HTTPException(404, str(exc)) from exc
    except ManualOtpRequestAmbiguous as exc:
        raise HTTPException(409, str(exc)) from exc
    return {"accepted": True, "request_id": request_id}


@router.get("/{task_id}/logs/stream")
async def stream_logs(task_id: str, since: int = 0):
    if not query_service.get_task(task_id):
        raise HTTPException(404, "任务不存在")
    return StreamingResponse(
        command_service.stream_task_events(task_id, since=since),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
