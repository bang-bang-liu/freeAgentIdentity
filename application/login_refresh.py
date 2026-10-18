"""刷新登录任务的创建、预检和执行器。"""
from __future__ import annotations

import json
import secrets
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from application.account_locks import AccountLockBusyError, account_lock_registry
from application.login_refresh_types import (
    LoginRefreshAccountResult,
    LoginRefreshAccountState,
    LoginRefreshPreview,
    LoginRefreshPreviewItem,
    LoginRefreshRequest,
    LoginRefreshTaskResult,
)
from application.manual_otp import ManualOtpHandle, manual_otp_registry
from application.tasks import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_RUNNING,
    TASK_STATUS_SUCCEEDED,
    TASK_TYPE_LOGIN_REFRESH,
    TaskLogger,
    create_task,
)
from core.account_graph import load_account_graphs, patch_account_graph
from core.auth_metadata import build_auth_metadata, get_auth_metadata, merge_auth_metadata, set_auth_metadata
from core.db import AccountCredentialModel, AccountModel, AccountOverviewModel, engine
from core.platform_accounts import resolve_primary_token
from core.workbench_api_mailbox import WorkbenchApiRebuiltMailbox
from platforms.chatgpt.login_client import EmailOtpResolver, login_chatgpt_web, mask_email
from platforms.chatgpt.login_types import (
    ChatGPTLoginError,
    ChatGPTLoginResult,
    LoginErrorCode,
    SecuritySetupResult,
)


def _has_email_otp_source(graph: dict[str, Any]) -> bool:
    """检查账号是否有可用的邮箱 OTP 数据源（Workbench API 或 IMAP）。

    Args:
        graph: 账号图数据，包含 provider_accounts 信息

    Returns:
        True 如果有 Workbench API URL 或 IMAP 配置
    """
    provider_accounts = graph.get("provider_accounts", [])
    for pa in provider_accounts:
        if not isinstance(pa, dict):
            continue
        credentials = pa.get("credentials", {})
        if not isinstance(credentials, dict):
            continue

        # 检查 Workbench API URL
        api_url = str(credentials.get("api_url") or "").strip()
        if api_url and api_url.startswith("http"):
            return True

        # 检查 IMAP 配置（未来扩展）
        imap_server = str(credentials.get("imap_server") or "").strip()
        imap_password = str(credentials.get("imap_password") or "").strip()
        if imap_server and imap_password:
            return True

    return False


def _build_email_otp_resolver_from_graph(
    *,
    graph: dict[str, Any],
    email: str,
    manual_handle: Any,
    email_otp_mode: str,
    log: Any,
    cancel_check: Any = None,
) -> EmailOtpResolver | None:
    """从账号图构建邮箱 OTP resolver。

    Args:
        graph: 账号图数据
        email: 账号邮箱
        manual_handle: 手动 OTP 处理器
        email_otp_mode: OTP 模式（auto/manual/auto_then_manual）
        log: 日志函数

    Returns:
        EmailOtpResolver 实例，如果不支持则返回 None
    """
    if email_otp_mode not in ["auto", "manual", "auto_then_manual"]:
        return None

    rebuilt = None

    # 尝试从 provider_accounts 中查找 Workbench API
    provider_accounts = graph.get("provider_accounts", [])
    for pa in provider_accounts:
        if not isinstance(pa, dict):
            continue
        credentials = pa.get("credentials", {})
        if not isinstance(credentials, dict):
            continue

        api_url = str(credentials.get("api_url") or "").strip()
        if api_url and api_url.startswith("http"):
            log(f"使用 Workbench API 获取邮箱验证码")
            rebuilt = WorkbenchApiRebuiltMailbox(api_url=api_url, email=email)
            break

    # 未来可以在这里添加 IMAP 支持
    # if not rebuilt:
    #     检查 IMAP 配置...

    # 构建 resolver
    if email_otp_mode == "manual":
        # 纯手动模式，不需要 rebuilt
        return EmailOtpResolver(
            manual_handle=manual_handle,
            mode=email_otp_mode,
            log=log,
            cancel_check=cancel_check,
        )
    elif email_otp_mode in ["auto", "auto_then_manual"]:
        # 自动或自动+手动模式
        return EmailOtpResolver(
            rebuilt=rebuilt,
            manual_handle=manual_handle if email_otp_mode == "auto_then_manual" else None,
            mode=email_otp_mode,
            log=log,
            cancel_check=cancel_check,
        )

    return None


