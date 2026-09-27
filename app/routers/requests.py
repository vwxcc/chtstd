"""
ChatStudio — /api/requests/* (разделы 20-22 ТЗ).
"""

from __future__ import annotations

import asyncio
import json

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai_router import StreamEvent, router_service
from app.auth import enforce_csrf, get_current_user
from app.config import Settings, get_settings
from app.database import AIRequest, Chat, User, get_session
from app.schemas import AIRequestPublic

router = APIRouter(prefix="/api/requests", tags=["requests"])


async def _get_owned_request(session: AsyncSession, request_id: str, user_id: str) -> AIRequest:
    result = await session.execute(
        select(AIRequest).where(AIRequest.id == request_id, AIRequest.user_id == user_id)
    )
    ai_request = result.scalar_one_or_none()
    if not ai_request:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Запрос не найден.")
    return ai_request


@router.get("/{request_id}", response_model=AIRequestPublic)
async def get_request(
    request_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AIRequestPublic:
    ai_request = await _get_owned_request(session, request_id, user.id)
    return AIRequestPublic.model_validate(ai_request)


@router.post("/{request_id}/cancel", response_model=AIRequestPublic)
async def cancel_request(
    request_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AIRequestPublic:
    """Раздел 21: помечает запрос на отмену. Само прерывание генерации
    (между чанками потока) выполняет воркер в app.ai_router — здесь мы
    только выставляем флаг, чтобы не блокировать HTTP-ответ."""
    enforce_csrf(request, settings)
    ai_request = await _get_owned_request(session, request_id, user.id)

    if ai_request.status not in ("queued", "processing"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Запрос уже завершён — отменять нечего.")

    ai_request.cancel_requested = True
    await session.commit()
    await session.refresh(ai_request)
    return AIRequestPublic.model_validate(ai_request)


@router.get("/{request_id}/stream")
async def stream_request(
    request_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> StreamingResponse:
    """SSE-поток дельт генерации (раздел 20). Frontend подписывается сразу
    после отправки сообщения (используя request_id из ответа POST /api/messages)."""
    ai_request = await _get_owned_request(session, request_id, user.id)

    if ai_request.status in ("completed", "failed", "cancelled"):
        async def _final() -> "asyncio.AsyncIterator[str]":
            yield f"event: {ai_request.status}\ndata: {json.dumps({'status': ai_request.status})}\n\n"

        return StreamingResponse(_final(), media_type="text/event-stream")

    queue = router_service.subscribe(request_id)

    async def _events():
        try:
            # Re-check after subscribing to close the completion/subscription race.
            current = await _get_owned_request(session, request_id, user.id)
            if current.status in ("completed", "failed", "cancelled"):
                yield f"event: {current.status}\\ndata: {json.dumps({'status': current.status})}\\n\\n"
                return

            while True:
                try:
                    event: StreamEvent = await asyncio.wait_for(queue.get(), timeout=10)
                except asyncio.TimeoutError:
                    current = await _get_owned_request(session, request_id, user.id)
                    if current.status in ("completed", "failed", "cancelled"):
                        yield f"event: {current.status}\\ndata: {json.dumps({'status': current.status})}\\n\\n"
                        break
                    continue

                if event.kind == "delta":
                    yield f"event: delta\\ndata: {json.dumps({'text': event.text})}\\n\\n"
                elif event.kind == "replace":
                    yield f"event: replace\\ndata: {json.dumps({'text': event.text})}\\n\\n"
                else:
                    yield f"event: {event.kind}\\ndata: {json.dumps({'text': event.text})}\\n\\n"
                    break
        finally:
            router_service.unsubscribe(request_id, queue)

    return StreamingResponse(_events(), media_type="text/event-stream")
