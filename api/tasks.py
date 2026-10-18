from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from application.registration_stats import RegistrationStatsService
from application.tasks_query import TasksQueryService

router = APIRouter(prefix="/tasks", tags=["tasks"])
service = TasksQueryService()
stats_service = RegistrationStatsService()


@router.get("/register/stats")
def registration_statistics(platform: str = "chatgpt", days: int = Query(default=30, ge=0, le=3650)):
    return stats_service.get_statistics(platform=platform, days=days)


@router.get("/{task_id}")
def get_task(task_id: str):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return task


@router.get("/{task_id}/events")
def list_task_events(task_id: str, since: int = 0, limit: int = 200):
    task = service.get_task(task_id)
    if not task:
        raise HTTPException(404, "任务不存在")
    return service.list_events(task_id, since=since, limit=limit)
