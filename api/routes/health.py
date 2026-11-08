from fastapi import APIRouter

from api.core.health_status import get_health_status

healthRouter = APIRouter()


@healthRouter.get("/health-status")
async def health_status():
    return get_health_status()