def get_login_refresh_account_overview(
    *,
    platform: str,
    page: int = 1,
    page_size: int = 20,
    email: str = "",
) -> dict[str, Any]:
    """读取账号本地数据并计算刷新能力摘要；只读，不请求 ChatGPT。"""
    with Session(engine) as session:
        stmt = select(AccountModel).where(AccountModel.platform == platform)
        if email:
            stmt = stmt.where(AccountModel.email.contains(email))  # type: ignore
        stmt = stmt.offset((page - 1) * page_size).limit(page_size)
        accounts = session.exec(stmt).all()
        total = session.exec(select(AccountModel).where(AccountModel.platform == platform)).all()

        graphs = load_account_graphs(session, [acc.id for acc in accounts])
        items: list[LoginRefreshAccountState] = []

        for acc in accounts:
            graph = graphs.get(acc.id, {})
            creds_list = graph.get("credentials", [])
            creds = {item["key"]: item["value"] for item in creds_list}
            overview_record = session.exec(
                select(AccountOverviewModel).where(AccountOverviewModel.account_id == acc.id)
            ).first()
            summary_json = json.loads(overview_record.summary_json) if overview_record and overview_record.summary_json else {}
            auth = get_auth_metadata(summary_json)

            password_present = bool(acc.password)
            totp_present = "totp_secret" in creds
            access_token_present = "access_token" in creds

            # 检查是否有邮箱 OTP 数据源（Workbench API 或 IMAP）
            email_otp_source_present = _has_email_otp_source(graph)

            refresh_mode = ""
            if password_present and totp_present:
                refresh_mode = "password_totp"
            elif password_present:
                refresh_mode = "password"
            elif totp_present:
                refresh_mode = "email_otp_totp"
            elif email_otp_source_present:
                refresh_mode = "email_otp"
            else:
                refresh_mode = "manual_required"

            security_setup_needed = not password_present or not totp_present
            blocking_reasons: list[str] = []
            if not acc.email:
                blocking_reasons.append("缺少邮箱")
                refresh_mode = "blocked"

            items.append(
                LoginRefreshAccountState(
                    id=acc.id,
                    platform=acc.platform,
                    email=mask_email(acc.email) if acc.email else "",
                    lifecycle_status=acc.lifecycle_status or "",
                    password_present=password_present,
                    totp_present=totp_present,
                    email_otp_source_present=email_otp_source_present,
                    access_token_present=access_token_present,
                    access_token_expires_at=auth.get("access_token_expires_at", ""),
                    last_login_at=auth.get("last_login_at", ""),
                    last_login_status=auth.get("last_login_status", ""),
                    refresh_mode=refresh_mode,
                    security_setup_needed=security_setup_needed,
                    blocking_reasons=blocking_reasons,
                )
            )

        return {
            "total": len(total),
            "page": page,
            "items": [
                {
                    "id": item.id,
                    "platform": item.platform,
                    "email": item.email,
                    "lifecycle_status": item.lifecycle_status,
                    "auth_state": {
                        "password_present": item.password_present,
                        "totp_present": item.totp_present,
                        "email_otp_source_present": item.email_otp_source_present,
                        "access_token_present": item.access_token_present,
                        "access_token_expires_at": item.access_token_expires_at,
                        "last_login_at": item.last_login_at,
                        "last_login_status": item.last_login_status,
                        "refresh_mode": item.refresh_mode,
                        "security_setup_needed": item.security_setup_needed,
                        "blocking_reasons": item.blocking_reasons,
                    },
                }
                for item in items
            ],
        }


