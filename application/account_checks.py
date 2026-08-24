from __future__ import annotations

from sqlmodel import Session, select

from application.tasks import (
    create_account_check_all_task,
    create_gcash_check_all_task,
    create_trial_check_all_task,
)
from core.db import AccountModel, engine
from services.task_runtime import task_runtime


class AccountChecksService:
    def _normalize_and_validate_ids(self, platform: str, account_ids: list[int] | None) -> list[int]:
        normalized_ids: list[int] = []
        for raw_id in account_ids or []:
            try:
                account_id = int(raw_id)
            except (TypeError, ValueError) as exc:
                raise ValueError("账号 ID 必须是正整数") from exc
            if account_id <= 0:
                raise ValueError("账号 ID 必须是正整数")
            if account_id not in normalized_ids:
                normalized_ids.append(account_id)
        if normalized_ids:
            with Session(engine) as session:
                rows = session.exec(
                    select(AccountModel.id)
                    .where(AccountModel.platform == platform)
                    .where(AccountModel.id.in_(normalized_ids))
                ).all()
            found = {int(row) for row in rows if row is not None}
            missing = [str(account_id) for account_id in normalized_ids if account_id not in found]
            if missing:
                raise ValueError(f"账号不存在或不属于平台 {platform}: {', '.join(missing)}")
        return normalized_ids

    def check_all_async(
        self,
        platform: str = "chatgpt",
        account_ids: list[int] | None = None,
        proxy: str | None = None,
        check_chain: bool | None = None,
    ) -> dict:
        normalized_platform = str(platform or "chatgpt").strip() or "chatgpt"
        normalized_proxy = str(proxy or "").strip() or None
        normalized_ids = self._normalize_and_validate_ids(normalized_platform, account_ids)

        task_kwargs = {
            "account_ids": normalized_ids,
            "proxy": normalized_proxy,
        }
        if check_chain is not None:
            task_kwargs["check_chain"] = bool(check_chain)
        task = create_account_check_all_task(normalized_platform, **task_kwargs)
        task_runtime.wake_up()
        return task

    def check_trial_async(
        self,
        platform: str = "chatgpt",
        account_ids: list[int] | None = None,
        proxy: str | None = None,
        country: str = "PH",
        check_chain: bool = False,
    ) -> dict:
        normalized_platform = str(platform or "chatgpt").strip() or "chatgpt"
        if normalized_platform != "chatgpt":
            raise ValueError("试用资格检测目前仅支持 ChatGPT")
        normalized_proxy = str(proxy or "").strip()
        if not normalized_proxy:
            raise ValueError("查询试用需要填写代理")
        normalized_country = str(country or "PH").strip().upper()
        from platforms.chatgpt.subscription import CHECKOUT_CURRENCY_BY_COUNTRY
        if normalized_country not in CHECKOUT_CURRENCY_BY_COUNTRY:
            raise ValueError("国家/地区代码不支持")
        normalized_ids = self._normalize_and_validate_ids(normalized_platform, account_ids)
        task = create_trial_check_all_task(
            normalized_platform,
            account_ids=normalized_ids,
            proxy=normalized_proxy,
            country=normalized_country,
            check_chain=bool(check_chain),
        )
        task_runtime.wake_up()
        return task

    def check_gcash_async(
        self,
        platform: str = "chatgpt",
        account_ids: list[int] | None = None,
        proxy: str | None = None,
    ) -> dict:
        normalized_platform = str(platform or "chatgpt").strip() or "chatgpt"
        normalized_proxy = str(proxy or "").strip()
        if not normalized_proxy:
            raise ValueError("GCash 资格检测需要填写菲律宾代理")
        normalized_ids = self._normalize_and_validate_ids(normalized_platform, account_ids)
        task = create_gcash_check_all_task(
            normalized_platform,
            account_ids=normalized_ids,
            proxy=normalized_proxy,
        )
        task_runtime.wake_up()
        return task
