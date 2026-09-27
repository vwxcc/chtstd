from __future__ import annotations

import httpx
from fastapi import APIRouter, Depends, Query
from app.auth import get_current_user
from app.config import Settings, get_settings
from app.database import User

router = APIRouter(prefix="/api/search", tags=["search"])

@router.get("")
async def search_web(
    q: str = Query(..., min_length=1, max_length=500),
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
):
    if not settings.search_enabled or not settings.search_url:
        return {"enabled": False, "results": []}
    try:
        async with httpx.AsyncClient(timeout=settings.search_timeout) as client:
            response = await client.get(
                settings.search_url,
                params={"q": q, "format": "json", "language": "all"},
                headers={"Accept": "application/json"},
            )
            response.raise_for_status()
            data = response.json()
        return {"enabled": True, "results": [
            {"title": str(x.get("title") or ""), "url": str(x.get("url") or ""), "snippet": str(x.get("content") or "")}
            for x in (data.get("results") or [])[:10]
        ]}
    except Exception:
        return {"enabled": True, "results": []}
