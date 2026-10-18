from __future__ import annotations

from datetime import datetime, timedelta, timezone

from core.datetime_utils import serialize_datetime
from domain.registration_stats import aggregate_registration_statistics
from infrastructure.registration_stats_repository import RegistrationStatsRepository


class RegistrationStatsService:
    def __init__(self, repository: RegistrationStatsRepository | None = None):
        self.repository = repository or RegistrationStatsRepository()

    def get_statistics(self, *, platform: str = "chatgpt", days: int = 30) -> dict:
        now = datetime.now(timezone.utc)
        since = now - timedelta(days=days) if days > 0 else None
        statistics = aggregate_registration_statistics(
            self.repository.list_outcomes(platform=platform, since=since),
        )
        return {
            **statistics, "platform": platform, "days": days,
            "since": serialize_datetime(since), "updated_at": serialize_datetime(now),
        }
