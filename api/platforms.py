from __future__ import annotations

from fastapi import APIRouter

from application.platforms import PlatformsService

router = APIRouter(prefix="/platforms", tags=["platforms"])
service = PlatformsService()


@router.get("")
def list_platforms():
    return service.list_platforms()
