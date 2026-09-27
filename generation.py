"""
ChatStudio — создание AI-запросов (раздел 22 ТЗ).

Этот модуль отвечает только за создание записи ai_requests в статусе
"queued" и её постановку в очередь. Сама логика вызова провайдера,
маршрутизации и fallback (разделы 24-31) будет реализована в app/ai_router.py
одним из следующих файлов — здесь оставлена явная точка расширения
(`enqueue_generation`), чтобы routers/messages.py уже сейчас был рабочим
и не зависел от ещё не написанного кода.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.database import AIRequest, Chat, Message


async def create_ai_request(
    session: AsyncSession,
    *,
    user_id: str,
    chat: Chat,
    message: Message | None,
    task: str = "main_generation",
) -> AIRequest:
    """Создаёт запись ai_requests со статусом 'queued'.

    Раздел 23: уникальность активного main_generation на чат гарантируется
    partial unique индексом в БД (uq_active_main_generation_per_chat) —
    если такой запрос уже есть, insert упадёт с IntegrityError, которую
    обязан обработать вызывающий код (routers/messages.py) и вернуть 409.
    """
    ai_request = AIRequest(
        user_id=user_id,
        chat_id=chat.id,
        message_id=message.id if message else None,
        task=task,
        status="queued",
    )
    session.add(ai_request)
    await session.flush()
    return ai_request


async def enqueue_generation(ai_request: AIRequest) -> None:
    """Ставит запрос в очередь AI Router (app.ai_router.router_service).

    Локальный импорт — чтобы избежать циклической зависимости
    (ai_router.py не импортирует generation.py, но порядок инициализации
    модулей в приложении не гарантирован)."""
    from app.ai_router import router_service

    await router_service.submit(ai_request.id)
