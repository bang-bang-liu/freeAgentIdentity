from __future__ import annotations

import json
from datetime import datetime

from sqlmodel import Session, select

from core.db import TaskModel, engine


class RegistrationStatsRepository:
    def list_outcomes(self, *, platform: str, since: datetime | None = None) -> list[dict]:
        # Read only the fields needed for aggregates, never task payload/credentials.
        statement = select(
            TaskModel.status, TaskModel.success_count, TaskModel.error_count,
            TaskModel.progress_total, TaskModel.result_json, TaskModel.error,
        ).where(TaskModel.type == "register", TaskModel.platform == platform)
        if since is not None:
            statement = statement.where(TaskModel.created_at >= since)
        with Session(engine) as session:
            return [
                {
                    "status": status, "success": success, "error_count": failed,
                    "requested": requested, "result": json.loads(result_json or "{}"), "error": error,
                }
                for status, success, failed, requested, result_json, error in session.exec(statement)
            ]
