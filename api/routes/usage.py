from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field
from api.core.usage_db import init_usage_tables, record_download, get_usage_summary, get_dataset_rankings
import os
import logging

usageRouter = APIRouter(prefix="/api/usage")
logger = logging.getLogger("pelican-ui.usage")

init_usage_tables()

USER = os.environ.get("USER", "")


class DownloadEvent(BaseModel):
    dataset_name: str = Field(..., min_length=1, max_length=512)
    dataset_path: str = Field(..., min_length=1, max_length=1024)
    category: str = Field("", max_length=256)
    file_count: int = Field(1, ge=1, le=100000)
    size_bytes: int = Field(0, ge=0)


@usageRouter.post("/track-download")
async def track_download(event: DownloadEvent):
    try:
        record_download(
            dataset_name=event.dataset_name,
            dataset_path=event.dataset_path,
            category=event.category,
            user=USER,
            file_count=event.file_count,
            size_bytes=event.size_bytes,
        )
    except Exception:
        logger.exception("Failed to record download event")
        raise HTTPException(status_code=500, detail="Failed to record download")
    return {"ok": True}


@usageRouter.get("/stats")
async def usage_stats(days: int = 30):
    if days < 1 or days > 365:
        raise HTTPException(status_code=400, detail="days must be between 1 and 365")
    try:
        return get_usage_summary(days)
    except Exception:
        logger.exception("Failed to fetch usage stats")
        raise HTTPException(status_code=500, detail="Failed to fetch usage stats")


@usageRouter.get("/rankings")
async def dataset_rankings(limit: int = 10):
    if limit < 1 or limit > 50:
        raise HTTPException(status_code=400, detail="limit must be between 1 and 50")
    try:
        return get_dataset_rankings(limit)
    except Exception:
        logger.exception("Failed to fetch dataset rankings")
        raise HTTPException(status_code=500, detail="Failed to fetch rankings")
