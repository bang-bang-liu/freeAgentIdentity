from __future__ import annotations

from core.datetime_utils import serialize_datetime
from domain.accounts import (
    AccountImportLine,
    AccountQuery,
    AccountRecord,
    AccountStats,
    AccountUpdateCommand,
)
from infrastructure.accounts_repository import AccountsRepository


IMPORT_DELIMITER = "----"


class AccountsService:
    def __init__(self, repository: AccountsRepository | None = None):
        self.repository = repository or AccountsRepository()

    def list_accounts(self, query: AccountQuery) -> dict:
        total, items = self.repository.list(query)
        return {
            "total": total,
            "page": query.page,
            "items": [self._serialize(item) for item in items],
        }

    def get_account(self, account_id: int) -> dict | None:
        item = self.repository.get(account_id)
        return self._serialize(item) if item else None

    def update_account(self, account_id: int, command: AccountUpdateCommand) -> dict | None:
        item = self.repository.update(account_id, command)
        return self._serialize(item) if item else None

    def delete_account(self, account_id: int) -> dict:
        return {"ok": self.repository.delete(account_id)}

    def import_accounts(self, platform: str, lines: list[str]) -> dict:
        parsed: list[AccountImportLine] = []
        for line in lines:
            raw = line.strip()
            if not raw:
                continue
            parts = raw.split(IMPORT_DELIMITER)
            if len(parts) != 3:
                continue
            email, password, totp_secret = (part.strip() for part in parts)
            if not email or "@" not in email or any(char.isspace() for char in email):
                continue
            if not password or not totp_secret:
                continue
            parsed.append(
                AccountImportLine(
                    email=email,
                    password=password,
                    extra={"totp_secret": totp_secret},
                )
            )
        return {"created": self.repository.import_lines(platform, parsed)}

    def get_stats(self) -> dict:
        stats: AccountStats = self.repository.stats()
        return {
            "total": stats.total,
            "by_platform": stats.by_platform,
            "by_status": stats.by_status,
            "by_lifecycle_status": stats.by_lifecycle_status,
            "by_plan_state": stats.by_plan_state,
            "by_validity_status": stats.by_validity_status,
            "by_display_status": stats.by_display_status,
        }

    @staticmethod
    def _serialize(item: AccountRecord) -> dict:
        return {
            "id": item.id,
            "platform": item.platform,
            "email": item.email,
            "password": item.password,
            "user_id": item.user_id,
            "primary_token": item.primary_token,
            "trial_end_time": item.trial_end_time,
            "cashier_url": item.cashier_url,
            "lifecycle_status": item.lifecycle_status,
            "validity_status": item.validity_status,
            "plan_state": item.plan_state,
            "plan_name": item.plan_name,
            "display_status": item.display_status,
            "overview": item.overview,
            "display_summary": item.display_summary,
            "credentials": item.credentials,
            "provider_accounts": item.provider_accounts,
            "provider_resources": item.provider_resources,
            "created_at": serialize_datetime(item.created_at),
            "updated_at": serialize_datetime(item.updated_at),
        }
