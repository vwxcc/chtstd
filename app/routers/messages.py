"""
ChatStudio — /api/messages/* (разделы 14-18, 20, 21 ТЗ).

Важно: сама генерация ответа AI (потоковая, с fallback) здесь НЕ выполняется.
Эти эндпоинты создают/изменяют записи messages и ai_requests; обработка
ai_requests в статусе "queued" будет выполняться фоновым воркером
app/ai_router.py (следующий файл по плану). Пока он не подключён, запрос
останется в статусе "queued" — это ожидаемо и явно.
"""

from __future__ import annotations

import datetime as dt

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.auth import enforce_csrf, get_current_user
from app.config import Settings, get_settings
from app.database import AIRequest, Chat, FileRecord, Message, MessageFile, Plan, User, get_session
from app.generation import create_ai_request, enqueue_generation
from app.schemas import (
    AIRequestPublic,
    AttachedFileRef,
    BranchRequest,
    EditMessageRequest,
    MessagePublic,
    SendMessageRequest,
)

router = APIRouter(prefix="/api/messages", tags=["messages"])


async def _get_owned_chat(session: AsyncSession, chat_id: str, user_id: str) -> Chat:
    result = await session.execute(select(Chat).where(Chat.id == chat_id, Chat.user_id == user_id))
    chat = result.scalar_one_or_none()
    if not chat:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Чат не найден.")
    return chat


async def _get_owned_message(session: AsyncSession, message_id: str, user_id: str) -> Message:
    result = await session.execute(
        select(Message)
        .options(selectinload(Message.message_files))
        .join(Chat, Chat.id == Message.chat_id)
        .where(Message.id == message_id, Chat.user_id == user_id)
    )
    message = result.scalar_one_or_none()
    if not message:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Сообщение не найдено.")
    return message


async def _get_user_plan(session: AsyncSession, user: User) -> Plan:
    plan = (await session.execute(select(Plan).where(Plan.name == (user.plan_name or "free")))).scalar_one_or_none()
    if plan is None:
        plan = (await session.execute(select(Plan).where(Plan.name == "free"))).scalar_one_or_none()
    if plan is None:
        raise HTTPException(status.HTTP_500_INTERNAL_SERVER_ERROR, "Тарифы ещё не инициализированы.")
    return plan


async def _enforce_plan_limits(session: AsyncSession, user: User, content: str, file_ids: list[str], settings: Settings) -> Plan:
    plan = await _get_user_plan(session, user)
    if not plan.enabled:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Ваш тариф отключён администратором.")
    if len(content) > min(settings.max_prompt_length, plan.max_prompt_length):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Сообщение длиннее лимита тарифа: {plan.max_prompt_length} символов.")
    if len(file_ids) > min(settings.max_files_per_request, plan.max_files_per_request):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Тариф разрешает не более {plan.max_files_per_request} файлов.")
    since = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
    used = await session.execute(
        select(func.count(AIRequest.id)).where(
            AIRequest.user_id == user.id,
            AIRequest.task == "main_generation",
            AIRequest.created_at >= since,
        )
    )
    if plan.requests_per_day and int(used.scalar() or 0) >= plan.requests_per_day:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, f"Достигнут лимит тарифа: {plan.requests_per_day} запросов за 24 часа.")
    return plan


async def _resolve_and_validate_files(
    session: AsyncSession, file_ids: list[str], user_id: str, settings: Settings
) -> list[FileRecord]:
    if not file_ids:
        return []
    if len(file_ids) > settings.max_files_per_request:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не более {settings.max_files_per_request} файлов за раз.")

    result = await session.execute(
        select(FileRecord).where(FileRecord.id.in_(file_ids), FileRecord.user_id == user_id)
    )
    files = result.scalars().all()
    if len(files) != len(set(file_ids)):
        # часть файлов не найдена или принадлежит другому пользователю (раздел 41)
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Один или несколько файлов не найдены.")

    total_size = sum(f.size_bytes for f in files)
    if total_size > settings.max_total_file_size:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Превышен суммарный размер файлов на сообщение.")

    return files


def _to_public(message: Message) -> MessagePublic:
    return MessagePublic(
        id=message.id,
        chat_id=message.chat_id,
        role=message.role,
        content=message.content,
        model=message.model,
        provider=message.provider,
        routing_set=message.routing_set,
        parent_message_id=message.parent_message_id,
        created_at=message.created_at,
        files=[
            AttachedFileRef(
                id=mf.file.id,
                original_name=mf.file.original_name,
                extension=mf.file.extension,
                mime_type=mf.file.mime_type,
                size_bytes=mf.file.size_bytes,
            )
            for mf in message.message_files
        ],
    )


async def _create_generation_request(
    session: AsyncSession, *, user_id: str, chat: Chat, message: Message | None, options: dict | None = None
) -> AIRequestPublic:
    """Создаёт ai_requests(task='main_generation'); ловит нарушение partial
    unique индекса (раздел 23) и превращает его в понятную 409-ошибку."""
    try:
        ai_request = await create_ai_request(session, user_id=user_id, chat=chat, message=message)
        if options:
            import json
            ai_request.options_json = json.dumps(options, ensure_ascii=False)
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "В этом чате уже выполняется генерация ответа. Дождитесь завершения или остановите её.",
        )
    await session.refresh(ai_request)
    await enqueue_generation(ai_request)
    return AIRequestPublic.model_validate(ai_request)


