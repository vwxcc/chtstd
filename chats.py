"""
ChatStudio — /api/chats/* (разделы 10-13, 19, 44-46 ТЗ).

Везде соблюдается принцип владения данными (раздел 60): любой доступ к
чату проверяется по user_id из сессии, а не по тому, что прислал frontend.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import enforce_csrf, get_current_user
from app.config import Settings, get_settings
from app.database import (
    Chat,
    ChatShare,
    FileRecord,
    Message,
    MessageFile,
    User,
    get_session,
)
from app.schemas import (
    BranchRequest,
    ChatCreate,
    ChatPublic,
    ChatRename,
    ChatSearchResult,
    ShareInfo,
)

router = APIRouter(prefix="/api/chats", tags=["chats"])


async def _get_owned_chat(session: AsyncSession, chat_id: str, user_id: str) -> Chat:
    result = await session.execute(
        select(Chat).where(Chat.id == chat_id, Chat.user_id == user_id)
    )
    chat = result.scalar_one_or_none()
    if not chat:
        # 404, а не 403 — не подтверждаем даже факт существования чужого чата.
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чат не найден.")
    return chat


def _to_public(chat: Chat, share: ChatShare | None = None) -> ChatPublic:
    return ChatPublic(
        id=chat.id,
        title=chat.title,
        archived=chat.archived,
        created_at=chat.created_at,
        updated_at=chat.updated_at,
        is_shared=bool(share and share.enabled),
        share_token=share.token if (share and share.enabled) else None,
    )


@router.post("", response_model=ChatPublic, status_code=status.HTTP_201_CREATED)
async def create_chat(
    payload: ChatCreate,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatPublic:
    enforce_csrf(request, settings)

    title = (payload.title or "Новый чат").strip()[: settings.max_name_length] or "Новый чат"
    chat = Chat(user_id=user.id, title=title)
    session.add(chat)
    await session.commit()
    await session.refresh(chat)
    return _to_public(chat)


@router.get("", response_model=list[ChatPublic])
async def list_chats(
    archived: bool = Query(default=False, description="Показать архивные вместо обычных (раздел 11)"),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[ChatPublic]:
    result = await session.execute(
        select(Chat)
        .options(selectinload(Chat.share))
        .where(Chat.user_id == user.id, Chat.archived == archived)
        .order_by(Chat.updated_at.desc())
    )
    chats = result.scalars().all()
    return [_to_public(c, c.share) for c in chats]


@router.get("/search", response_model=list[ChatSearchResult])
async def search_chats(
    q: str = Query(min_length=1),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> list[ChatSearchResult]:
    """Поиск по названию чата, содержимому сообщений и именам файлов (раздел 13)."""
    q = q.strip()[: settings.max_search_length]
    if not q:
        return []
    like = f"%{q}%"

    results: dict[str, ChatSearchResult] = {}

    # 1) по названию
    title_hits = await session.execute(
        select(Chat)
        .options(selectinload(Chat.share))
        .where(Chat.user_id == user.id, Chat.title.ilike(like))
        .order_by(Chat.updated_at.desc())
    )
    for chat in title_hits.scalars().all():
        results[chat.id] = ChatSearchResult(chat=_to_public(chat, chat.share), matched_in="title")

    # 2) по содержимому сообщений
    msg_hits = await session.execute(
        select(Chat, Message.content)
        .join(Message, Message.chat_id == Chat.id)
        .options(selectinload(Chat.share))
        .where(Chat.user_id == user.id, Message.content.ilike(like))
        .order_by(Chat.updated_at.desc())
    )
    for chat, content in msg_hits.all():
        if chat.id not in results:
            snippet = content[:160]
            results[chat.id] = ChatSearchResult(chat=_to_public(chat, chat.share), matched_in="message", snippet=snippet)

    # 3) по именам прикреплённых файлов
    file_hits = await session.execute(
        select(Chat, FileRecord.original_name)
        .join(Message, Message.chat_id == Chat.id)
        .join(MessageFile, MessageFile.message_id == Message.id)
        .join(FileRecord, FileRecord.id == MessageFile.file_id)
        .options(selectinload(Chat.share))
        .where(Chat.user_id == user.id, FileRecord.original_name.ilike(like))
        .order_by(Chat.updated_at.desc())
    )
    for chat, filename in file_hits.all():
        if chat.id not in results:
            results[chat.id] = ChatSearchResult(chat=_to_public(chat, chat.share), matched_in="file", snippet=filename)

    return list(results.values())


@router.get("/{chat_id}", response_model=ChatPublic)
async def get_chat(
    chat_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatPublic:
    chat = await _get_owned_chat(session, chat_id, user.id)
    share_result = await session.execute(select(ChatShare).where(ChatShare.chat_id == chat.id))
    return _to_public(chat, share_result.scalar_one_or_none())


@router.patch("/{chat_id}", response_model=ChatPublic)
async def rename_chat(
    chat_id: str,
    payload: ChatRename,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatPublic:
    enforce_csrf(request, settings)
    chat = await _get_owned_chat(session, chat_id, user.id)
    chat.title = payload.title.strip()[: settings.max_name_length] or chat.title
    await session.commit()
    await session.refresh(chat)
    return _to_public(chat)


@router.delete("/{chat_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_chat(
    chat_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> None:
    enforce_csrf(request, settings)
    chat = await _get_owned_chat(session, chat_id, user.id)
    await session.delete(chat)  # каскад удалит messages/message_files/ai_requests/share на уровне БД
    await session.commit()


@router.post("/{chat_id}/archive", response_model=ChatPublic)
async def archive_chat(
    chat_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatPublic:
    enforce_csrf(request, settings)
    chat = await _get_owned_chat(session, chat_id, user.id)
    chat.archived = True
    await session.commit()
    await session.refresh(chat)
    return _to_public(chat)


@router.post("/{chat_id}/unarchive", response_model=ChatPublic)
async def unarchive_chat(
    chat_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatPublic:
    enforce_csrf(request, settings)
    chat = await _get_owned_chat(session, chat_id, user.id)
    chat.archived = False
    await session.commit()
    await session.refresh(chat)
    return _to_public(chat)


@router.post("/{chat_id}/branch", response_model=ChatPublic, status_code=status.HTTP_201_CREATED)
async def branch_chat(
    chat_id: str,
    payload: BranchRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ChatPublic:
    """Ветвление диалога (раздел 19): новый приватный чат с историей до
    выбранного сообщения включительно, вместе с привязанными файлами."""
    enforce_csrf(request, settings)
    source_chat = await _get_owned_chat(session, chat_id, user.id)

    anchor_result = await session.execute(
        select(Message).where(Message.id == payload.message_id, Message.chat_id == source_chat.id)
    )
    anchor = anchor_result.scalar_one_or_none()
    if not anchor:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сообщение не найдено в этом чате.")

    history_result = await session.execute(
        select(Message)
        .options(selectinload(Message.message_files))
        .where(Message.chat_id == source_chat.id, Message.created_at <= anchor.created_at)
        .order_by(Message.created_at.asc())
    )
    history = history_result.scalars().all()

    new_chat = Chat(user_id=user.id, title=f"{source_chat.title} (ветка)")
    session.add(new_chat)
    await session.flush()

    id_map: dict[str, str] = {}
    for old_msg in history:
        new_msg = Message(
            chat_id=new_chat.id,
            user_id=user.id,
            role=old_msg.role,
            content=old_msg.content,
            model=old_msg.model,
            provider=old_msg.provider,
            routing_set=old_msg.routing_set,
            parent_message_id=id_map.get(old_msg.parent_message_id) if old_msg.parent_message_id else None,
        )
        session.add(new_msg)
        await session.flush()
        id_map[old_msg.id] = new_msg.id

        for mf in old_msg.message_files:
            session.add(MessageFile(message_id=new_msg.id, file_id=mf.file_id))

    await session.commit()
    await session.refresh(new_chat)
    return _to_public(new_chat)


# --------------------------------------------------------------------------
# Sharing (раздел 44-46)
# --------------------------------------------------------------------------


def _share_url_path(token: str) -> str:
    return f"/share/{token}"


@router.post("/{chat_id}/share", response_model=ShareInfo)
async def create_or_enable_share(
    chat_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ShareInfo:
    enforce_csrf(request, settings)
    chat = await _get_owned_chat(session, chat_id, user.id)

    result = await session.execute(select(ChatShare).where(ChatShare.chat_id == chat.id))
    share = result.scalar_one_or_none()
    if share is None:
        share = ChatShare(chat_id=chat.id, enabled=True)
        session.add(share)
    else:
        share.enabled = True

    await session.commit()
    await session.refresh(share)
    return ShareInfo(chat_id=chat.id, token=share.token, enabled=share.enabled, url_path=_share_url_path(share.token))


@router.get("/{chat_id}/share", response_model=ShareInfo)
async def get_share(
    chat_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ShareInfo:
    chat = await _get_owned_chat(session, chat_id, user.id)
    result = await session.execute(select(ChatShare).where(ChatShare.chat_id == chat.id))
    share = result.scalar_one_or_none()
    if not share:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Для этого чата ещё не создана ссылка.")
    return ShareInfo(chat_id=chat.id, token=share.token, enabled=share.enabled, url_path=_share_url_path(share.token))


@router.delete("/{chat_id}/share", response_model=ShareInfo)
async def disable_share(
    chat_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> ShareInfo:
    """Выключает шаринг (раздел 46) — ссылка перестаёт быть доступной,
    но токен сохраняется на случай повторного включения."""
    enforce_csrf(request, settings)
    chat = await _get_owned_chat(session, chat_id, user.id)
    result = await session.execute(select(ChatShare).where(ChatShare.chat_id == chat.id))
    share = result.scalar_one_or_none()
    if not share:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Для этого чата ещё не создана ссылка.")

    share.enabled = False
    await session.commit()
    await session.refresh(share)
    return ShareInfo(chat_id=chat.id, token=share.token, enabled=share.enabled, url_path=_share_url_path(share.token))