def preview_login_refresh(
    *,
    platform: str,
    account_ids: list[int],
    setup_security: bool = False,
    email_otp_mode: str = "auto_then_manual",
) -> LoginRefreshPreview:
    """本地预检，不修改账号，也不向 ChatGPT 发起登录请求。"""
    with Session(engine) as session:
        accounts = session.exec(
            select(AccountModel).where(AccountModel.id.in_(account_ids))  # type: ignore
        ).all()

        graphs = load_account_graphs(session, [acc.id for acc in accounts])
        items: list[LoginRefreshPreviewItem] = []

        for acc in accounts:
            graph = graphs.get(acc.id, {})
            creds_list = graph.get("credentials", [])
            creds = {item["key"]: item["value"] for item in creds_list}

            password_present = bool(acc.password)
            totp_present = "totp_secret" in creds

            # 检查是否有邮箱 OTP 数据源（Workbench API 或 IMAP）
            email_otp_source_present = _has_email_otp_source(graph)

            refresh_mode = ""
            if password_present and totp_present:
                refresh_mode = "password_totp"
            elif password_present:
                refresh_mode = "password"
            elif totp_present:
                refresh_mode = "email_otp_totp"
            elif email_otp_source_present:
                refresh_mode = "email_otp"
            else:
                refresh_mode = "manual_required"

            security_setup_plan = "skip"
            if setup_security:
                if password_present and totp_present:
                    security_setup_plan = "skip"
                elif password_present and not totp_present:
                    security_setup_plan = "add_totp"
                elif not password_present and totp_present:
                    security_setup_plan = "add_password"
                else:
                    security_setup_plan = "add_password_then_totp"

            blocking_reasons: list[str] = []
            ready = True
            if not acc.email:
                blocking_reasons.append("缺少邮箱")
                ready = False
                refresh_mode = "blocked"
            elif acc.platform != platform:
                blocking_reasons.append("平台不匹配")
                ready = False
            elif refresh_mode == "manual_required":
                blocking_reasons.append("需要邮箱验证码")

            items.append(
                LoginRefreshPreviewItem(
                    account_id=acc.id,
                    email=acc.email,
                    ready=ready,
                    refresh_mode=refresh_mode,
                    security_setup_plan=security_setup_plan,
                    blocking_reasons=blocking_reasons,
                )
            )

        can_start = any(item.ready for item in items)
        return LoginRefreshPreview(items=items, can_start=can_start)


def create_login_refresh_task(
    *,
    platform: str,
    account_ids: list[int],
    proxy: str = "",
    concurrency: int = 1,
    setup_security: bool = False,
    email_otp_mode: str = "auto_then_manual",
) -> dict[str, Any]:
    """创建持久化任务并唤醒 TaskRuntime。"""
    # 去重和限制
    unique_ids = list(dict.fromkeys(account_ids))
    if not unique_ids:
        raise ValueError("account_ids 不能为空")
    if len(unique_ids) > 100:
        raise ValueError("单次任务账号数量不能超过 100")
    if concurrency < 1 or concurrency > 3:
        concurrency = 1

    # 拒绝 secret 字段
    forbidden_keys = {"password", "totp_secret", "email_otp", "mail_api_key", "access_token", "session_token"}
    # （这里只是示意；实际的入参结构已经限制为只有公开字段）

    payload = {
        "platform": platform,
        "account_ids": unique_ids,
        "proxy": proxy,
        "concurrency": concurrency,
        "setup_security": setup_security,
        "email_otp_mode": email_otp_mode,
    }

    task_dict = create_task(
        task_type=TASK_TYPE_LOGIN_REFRESH,
        platform=platform,
        payload=payload,
        progress_total=len(unique_ids),
    )

    return task_dict


