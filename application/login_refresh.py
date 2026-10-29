"""刷新登录任务的创建、预检和执行器。"""
from __future__ import annotations

import json
import time
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from sqlmodel import Session, select

from application.account_locks import AccountLockBusyError, account_lock_registry
from application.login_refresh_types import (
    LoginRefreshAccountResult,
    LoginRefreshAccountState,
    LoginRefreshPreview,
    LoginRefreshPreviewItem,
    LoginRefreshTaskResult,
)
from application.manual_otp import ManualOtpHandle, manual_otp_registry
from application.tasks import (
    TASK_STATUS_CANCELLED,
    TASK_STATUS_FAILED,
    TASK_STATUS_SUCCEEDED,
    TASK_TYPE_LOGIN_REFRESH,
    TaskLogger,
    create_task,
)
from core.account_graph import load_account_graphs, patch_account_graph
from core.auth_metadata import get_auth_metadata
from core.db import AccountCredentialModel, AccountModel, AccountOverviewModel, engine
from core.platform_accounts import resolve_primary_token
from core.workbench_api_mailbox import WorkbenchApiRebuiltMailbox
from platforms.chatgpt.login_client import EmailOtpResolver, login_chatgpt_web
from platforms.chatgpt.login_types import (
    ChatGPTLoginError,
    ChatGPTLoginResult,
    LoginErrorCode,
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
            email=email,
        )
    elif email_otp_mode in ["auto", "auto_then_manual"]:
        # 自动或自动+手动模式
        return EmailOtpResolver(
            rebuilt=rebuilt,
            manual_handle=manual_handle if email_otp_mode == "auto_then_manual" else None,
            mode=email_otp_mode,
            log=log,
            cancel_check=cancel_check,
            email=email,
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
                    email=acc.email or "",
                    lifecycle_status=graph.get("lifecycle_status") or "registered",
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
    if platform != "chatgpt":
        raise ValueError("目前只支持 chatgpt 平台")
    if email_otp_mode not in {"auto", "manual", "auto_then_manual"}:
        raise ValueError("邮箱验证码来源无效")
    # 去重和限制
    unique_ids = list(dict.fromkeys(account_ids))
    if not unique_ids:
        raise ValueError("account_ids 不能为空")
    if len(unique_ids) > 100:
        raise ValueError("单次任务账号数量不能超过 100")
    if concurrency < 1 or concurrency > 3:
        concurrency = 1

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
        results.append(result)
        if result.status == "cancelled":
            cancelled = True
            return
        completed += 1
        if result.status == "succeeded":
            succeeded += 1
            record_success = getattr(logger, "record_success", None)
            if callable(record_success):
                record_success()
            if result.security_setup_status == "completed":
                security_setup_completed += 1
            elif result.security_setup_status == "partial":
                security_setup_partial += 1
        else:
            failed += 1
            record_error = getattr(logger, "record_error", None)
            if callable(record_error):
                record_error(f"{result.email or result.account_id}: {result.error_code} {result.error_message}")
        logger.set_result_data({"platform": platform, "requested": len(account_ids),
                                "succeeded": succeeded, "failed": failed,
                                "items": [asdict(item) for item in results]})
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
                asdict(r) for r in task_result.items
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
    *, account_id: int, platform: str, proxy: str, setup_security: bool,
    email_otp_mode: str, logger: TaskLogger,
) -> LoginRefreshAccountResult:
    """Login under the task's account lock; never save an unverified token."""
    with Session(engine) as session:
        account = session.get(AccountModel, account_id)
        if not account:
            return LoginRefreshAccountResult(account_id, "", "failed", error_code=LoginErrorCode.ACCOUNT_NOT_FOUND, error_message="账号不存在")
        if account.platform != platform or platform != "chatgpt":
            return LoginRefreshAccountResult(account_id, account.email, "failed", error_code=LoginErrorCode.PLATFORM_UNSUPPORTED, error_message="平台不匹配")
        graph = load_account_graphs(session, [account_id])[account_id]
        creds = {item["key"]: item["value"] for item in graph.get("credentials", []) if item.get("scope") == "platform"}
        password, totp_secret = account.password or "", creds.get("totp_secret", "")
        password_created = totp_created = False
        security_setup_status = "skipped"
        login_result = None
        otp_resolver = None
        manual_handle = ManualOtpHandle(
            task_id=logger.task_id, worker_id=f"account_{account_id}",
            worker_label=account.email, registry=manual_otp_registry,
        )
        register_cancel = getattr(logger, "register_cancel_callback", None)

        def check_cancelled():
            if logger.is_cancel_requested():
                raise ChatGPTLoginError(LoginErrorCode.CANCELLED, "任务已取消")

        def authenticate():
            nonlocal otp_resolver
            otp_resolver = _build_email_otp_resolver_from_graph(
                graph=graph, email=account.email, manual_handle=manual_handle,
                email_otp_mode=email_otp_mode, log=logger.log,
                cancel_check=logger.is_cancel_requested,
            )
            return login_chatgpt_web(
                email=account.email, password=password, totp_secret=totp_secret,
                proxy=proxy or None, otp_resolver=otp_resolver,
                manual_totp_handle=manual_handle if not totp_secret else None,
                log=logger.log, cancel_check=logger.is_cancel_requested,
                register_cancel_callback=register_cancel if callable(register_cancel) else None,
            )

        try:
            check_cancelled()
            logger.log(f"开始登录 {account.email}；代理：{proxy or '直连'}")
            login_result = authenticate()
            check_cancelled()
            if not login_result.is_valid():
                raise ChatGPTLoginError(LoginErrorCode.CREDENTIAL_INVALID, "登录未返回经业务接口验证的 Token")
            if setup_security:
                from platforms.chatgpt.ensure_security import ensure_security
                from platforms.chatgpt.token_validation import validate_chatgpt_token
                security = ensure_security(
                    email=account.email, session=login_result,
                    has_password=bool(password), has_totp=bool(totp_secret),
                    proxy=proxy, log=logger.log,
                )
                password_created, totp_created = security.password_created, security.totp_created
                security_setup_status = security.security_setup_status
                # Persist completed security changes even if reauth is later cancelled.
                # Session credentials remain untouched until the final validation passes.
                if password_created or totp_created:
                    if password_created:
                        password = security.new_password
                        account.password = password
                    if totp_created:
                        totp_secret = security.totp_secret
                    patch_account_graph(session, account, credential_updates={
                        "totp_secret": totp_secret, "otpauth": security.otpauth,
                    })
                    session.commit()
                check_cancelled()
                try:
                    validate_chatgpt_token(
                        login_result.access_token, expected_email=account.email, proxy=proxy or None,
                        cancel_check=logger.is_cancel_requested, register_cancel_callback=register_cancel,
                    )
                except ChatGPTLoginError as exc:
                    if exc.code not in {LoginErrorCode.CREDENTIAL_REVOKED, LoginErrorCode.CREDENTIAL_INVALID}:
                        raise
                    logger.log("安全设置后凭据失效，重新认证一次")
                    login_result = authenticate()
            check_cancelled()
            logger.log("身份验证通过，保存新的登录凭据")
            try:
                _persist_credentials(
                    account_id=account_id, account_email=account.email, password=password,
                    totp_secret=totp_secret, login_result=login_result,
                    password_created=password_created, totp_created=totp_created,
                    security_setup_status=security_setup_status, session=session,
                )
            except ChatGPTLoginError:
                raise
            except Exception as exc:
                session.rollback()
                raise ChatGPTLoginError(LoginErrorCode.PERSIST_FAILED, f"凭据保存失败：{type(exc).__name__}") from exc
            logger.log(f"重新登录成功：{account.email}，方式 {login_result.login_method}")
            return LoginRefreshAccountResult(
                account_id, account.email, "succeeded", proxy=proxy,
                login_method=login_result.login_method, token_verified=True,
                access_token_updated=True, session_token_updated=bool(login_result.session_token),
                token_expires_at=login_result.access_token_expires_at,
                security_setup_status=security_setup_status,
                password_created=password_created, totp_created=totp_created,
            )
        except Exception as exc:
            cancelled = logger.is_cancel_requested() or (isinstance(exc, ChatGPTLoginError) and exc.code == LoginErrorCode.CANCELLED)
            code = LoginErrorCode.CANCELLED if cancelled else (exc.code if isinstance(exc, ChatGPTLoginError) else LoginErrorCode.API_RESPONSE_INVALID)
            message = "任务已取消" if cancelled else (exc.message if isinstance(exc, ChatGPTLoginError) else f"登录异常：{type(exc).__name__}")
            session.rollback()
            _record_login_failure(session, account, code, message, cancelled=cancelled)
            logger.log(f"{account.email} 登录{'取消' if cancelled else '失败'}：{message}", level="warning" if cancelled else "error")
            return LoginRefreshAccountResult(
                account_id, account.email, "cancelled" if cancelled else "failed", proxy=proxy,
                login_method=login_result.login_method if login_result else "",
                error_code=code, error_message=message, security_setup_status=security_setup_status,
                password_created=password_created, totp_created=totp_created,
            )
        finally:
            if otp_resolver is not None:
                otp_resolver.close()


