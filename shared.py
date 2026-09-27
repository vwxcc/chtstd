"""
ChatStudio — /api/shared/* (разделы 44-46 ТЗ).

Публичные, неавторизованные эндпоинты. По ссылке можно только СМОТРЕТЬ —
никаких изменений, отправки сообщений или доступа к другим чатам владельца.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.config import Settings, get_settings
from app.database import ChatShare, FileRecord, Message, MessageFile, get_session
from app.schemas import MessagePublic

router = APIRouter(prefix="/api/shared", tags=["shared"])


async def _get_enabled_share(session: AsyncSession, token: str) -> ChatShare:
    result = await session.execute(select(ChatShare).where(ChatShare.token == token))
    share = result.scalar_one_or_none()
    if not share or not share.enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Ссылка недействительна или отключена владельцем.")
    return share


@router.get("/{token}")
async def get_shared_chat(
    token: str,
    session: AsyncSession = Depends(get_session),
) -> dict:
    share = await _get_enabled_share(session, token)

    result = await session.execute(
        select(Message)
        .options(selectinload(Message.message_files).selectinload(MessageFile.file))
        .where(Message.chat_id == share.chat_id)
        .order_by(Message.created_at.asc())
    )
    messages = result.scalars().all()

    return {
        "chat_id": share.chat_id,
        "messages": [
            MessagePublic(
                id=m.id, chat_id=m.chat_id, role=m.role, content=m.content,
                model=m.model, provider=m.provider, routing_set=m.routing_set,
                parent_message_id=m.parent_message_id, created_at=m.created_at,
                files=[
                    {
                        "id": mf.file.id, "original_name": mf.file.original_name,
                        "extension": mf.file.extension, "mime_type": mf.file.mime_type,
                        "size_bytes": mf.file.size_bytes,
                    }
                    for mf in m.message_files
                ],
            )
            for m in messages
        ],
    }


@router.get("/{token}/files/{file_id}/download")
async def download_shared_file(
    token: str,
    file_id: str,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    share = await _get_enabled_share(session, token)

    # файл должен быть привязан именно к сообщению ИМЕННО этого чата —
    # иначе по токену одного чата можно было бы скачать чужой файл по id
    result = await session.execute(
        select(FileRecord)
        .join(MessageFile, MessageFile.file_id == FileRecord.id)
        .join(Message, Message.id == MessageFile.message_id)
        .where(FileRecord.id == file_id, Message.chat_id == share.chat_id)
    )
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл не найден в этом чате.")

    full_path = (settings.upload_dir / f.stored_path).resolve()
    base = settings.upload_dir.resolve()
    if base not in full_path.parents or not full_path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл отсутствует на диске.")

    return FileResponse(path=full_path, filename=f.original_name, media_type=f.mime_type)