def _execute_login_refresh_task(payload: dict[str, Any], logger: TaskLogger) -> None:
    """刷新登录任务执行器；由 TaskRuntime 调用。"""
    platform = payload.get("platform", "")
    account_ids: list[int] = [
        int(account_id)
        for account_id in (payload.get("account_ids", []) or [])
        if str(account_id).strip().isdigit() and int(account_id) > 0
    ]
    proxy = payload.get("proxy", "")
    concurrency = int(payload.get("concurrency", 1) or 1)
    setup_security = bool(payload.get("setup_security", False))
    email_otp_mode = payload.get("email_otp_mode", "auto_then_manual")

    logger.mark_running()
    if logger.is_cancel_requested():
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return
    logger.log(f"平台={platform}，账号数={len(account_ids)}，并发={concurrency}")

    results: list[LoginRefreshAccountResult] = []
    succeeded = 0
    failed = 0
    security_setup_completed = 0
    security_setup_partial = 0

    def cancelled_result(account_id: int) -> LoginRefreshAccountResult:
        return LoginRefreshAccountResult(
            account_id=account_id,
            email="",
            status="cancelled",
            error_code="CANCELLED",
            error_message="任务已取消",
        )

    def refresh_one(index: int, account_id: int) -> LoginRefreshAccountResult:
        if logger.is_cancel_requested():
            return cancelled_result(account_id)
        logger.log(f"开始刷新登录第 {index + 1}/{len(account_ids)} 个账号：account_id={account_id}")

        try:
            with account_lock_registry.acquire(account_id):
                if logger.is_cancel_requested():
                    return cancelled_result(account_id)
                result = _refresh_single_account(
                    account_id=account_id,
                    platform=platform,
                    proxy=proxy,
                    setup_security=setup_security,
                    email_otp_mode=email_otp_mode,
                    logger=logger,
                )
                return result
        except AccountLockBusyError as e:
            logger.log(f"账号 {account_id} 被其他任务占用，跳过", level="warning")
            return LoginRefreshAccountResult(
                account_id=account_id,
                email="",
                status="failed",
                error_code="ACCOUNT_LOCKED",
                error_message=str(e),
            )
        except ChatGPTLoginError as e:
            if e.code == LoginErrorCode.CANCELLED or logger.is_cancel_requested():
                return cancelled_result(account_id)
            logger.log(f"账号 {account_id} 刷新失败：{e}", level="error")
            return LoginRefreshAccountResult(
                account_id=account_id,
                email="",
                status="failed",
                error_code=str(e.code or LoginErrorCode.NETWORK_TIMEOUT),
                error_message=e.message or str(e),
            )
        except Exception as e:
            if logger.is_cancel_requested():
                return cancelled_result(account_id)
            logger.log(f"账号 {account_id} 刷新失败：{e}", level="error")
            return LoginRefreshAccountResult(
                account_id=account_id,
                email="",
                status="failed",
                error_code="UNKNOWN_ERROR",
                error_message=str(e),
            )

    completed = 0
    cancelled = False

    def consume_result(result: LoginRefreshAccountResult) -> None:
        nonlocal completed, cancelled, succeeded, failed
        nonlocal security_setup_completed, security_setup_partial
        if result.status == "cancelled":
            cancelled = True
            return
        results.append(result)
        completed += 1
        if result.status == "succeeded":
            succeeded += 1
            if result.security_setup_status == "completed":
                security_setup_completed += 1
            elif result.security_setup_status == "partial":
                security_setup_partial += 1
        else:
            failed += 1
        # Keep the original one-argument logger contract; the task row already
        # carries ``progress_total`` from create_login_refresh_task().
        logger.set_progress(completed)
        if logger.is_cancel_requested():
            cancelled = True

    if concurrency == 1:
        for i, aid in enumerate(account_ids):
            if logger.is_cancel_requested():
                cancelled = True
                break
            result = refresh_one(i, aid)
            consume_result(result)
            if cancelled:
                break
    else:
        executor = ThreadPoolExecutor(max_workers=concurrency)
        futures = {
            executor.submit(refresh_one, i, aid): aid
            for i, aid in enumerate(account_ids)
        }
        pending = set(futures)
        try:
            while pending:
                done, pending = wait(
                    pending,
                    timeout=0.1,
                    return_when=FIRST_COMPLETED,
                )
                cancel_requested = logger.is_cancel_requested()
                for future in done:
                    try:
                        result = future.result()
                    except ChatGPTLoginError as exc:
                        account_id = futures.get(future, 0)
                        if exc.code == LoginErrorCode.CANCELLED or logger.is_cancel_requested():
                            result = cancelled_result(int(account_id or 0))
                        else:
                            result = LoginRefreshAccountResult(
                                account_id=int(account_id or 0),
                                email="",
                                status="failed",
                                error_code=str(exc.code or LoginErrorCode.NETWORK_TIMEOUT),
                                error_message=exc.message or str(exc),
                            )
                    except Exception as exc:
                        account_id = futures.get(future, 0)
                        result = LoginRefreshAccountResult(
                            account_id=int(account_id or 0),
                            email="",
                            status="failed",
                            error_code="UNKNOWN_ERROR",
                            error_message=str(exc),
                        )
                    consume_result(result)
                if cancel_requested or cancelled:
                    cancelled = True
                    for remaining in pending:
                        remaining.cancel()
                    break
        finally:
            # A cancelled refresh must return control to the task runtime even
            # when one account is still unwinding a network/OTP operation.
            executor.shutdown(wait=not cancelled, cancel_futures=True)

    if logger.is_cancel_requested():
        cancelled = True

    task_result = LoginRefreshTaskResult(
        platform=platform,
        requested=len(account_ids),
        succeeded=succeeded,
        failed=failed,
        security_setup_requested=setup_security,
        security_setup_completed=security_setup_completed,
        security_setup_partial=security_setup_partial,
        items=results,
    )

    logger.set_result_data(
        {
            "platform": task_result.platform,
            "requested": task_result.requested,
            "succeeded": task_result.succeeded,
            "failed": task_result.failed,
            "security_setup_requested": task_result.security_setup_requested,
            "security_setup_completed": task_result.security_setup_completed,
            "security_setup_partial": task_result.security_setup_partial,
            "items": [
                {
                    "account_id": r.account_id,
                    "status": r.status,
                    "security_setup_status": r.security_setup_status,
                }
                for r in task_result.items
            ],
        }
    )

    if cancelled:
        logger.log("任务已取消", level="warning")
        logger.finish(TASK_STATUS_CANCELLED, error="任务已取消")
        return

    if failed == 0:
        logger.finish(TASK_STATUS_SUCCEEDED)
        logger.log(f"所有账号刷新成功：成功={succeeded}")
    else:
        logger.finish(TASK_STATUS_FAILED)
        logger.log(f"任务完成，部分失败：成功={succeeded}，失败={failed}")