def _record_login_failure(session: Session, account: AccountModel, code: str, message: str, *, cancelled=False):
    graph = load_account_graphs(session, [account.id])[account.id]
    auth = dict(graph.get("overview", {}).get("auth") or {})
    auth.update(last_login_attempt_at=datetime.now(timezone.utc).isoformat(),
                last_login_status="cancelled" if cancelled else "failed",
                last_login_error_code=code, last_login_error=message)
    updates = {"auth": auth}
    if code in {LoginErrorCode.CREDENTIAL_INVALID, LoginErrorCode.CREDENTIAL_REVOKED, LoginErrorCode.ANONYMOUS_PROFILE}:
        updates.update(login_required=True, valid=None, check_state="credential_invalid", check_error=message)
    patch_account_graph(session, account, summary_updates=updates)
    session.commit()


def _persist_credentials(
    *, account_id: int, account_email: str, password: str, totp_secret: str,
    login_result: ChatGPTLoginResult, password_created: bool, totp_created: bool,
    security_setup_status: str, session: Session,
) -> None:
    """Atomically replace verified credentials, identity and auth metadata."""
    if not login_result.is_valid():
        raise ChatGPTLoginError(LoginErrorCode.CREDENTIAL_INVALID, "拒绝保存未经业务接口验证的凭据")
    if login_result.verified_email.casefold() != account_email.casefold():
        raise ChatGPTLoginError(LoginErrorCode.IDENTITY_MISMATCH, "拒绝保存其他账号的登录凭据")
    account = session.get(AccountModel, account_id)
    if not account:
        raise ValueError(f"账号 {account_id} 不存在")
    graph = load_account_graphs(session, [account_id])[account_id]
    auth = dict(graph.get("overview", {}).get("auth") or {})
    now = datetime.now(timezone.utc).isoformat()
    auth.update(
        last_login_at=now, last_login_attempt_at=now, last_login_method=login_result.login_method,
        access_token_obtained_at=now, access_token_expires_at=login_result.access_token_expires_at,
        token_exp_source="jwt_exp" if login_result.access_token_expires_at else "",
        token_verified_at=now, verified_email=login_result.verified_email,
        verified_user_id=login_result.verified_user_id,
        last_login_status="succeeded", last_login_error_code="", last_login_error="",
        security_setup_status=security_setup_status, security_setup_requested=security_setup_status != "skipped",
        credential_version=int(auth.get("credential_version") or 0) + 1,
    )
    credentials = login_result.credential_updates()
    existing_keys = {item["key"] for item in graph.get("credentials", []) if item.get("scope") == "platform"}
    for alias in ("legacy_token", "accessToken"):
        if alias in existing_keys:
            credentials[alias] = login_result.access_token
    if "sessionToken" in existing_keys and login_result.session_token:
        credentials["sessionToken"] = login_result.session_token
    if totp_secret:
        credentials["totp_secret"] = totp_secret
    patch_account_graph(
        session, account, primary_token=login_result.access_token,
        credential_updates=credentials,
        summary_updates={"auth": auth, "valid": True, "check_state": "valid", "check_error": "",
                         "login_required": False, "checked_at": now},
    )
    if password:
        account.password = password
    if login_result.account_id:
        account.user_id = login_result.account_id
    account.updated_at = datetime.now(timezone.utc)
    session.commit()