@router.get("", response_model=list[MessagePublic])
async def list_messages(
    chat_id: str = Query(...),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> list[MessagePublic]:
    await _get_owned_chat(session, chat_id, user.id)  # проверка владения (раздел 60)

    result = await session.execute(
        select(Message)
        .options(selectinload(Message.message_files).selectinload(MessageFile.file))
        .where(Message.chat_id == chat_id)
        .order_by(Message.created_at.asc())
    )
    return [_to_public(m) for m in result.scalars().all()]


@router.post("", status_code=status.HTTP_201_CREATED)
async def send_message(
    payload: SendMessageRequest,
    request: Request,
    chat_id: str = Query(...),
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Отправка нового пользовательского сообщения (раздел 14-15)."""
    enforce_csrf(request, settings)
    chat = await _get_owned_chat(session, chat_id, user.id)

    content = payload.content.strip()
    plan = await _enforce_plan_limits(session, user, content, payload.file_ids, settings)
    files = await _resolve_and_validate_files(session, payload.file_ids, user.id, settings)

    # Разрешаем отправлять только файл без текста.
    if not content and not files:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Добавьте текст или хотя бы один файл.")
    if len(content) > settings.max_prompt_length:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Сообщение длиннее {settings.max_prompt_length} символов.")

    message = Message(chat_id=chat.id, user_id=user.id, role="user", content=content)
    session.add(message)
    await session.flush()

    for f in files:
        session.add(MessageFile(message_id=message.id, file_id=f.id))

    await session.flush()
    chat.updated_at = dt.datetime.now(dt.timezone.utc)
    await session.refresh(message, attribute_names=["message_files"])

    options = {}
    if payload.temperature is not None:
        options["temperature"] = payload.temperature
    if payload.effort:
        options["effort"] = payload.effort
    if payload.top_p is not None:
        options["top_p"] = payload.top_p
    if payload.max_tokens is not None:
        options["max_tokens"] = payload.max_tokens
    ai_request = await _create_generation_request(session, user_id=user.id, chat=chat, message=message, options=options)

    return {"message": _to_public(message), "ai_request": ai_request}


@router.patch("/{message_id}")
async def edit_message(
    message_id: str,
    payload: EditMessageRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Редактирование сообщения пользователя (раздел 16):
    1) меняются текст и attachments этого же message_id,
    2) все последующие сообщения в чате удаляются,
    3) запускается новая генерация ответа.
    """
    enforce_csrf(request, settings)
    message = await _get_owned_message(session, message_id, user.id)

    if message.role != "user":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Редактировать можно только сообщения пользователя.")

    content = payload.content.strip()
    if not content:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Сообщение не может быть пустым.")
    if len(content) > settings.max_prompt_length:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Сообщение длиннее {settings.max_prompt_length} символов.")

    files = await _resolve_and_validate_files(session, payload.file_ids, user.id, settings)

    chat = await _get_owned_chat(session, message.chat_id, user.id)

    # 2) удаляем всё, что шло после этого сообщения
    later = (
        await session.execute(
            select(Message).where(Message.chat_id == chat.id, Message.created_at > message.created_at)
        )
    ).scalars().all()
    for m in later:
        await session.delete(m)

    # 1) обновляем текст и вложения того же message_id
    message.content = content
    for mf in list(message.message_files):
        await session.delete(mf)
    await session.flush()
    for f in files:
        session.add(MessageFile(message_id=message.id, file_id=f.id))

    await session.flush()
    await session.refresh(message, attribute_names=["message_files"])

    # 3) новая генерация
    ai_request = await _create_generation_request(session, user_id=user.id, chat=chat, message=message)

    return {"message": _to_public(message), "ai_request": ai_request}


@router.post("/{message_id}/retry")
async def retry_message(
    message_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Повторная генерация ответа AI (раздел 17): использует исходное
    пользовательское сообщение и его вложения, старый ответ заменяется новым."""
    enforce_csrf(request, settings)
    assistant_message = await _get_owned_message(session, message_id, user.id)

    if assistant_message.role != "assistant":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Retry применим только к ответам AI.")
    if not assistant_message.parent_message_id:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "У этого ответа нет исходного сообщения пользователя.")

    user_message = await _get_owned_message(session, assistant_message.parent_message_id, user.id)
    chat = await _get_owned_chat(session, assistant_message.chat_id, user.id)

    await session.delete(assistant_message)
    await session.flush()

    ai_request = await _create_generation_request(session, user_id=user.id, chat=chat, message=user_message)
    return {"ai_request": ai_request}


@router.post("/{message_id}/continue")
async def continue_message(
    message_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Продолжение ответа AI (раздел 18). ai_router.py при обработке этого
    ai_request (task='main_generation', message_id=существующий assistant
    message) должен дописывать текст в message.content, а не создавать
    новое сообщение, и отправлять модели инструкцию продолжить с места
    остановки, не повторяя уже сказанное."""
    enforce_csrf(request, settings)
    assistant_message = await _get_owned_message(session, message_id, user.id)
    if assistant_message.role != "assistant":
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Continue применим только к ответам AI.")

    chat = await _get_owned_chat(session, assistant_message.chat_id, user.id)
    ai_request = await _create_generation_request(session, user_id=user.id, chat=chat, message=assistant_message)
    return {"ai_request": ai_request}


@router.post("/{message_id}/branch", status_code=status.HTTP_201_CREATED)
async def branch_from_message(
    message_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Тонкая обёртка над POST /api/chats/{chat_id}/branch (раздел 19) —
    удобный вызов прямо из контекста сообщения."""
    message = await _get_owned_message(session, message_id, user.id)
    from app.routers.chats import branch_chat  # локальный импорт во избежание цикла

    return await branch_chat(
        message.chat_id,
        BranchRequest(message_id=message_id),
        request,
        settings,
        user,
        session,
    )