def _refresh_single_account(
    *,
    account_id: int,
    platform: str,
    proxy: str,
    setup_security: bool,
    email_otp_mode: str,
    logger: TaskLogger,
) -> LoginRefreshAccountResult:
    """单账号刷新登录执行器。"""
    with Session(engine) as session:
        account = session.get(AccountModel, account_id)
        if not account:
            return LoginRefreshAccountResult(
                account_id=account_id,
                email="",
                status="failed",
                error_code="ACCOUNT_NOT_FOUND",
                error_message="账号不存在",
            )

        if account.platform != platform:
            return LoginRefreshAccountResult(
                account_id=account_id,
                email=mask_email(account.email) if account.email else "",
                status="failed",
                error_code="PLATFORM_UNSUPPORTED",
                error_message="平台不匹配",
            )

        if not account.email:
            return LoginRefreshAccountResult(
                account_id=account_id,
                email="",
                status="failed",
                error_code="MISSING_EMAIL",
                error_message="账号邮箱为空",
            )

        graph = load_account_graphs(session, [account_id])[account_id]
        creds_list = graph.get("credentials", [])
        creds = {item["key"]: item["value"] for item in creds_list}

        password = account.password or ""
        totp_secret = creds.get("totp_secret", "")

        has_password = bool(password)
        has_totp = bool(totp_secret)
        has_email_otp_source = _has_email_otp_source(graph)

        logger.log(
            f"读取已保存登录资料：password={'yes' if has_password else 'no'}, "
            f"totp={'yes' if has_totp else 'no'}, email_otp_source={'yes' if has_email_otp_source else 'no'}"
        )

        # 构建手动 OTP 处理器
        manual_otp_handle = ManualOtpHandle(
            task_id=logger.task_id,
            worker_id=f"account_{account_id}",
            worker_label=mask_email(account.email),
            registry=manual_otp_registry,
        )

        # 构建邮箱 OTP resolver
        otp_resolver = _build_email_otp_resolver_from_graph(
            graph=graph,
            email=account.email,
            manual_handle=manual_otp_handle,
            email_otp_mode=email_otp_mode,
            log=logger.log,
            cancel_check=logger.is_cancel_requested,
        )

        # 构建手动 TOTP 处理器（当账号无保存的 TOTP 时使用）
        manual_totp_handle = manual_otp_handle if not has_totp else None

        try:
            if logger.is_cancel_requested():
                return LoginRefreshAccountResult(
                    account_id=account_id,
                    email=mask_email(account.email),
                    status="cancelled",
                    error_code=LoginErrorCode.CANCELLED,
                    error_message="任务已取消",
                )
            # 主登录流程
            logger.log("开始 ChatGPT 登录")
            register_cancel_callback = getattr(logger, "register_cancel_callback", None)
            login_result = login_chatgpt_web(
                email=account.email,
                password=password,
                totp_secret=totp_secret,
                proxy=proxy or None,
                otp_resolver=otp_resolver,
                manual_totp_handle=manual_totp_handle,
                log=logger.log,
                cancel_check=logger.is_cancel_requested,
                register_cancel_callback=(
                    register_cancel_callback
                    if callable(register_cancel_callback)
                    else None
                ),
            )

            if logger.is_cancel_requested():
                return LoginRefreshAccountResult(
                    account_id=account_id,
                    email=mask_email(account.email),
                    status="cancelled",
                    error_code=LoginErrorCode.CANCELLED,
                    error_message="任务已取消",
                )

            logger.log(
                f"登录成功：method={login_result.login_method}, "
                f"access_token={'yes' if login_result.access_token else 'no'}"
            )

            # 安全设置流程
            security_setup_status = "skipped"
            password_created = False
            totp_created = False
            new_password_plaintext = ""

            if setup_security:
                if logger.is_cancel_requested():
                    return LoginRefreshAccountResult(
                        account_id=account_id,
                        email=mask_email(account.email),
                        status="cancelled",
                        error_code=LoginErrorCode.CANCELLED,
                        error_message="任务已取消",
                    )
                logger.log(f"开始安全设置：has_password={has_password}, has_totp={has_totp}")

                # 调用 ensure_security
                from platforms.chatgpt.ensure_security import ensure_security

                security_result = ensure_security(
                    email=account.email,
                    session=login_result,
                    has_password=has_password,
                    has_totp=has_totp,
                    proxy=proxy or "",
                    log=logger.log,
                )

                password_created = security_result.password_created
                totp_created = security_result.totp_created
                security_setup_status = security_result.security_setup_status

                if security_result.password_created:
                    password = security_result.new_password
                    new_password_plaintext = security_result.new_password
                if security_result.totp_created:
                    totp_secret = security_result.totp_secret

                logger.log(
                    f"安全设置完成：status={security_setup_status}, "
                    f"password_created={password_created}, totp_created={totp_created}"
                )

            # 持久化凭据
            if logger.is_cancel_requested():
                return LoginRefreshAccountResult(
                    account_id=account_id,
                    email=mask_email(account.email),
                    status="cancelled",
                    error_code=LoginErrorCode.CANCELLED,
                    error_message="任务已取消",
                )
            logger.log("保存新的 ChatGPT 登录凭据")
            _persist_credentials(
                account_id=account_id,
                account_email=account.email,
                password=password,
                totp_secret=totp_secret,
                login_result=login_result,
                password_created=password_created,
                totp_created=totp_created,
                security_setup_status=security_setup_status,
                session=session,
            )

            logger.log("刷新登录成功")
            return LoginRefreshAccountResult(
                account_id=account_id,
                email=mask_email(account.email),
                status="succeeded",
                login_method=login_result.login_method,
                access_token_updated=bool(login_result.access_token),
                session_token_updated=bool(login_result.session_token),
                token_expires_at=login_result.access_token_expires_at,
                security_setup_status=security_setup_status,
                password_created=password_created,
                totp_created=totp_created,
            )

        except ChatGPTLoginError as e:
            if e.code == LoginErrorCode.CANCELLED or logger.is_cancel_requested():
                logger.log("登录已取消", level="warning")
                return LoginRefreshAccountResult(
                    account_id=account_id,
                    email=mask_email(account.email),
                    status="cancelled",
                    error_code=LoginErrorCode.CANCELLED,
                    error_message="任务已取消",
                )
            logger.log(f"登录失败：{e}", level="error")
            return LoginRefreshAccountResult(
                account_id=account_id,
                email=mask_email(account.email),
                status="failed",
                error_code=str(e.code or LoginErrorCode.NETWORK_TIMEOUT),
                error_message=e.message or str(e),
            )
        except Exception as e:
            if logger.is_cancel_requested():
                logger.log("登录已取消", level="warning")
                return LoginRefreshAccountResult(
                    account_id=account_id,
                    email=mask_email(account.email),
                    status="cancelled",
                    error_code=LoginErrorCode.CANCELLED,
                    error_message="任务已取消",
                )
            logger.log(f"登录失败：{e}", level="error")
            return LoginRefreshAccountResult(
                account_id=account_id,
                email=mask_email(account.email),
                status="failed",
                error_code="LOGIN_FAILED",
                error_message=str(e),
            )
        finally:
            close_resolver = getattr(otp_resolver, "close", None)
            if callable(close_resolver):
                try:
                    close_resolver()
                except Exception:
                    pass


def _persist_credentials(
    *,
    account_id: int,
    account_email: str,
    password: str,
    totp_secret: str,
    login_result: ChatGPTLoginResult,
    password_created: bool,
    totp_created: bool,
    security_setup_status: str,
    session: Session,
) -> None:
    """原子更新账号凭据、密码和 auth 元数据。

    设计依据：docs/CHATGPT_LOGIN_REFRESH_DESIGN.md 10.2。
    - credential_updates 传入 login_result.credential_updates()（已过滤空值）
    - primary_token 用 access_token（主 token 固定走单独参数）
    - 密码更新走 AccountModel.password
    - totp_secret / device_id 等其他凭据走 credential_updates
    - 所有写操作在同一个 session 事务内完成
    """
    from core.account_graph import patch_account_graph

    account = session.get(AccountModel, account_id)
    if not account:
        raise ValueError(f"账号 {account_id} 不存在")

    # 构建凭据更新字典
    credential_updates = login_result.credential_updates()

    # 如果新建了 TOTP，加入凭据更新
    if totp_secret:
        credential_updates["totp_secret"] = totp_secret

    # 更新账号图谱：主 token + 其他凭据
    patch_account_graph(
        session=session,
        model=account,
        primary_token=login_result.access_token if login_result.access_token else None,
        credential_updates=credential_updates,
    )

    # 更新密码（如果有新建或已有）
    if password:
        account.password = password

    # 读取 account_overviews 并更新 auth 元数据
    overview_record = session.exec(
        select(AccountOverviewModel).where(AccountOverviewModel.account_id == account_id)
    ).first()

    if not overview_record:
        overview_record = AccountOverviewModel(
            account_id=account_id,
            platform_id=account_email,
            summary_json="{}",
        )
        session.add(overview_record)

    import json
    summary_json = json.loads(overview_record.summary_json) if overview_record.summary_json else {}
    existing_auth = get_auth_metadata(summary_json)

    now = datetime.now(timezone.utc)
    new_auth = build_auth_metadata(
        last_login_at=now,
        last_login_method=login_result.login_method,
        access_token_obtained_at=now,
        access_token_expires_at=datetime.fromisoformat(login_result.access_token_expires_at.replace("Z", "+00:00")) if login_result.access_token_expires_at else None,
        token_exp_source="jwt_exp" if login_result.access_token_expires_at else "",
        last_login_status="succeeded",
        last_login_error_code="",
        security_setup_status=security_setup_status,
        security_setup_requested=security_setup_status != "skipped",
        security_setup_at=now if security_setup_status in ["completed", "partial"] else None,
        credential_version=existing_auth.get("credential_version", 0) + 1,
    )

    merged_auth = merge_auth_metadata(existing_auth, new_auth)
    updated_summary = set_auth_metadata(summary_json, merged_auth)
    overview_record.summary_json = json.dumps(updated_summary, ensure_ascii=False)

    session.commit()


def _generate_password() -> str:
    """生成符合 ChatGPT 密码策略的随机密码。"""
    # 16 位随机字符：包含大小写字母、数字和特殊字符
    import string
    chars = string.ascii_letters + string.digits + "!@#$%^&*"
    return "".join(secrets.choice(chars) for _ in range(16))
