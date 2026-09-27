"""
ChatStudio — AI Router (разделы 20, 21, 22, 24-31, 40, 58 ТЗ).

Отвечает за:
    - разрешение task -> routing set -> упорядоченный список моделей (28, 29)
    - вызов провайдера с потоковой генерацией и fallback при ошибке (24-27, 30)
    - глобальное ограничение конкурентности (58)
    - остановку генерации по запросу пользователя (21)
    - восстановление "зависших" запросов после рестарта сервера (22)
    - постановку задач в очередь через generation.enqueue_generation (22)

Архитектура очереди — простая in-process асинхронная очередь + пул воркеров,
без внешних брокеров (Redis/Celery), так как целевое окружение (раздел 57) —
один Docker-контейнер на ZimaOS. Это сознательное упрощение: при переходе на
несколько инстансов backend'а очередь нужно будет вынести во внешнее хранилище.

НЕ является полноценной production-реализацией: конкретный формат API
провайдера предполагается OpenAI-совместимым (`POST {base_url}/chat/completions`,
SSE `data: {...}`). Если реальный провайдер (например, кастомный Qwen-сервер)
использует другой протокол, адаптер `_call_provider_stream` нужно расширить.
"""

from __future__ import annotations

import asyncio
import base64
import datetime as dt
import json
import logging
import os
from dataclasses import dataclass, field
from typing import AsyncIterator, Optional

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import Settings, get_settings
from app.database import (
    AIRequest,
    AsyncSessionLocal,
    Chat,
    ChatSuggestion,
    FileRecord,
    Message,
    MessageFile,
    ModelConfig,
    Provider,
    Plan,
    RoutingSet,
    RoutingSetModel,
    TaskRoute,
    User,
)

logger = logging.getLogger("chatstudio.ai_router")


CONTINUE_PROMPT = (
    "Продолжи свой предыдущий ответ, не повторяя уже сказанное. "
    "Если ответ был прерван — продолжи с места остановки."
)


class ProviderError(Exception):
    """Ошибка вызова провайдера — повод для fallback (раздел 30)."""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason  # timeout | http_error | connection_error | rate_limit | busy
        self.detail = detail
        super().__init__(f"{reason}: {detail}")


class Cancelled(Exception):
    """Генерация отменена пользователем (раздел 21)."""


# --------------------------------------------------------------------------
# Разрешение маршрутов (разделы 28-29, 33)
# --------------------------------------------------------------------------


async def resolve_models_for_task(session: AsyncSession, task_name: str) -> tuple[Optional[str], list[tuple[Provider, ModelConfig]]]:
    task_route_result = await session.execute(
        select(TaskRoute).where(TaskRoute.task_name == task_name, TaskRoute.enabled.is_(True))
    )
    task_route = task_route_result.scalar_one_or_none()
    if not task_route or not task_route.routing_set_id:
        return None, []

    routing_set = await session.get(RoutingSet, task_route.routing_set_id)

    result = await session.execute(
        select(RoutingSetModel, ModelConfig, Provider)
        .join(ModelConfig, ModelConfig.id == RoutingSetModel.model_config_id)
        .join(Provider, Provider.id == ModelConfig.provider_id)
        .where(
            RoutingSetModel.routing_set_id == task_route.routing_set_id,
            ModelConfig.enabled.is_(True),
            Provider.enabled.is_(True),
        )
        .order_by(RoutingSetModel.priority.asc())
    )
    models = [(provider, model) for _rsm, model, provider in result.all()]
    return (routing_set.name if routing_set else None), models


# --------------------------------------------------------------------------
# Сборка истории сообщений для промпта (разделы 14, 40)
# --------------------------------------------------------------------------


async def _file_content_part(file: FileRecord, settings: Settings) -> Optional[dict]:
    """Готовит файл как часть контента сообщения: текст — инлайном,
    изображение — как data URI (раздел 40), если это включено."""
    if file.extension.lower() in {"png", "jpg", "jpeg", "gif", "webp"}:
        if not settings.ai_send_images:
            return {"type": "text", "text": f"[Изображение «{file.original_name}» не отправлено: AI_SEND_IMAGES=false]"}
        full_path = settings.upload_dir / file.stored_path
        if not full_path.exists():
            return {"type": "text", "text": f"[Файл «{file.original_name}» недоступен на диске]"}
        data = full_path.read_bytes()
        b64 = base64.b64encode(data).decode("ascii")
        return {
            "type": "image_url",
            "image_url": {"url": f"data:{file.mime_type};base64,{b64}"},
        }

    text = file.extracted_text or ""
    text = text[: settings.max_file_context_chars]
    return {"type": "text", "text": f"[Файл «{file.original_name}»]\n{text}"}


async def _web_search(query: str, settings: Settings) -> str:
    if not settings.search_enabled or not settings.search_url:
        return ""
    try:
        async with httpx.AsyncClient(timeout=settings.search_timeout) as client:
            response = await client.get(settings.search_url, params={"q": query[:500], "format": "json", "language": "all"})
            response.raise_for_status()
            data = response.json()
        rows = []
        for item in (data.get("results") or [])[:8]:
            title, url, content = str(item.get("title") or ""), str(item.get("url") or ""), str(item.get("content") or "")
            if title and url:
                rows.append(f"- {title}\n  URL: {url}\n  {content[:500]}")
        return "\n".join(rows)
    except Exception as exc:
        logger.warning("Web search unavailable: %s", exc)
        return ""

async def build_messages(
    session: AsyncSession,
    *,
    chat_id: str,
    up_to_message_id: str,
    settings: Settings,
    append_continue_prompt: bool,
) -> list[dict]:
    result = await session.execute(
        select(Message)
        .where(Message.chat_id == chat_id)
        .order_by(Message.created_at.asc())
    )
    all_messages = result.scalars().all()

    cutoff_index = next((i for i, m in enumerate(all_messages) if m.id == up_to_message_id), len(all_messages) - 1)
    history = all_messages[: cutoff_index + 1]

    payload: list[dict] = []
    for m in history:
        if m.role not in ("user", "assistant", "system"):
            continue

        files_result = await session.execute(
            select(MessageFile, FileRecord)
            .join(FileRecord, FileRecord.id == MessageFile.file_id)
            .where(MessageFile.message_id == m.id)
        )
        file_rows = files_result.all()

        if not file_rows:
            payload.append({"role": m.role, "content": m.content})
            continue

        parts: list[dict] = [{"type": "text", "text": m.content}] if m.content else []
        for _mf, f in file_rows:
            parts.append(await _file_content_part(f, settings))
        payload.append({"role": m.role, "content": parts})

    if append_continue_prompt:
        payload.append({"role": "user", "content": CONTINUE_PROMPT})

    latest_user = next((m for m in reversed(history) if m.role == "user" and m.content), None)
    if latest_user:
        q = latest_user.content.strip()
        trigger_words = ("найди", "поищи", "поиск", "актуаль", "сегодня", "сейчас", "последн", "новости", "источник", "сайт", "url", "https://", "http://")
        if any(word in q.lower() for word in trigger_words):
            results = await _web_search(q, settings)
            if results:
                payload.insert(0, {"role": "system", "content": "Результаты веб-поиска. Используй их как свежие источники и указывай URL:\n" + results})

    return payload


# --------------------------------------------------------------------------
# Вызов провайдера (OpenAI-совместимый streaming chat completion)
# --------------------------------------------------------------------------


async def _call_provider_stream(
    provider: Provider,
    model: ModelConfig,
    messages: list[dict],
    *,
    connection_timeout: float,
    options: dict | None = None,
) -> AsyncIterator[tuple[str, str]]:
    api_key = os.getenv(provider.api_key_env, "")
    url = provider.base_url.rstrip("/") + "/chat/completions"
    options = options or {}
    body = {"model": model.model_name, "messages": messages,
            "temperature": float(options.get("temperature", model.temperature)),
            "max_tokens": max(256, min(int(options.get("max_tokens", model.max_tokens)), model.max_tokens)),
            "stream": True}
    if options.get("top_p") is not None:
        body["top_p"] = float(options["top_p"])
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    timeout = httpx.Timeout(connect=connection_timeout, read=model.timeout, write=connection_timeout, pool=connection_timeout)
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            async with client.stream("POST", url, json=body, headers=headers) as response:
                if response.status_code == 429:
                    raise ProviderError("rate_limit", f"HTTP 429 от {provider.name}")
                if response.status_code in (503, 502):
                    raise ProviderError("busy", f"HTTP {response.status_code} от {provider.name}")
                if response.status_code >= 400:
                    raise ProviderError("http_error", f"HTTP {response.status_code} от {provider.name}")
                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if data == "[DONE]":
                        return
                    try:
                        chunk = json.loads(data)
                        delta_obj = chunk["choices"][0].get("delta", {})
                    except (json.JSONDecodeError, KeyError, IndexError, TypeError):
                        continue
                    # Сырые reasoning tokens/chain-of-thought не выводим в UI.
                    # Интерфейс показывает отдельную анимацию статуса размышления.
                    delta = delta_obj.get("content")
                    if delta:
                        yield "delta", delta
    except httpx.ConnectTimeout as e:
        raise ProviderError("timeout", str(e))
    except httpx.ReadTimeout as e:
        raise ProviderError("timeout", str(e))
    except httpx.ConnectError as e:
        raise ProviderError("connection_error", str(e))
    except httpx.HTTPError as e:
        raise ProviderError("connection_error", str(e))


# --------------------------------------------------------------------------
# Сервис маршрутизации: очередь, воркеры, конкурентность, pub/sub стрима
# --------------------------------------------------------------------------


@dataclass
class StreamEvent:
    kind: str  # "delta" | "thinking" | "replace" | "done" | "error" | "cancelled"
    text: str = ""


class AIRouterService:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[str] = asyncio.Queue()
        self._workers: list[asyncio.Task] = []
        self._subscribers: dict[str, list[asyncio.Queue[StreamEvent]]] = {}
        self._settings: Settings = get_settings()
        self._semaphore = asyncio.Semaphore(self._settings.global_ai_concurrency)

    # ---- публичный API ----------------------------------------------------

    async def submit(self, ai_request_id: str) -> None:
        await self._queue.put(ai_request_id)

    def start(self) -> None:
        if self._workers:
            return
        n = max(1, self._settings.global_ai_concurrency)
        self._workers = [asyncio.create_task(self._worker_loop(i)) for i in range(n)]
        logger.info("AI Router: запущено %d воркеров (GLOBAL_AI_CONCURRENCY=%d)", n, n)

    async def stop(self) -> None:
        for task in self._workers:
            task.cancel()
        self._workers.clear()

    def subscribe(self, ai_request_id: str) -> asyncio.Queue[StreamEvent]:
        q: asyncio.Queue[StreamEvent] = asyncio.Queue()
        self._subscribers.setdefault(ai_request_id, []).append(q)
        return q

    def unsubscribe(self, ai_request_id: str, q: asyncio.Queue[StreamEvent]) -> None:
        subs = self._subscribers.get(ai_request_id, [])
        if q in subs:
            subs.remove(q)
        if not subs:
            self._subscribers.pop(ai_request_id, None)

    def _publish(self, ai_request_id: str, event: StreamEvent) -> None:
        for q in self._subscribers.get(ai_request_id, []):
            q.put_nowait(event)

    # ---- внутренняя обработка ---------------------------------------------

    async def _worker_loop(self, worker_id: int) -> None:
        while True:
            ai_request_id = await self._queue.get()
            async with self._semaphore:
                try:
                    await self._process(ai_request_id)
                except Exception:  # воркер не должен падать насовсем
                    logger.exception("AI Router: ошибка обработки запроса %s", ai_request_id)

    async def _is_cancelled(self, session: AsyncSession, ai_request_id: str) -> bool:
        result = await session.execute(select(AIRequest.cancel_requested).where(AIRequest.id == ai_request_id))
        row = result.scalar_one_or_none()
        return bool(row)

    async def _process(self, ai_request_id: str) -> None:
        async with AsyncSessionLocal() as session:
            ai_request = await session.get(AIRequest, ai_request_id)
            if not ai_request or ai_request.status != "queued":
                return

            ai_request.status = "processing"
            ai_request.started_at = dt.datetime.now(dt.timezone.utc)
            await session.commit()

            chat = await session.get(Chat, ai_request.chat_id)
            if not chat:
                ai_request.status = "failed"
                ai_request.error = "Чат не найден."
                await session.commit()
                return

            if ai_request.task == "main_generation":
                await self._process_main_generation(session, ai_request, chat)
            elif ai_request.task == "title_generation":
                await self._process_title_generation(session, ai_request, chat)
            elif ai_request.task == "suggestions_generation":
                await self._process_suggestions_generation(session, ai_request, chat)
            else:
                ai_request.status = "failed"
                ai_request.error = f"Неизвестная задача: {ai_request.task}"
                await session.commit()

    async def _run_with_fallback(
        self,
        session: AsyncSession,
        ai_request: AIRequest,
        models: list[tuple[Provider, ModelConfig]],
        messages: list[dict],
        on_delta,
        on_thinking=None,
        options: dict | None = None,
    ) -> tuple[bool, str, Optional[Provider], Optional[ModelConfig], list[dict]]:
        """Общий цикл перебора моделей с fallback (раздел 30).
        on_delta(text) вызывается на каждый кусок потока, если он не None."""
        attempts: list[dict] = []
        accumulated = ""

        for attempt_index, (provider, model) in enumerate(models):
            if await self._is_cancelled(session, ai_request.id):
                return False, accumulated, None, None, attempts
            try:
                if attempt_index > 0 and accumulated:
                    accumulated = ""
                    if on_delta:
                        on_delta("__CHATSTUDIO_RESET__")
                async for kind, piece in _call_provider_stream(
                    provider, model, messages, connection_timeout=self._settings.connection_timeout, options=options
                ):
                    if await self._is_cancelled(session, ai_request.id):
                        raise Cancelled()
                    if kind == "thinking":
                        if on_thinking:
                            on_thinking(piece)
                    else:
                        accumulated += piece
                        if on_delta:
                            on_delta(piece)
                return True, accumulated, provider, model, attempts

            except Cancelled:
                raise

            except ProviderError as e:
                attempts.append(
                    {
                        "provider": provider.name,
                        "model": model.display_name,
                        "reason": e.reason,
                        "detail": e.detail[:300],
                        "at": dt.datetime.now(dt.timezone.utc).isoformat(),
                    }
                )
                logger.warning("Fallback: %s/%s -> %s", provider.name, model.display_name, e.reason)
                continue

        return False, accumulated, None, None, attempts

    async def _process_main_generation(self, session: AsyncSession, ai_request: AIRequest, chat: Chat) -> None:
        trigger_message = await session.get(Message, ai_request.message_id) if ai_request.message_id else None
        if not trigger_message:
            ai_request.status = "failed"
            ai_request.error = "Исходное сообщение не найдено."
            await session.commit()
            return

        is_continuation = trigger_message.role == "assistant"

        if is_continuation:
            assistant_message = trigger_message
        else:
            assistant_message = Message(
                chat_id=chat.id, user_id=ai_request.user_id, role="assistant", content="",
                parent_message_id=trigger_message.id,
            )
            session.add(assistant_message)
            await session.flush()
            ai_request.message_id = assistant_message.id
            await session.commit()

        options = {}
        if ai_request.options_json:
            try:
                options = json.loads(ai_request.options_json)
            except Exception:
                options = {}

        user_plan = (await session.execute(select(Plan).where(Plan.name == "free"))).scalar_one_or_none()
        user = await session.get(User, ai_request.user_id)
        if user:
            selected_plan = (await session.execute(select(Plan).where(Plan.name == (user.plan_name or "free")))).scalar_one_or_none()
            if selected_plan:
                user_plan = selected_plan
        if user_plan:
            options["max_tokens"] = min(int(user_plan.max_tokens), 120000)

        try:
            routing_set_name, models = await resolve_models_for_task(session, ai_request.task)
        except Exception:
            routing_set_name, models = None, []
            logger.exception("Ошибка разрешения routing set для задачи %s", ai_request.task)

        if not models:
            ai_request.status = "failed"
            ai_request.error = "Для этой задачи не настроен рабочий routing set."
            await session.commit()
            self._publish(ai_request.id, StreamEvent("error", ai_request.error))
            return

        messages = await build_messages(
            session,
            chat_id=chat.id,
            up_to_message_id=trigger_message.id,
            settings=self._settings,
            append_continue_prompt=is_continuation,
        )

        def on_thinking(piece: str) -> None:
            self._publish(ai_request.id, StreamEvent("thinking", piece))

        def on_delta(delta: str) -> None:
            if delta == "__CHATSTUDIO_RESET__":
                assistant_message.content = ""
                self._publish(ai_request.id, StreamEvent("replace", ""))
                return
            assistant_message.content = (assistant_message.content or "") + delta
            self._publish(ai_request.id, StreamEvent("delta", delta))

        try:
            success, _text, provider, model, attempts = await self._run_with_fallback(
                session, ai_request, models, messages, on_delta, on_thinking, options
            )
        except Cancelled:
            ai_request.status = "cancelled"
            ai_request.completed_at = dt.datetime.now(dt.timezone.utc)
            await session.commit()
            self._publish(ai_request.id, StreamEvent("cancelled"))
            return

        ai_request.fallback_attempts_json = json.dumps(attempts, ensure_ascii=False)

        if success:
            ai_request.provider = provider.name
            ai_request.model = model.display_name
            ai_request.routing_set = routing_set_name
            ai_request.status = "completed"
            ai_request.completed_at = dt.datetime.now(dt.timezone.utc)
            await session.commit()
            self._publish(ai_request.id, StreamEvent("done"))
            await self._maybe_schedule_followups(session, chat, ai_request)
        else:
            if await self._is_cancelled(session, ai_request.id):
                ai_request.status = "cancelled"
                ai_request.completed_at = dt.datetime.now(dt.timezone.utc)
                await session.commit()
                self._publish(ai_request.id, StreamEvent("cancelled"))
                return
            ai_request.status = "failed"
            ai_request.error = "Все модели маршрута недоступны."
            ai_request.completed_at = dt.datetime.now(dt.timezone.utc)
            await session.commit()
            self._publish(ai_request.id, StreamEvent("error", ai_request.error))

    async def _process_title_generation(self, session: AsyncSession, ai_request: AIRequest, chat: Chat) -> None:
        """Короткое (не потоковое для UI) название чата (раздел 29)."""
        try:
            routing_set_name, models = await resolve_models_for_task(session, "title_generation")
        except Exception:
            routing_set_name, models = None, []

        if not models:
            ai_request.status = "failed"
            ai_request.error = "Для title_generation не настроен routing set."
            await session.commit()
            return

        result = await session.execute(
            select(Message).where(Message.chat_id == chat.id).order_by(Message.created_at.asc()).limit(4)
        )
        first_messages = result.scalars().all()
        history_text = "\n".join(f"{m.role}: {m.content}" for m in first_messages if m.content)
        prompt = [
            {"role": "system", "content": "Придумай короткое название чата (3-6 слов, без кавычек) по содержанию диалога. Ответь только названием."},
            {"role": "user", "content": history_text or "Пустой диалог"},
        ]

        try:
            success, text, provider, model, attempts = await self._run_with_fallback(
                session, ai_request, models, prompt, None
            )
        except Cancelled:
            ai_request.status = "cancelled"
            await session.commit()
            return

        ai_request.fallback_attempts_json = json.dumps(attempts, ensure_ascii=False)
        if success and text.strip():
            chat.title = text.strip().strip('"').strip("«»")[: self._settings.max_name_length]
            ai_request.provider = provider.name
            ai_request.model = model.display_name
            ai_request.routing_set = routing_set_name
            ai_request.status = "completed"
        else:
            ai_request.status = "failed"
            ai_request.error = "Не удалось сгенерировать название."
        ai_request.completed_at = dt.datetime.now(dt.timezone.utc)
        await session.commit()

    async def _process_suggestions_generation(self, session: AsyncSession, ai_request: AIRequest, chat: Chat) -> None:
        """2-4 коротких предложения продолжения диалога (раздел 29)."""
        try:
            routing_set_name, models = await resolve_models_for_task(session, "suggestions_generation")
        except Exception:
            routing_set_name, models = None, []

        if not models:
            ai_request.status = "failed"
            ai_request.error = "Для suggestions_generation не настроен routing set."
            await session.commit()
            return

        result = await session.execute(
            select(Message).where(Message.chat_id == chat.id).order_by(Message.created_at.desc()).limit(6)
        )
        recent = list(reversed(result.scalars().all()))
        history_text = "\n".join(f"{m.role}: {m.content}" for m in recent if m.content)
        prompt = [
            {
                "role": "system",
                "content": (
                    "Предложи 3 коротких (до 8 слов) варианта следующего сообщения пользователя "
                    "по контексту диалога. Каждый вариант — на отдельной строке, без нумерации и кавычек."
                ),
            },
            {"role": "user", "content": history_text or "Пустой диалог"},
        ]

        try:
            success, text, provider, model, attempts = await self._run_with_fallback(
                session, ai_request, models, prompt, None
            )
        except Cancelled:
            ai_request.status = "cancelled"
            await session.commit()
            return

        ai_request.fallback_attempts_json = json.dumps(attempts, ensure_ascii=False)
        if success and text.strip():
            lines = [ln.strip("-• \t") for ln in text.strip().splitlines() if ln.strip()][:4]
            for line in lines:
                session.add(ChatSuggestion(chat_id=chat.id, message_id=ai_request.message_id, suggestion_text=line[:300]))
            ai_request.provider = provider.name
            ai_request.model = model.display_name
            ai_request.routing_set = routing_set_name
            ai_request.status = "completed"
        else:
            ai_request.status = "failed"
            ai_request.error = "Не удалось сгенерировать подсказки."
        ai_request.completed_at = dt.datetime.now(dt.timezone.utc)
        await session.commit()

    async def _maybe_schedule_followups(self, session: AsyncSession, chat: Chat, ai_request: AIRequest) -> None:
        """После успешной main_generation — опционально ставит в очередь
        генерацию названия чата и подсказок (раздел 29)."""
        if ai_request.task != "main_generation":
            return

        if chat.title in ("Новый чат", ""):
            title_request = AIRequest(
                user_id=ai_request.user_id, chat_id=chat.id, message_id=ai_request.message_id,
                task="title_generation", status="queued",
            )
            session.add(title_request)
            await session.flush()
            await self.submit(title_request.id)

        suggestions_request = AIRequest(
            user_id=ai_request.user_id, chat_id=chat.id, message_id=ai_request.message_id,
            task="suggestions_generation", status="queued",
        )
        session.add(suggestions_request)
        await session.flush()
        await self.submit(suggestions_request.id)
        await session.commit()


router_service = AIRouterService()


# --------------------------------------------------------------------------
# Восстановление после рестарта (раздел 22)
# --------------------------------------------------------------------------


async def recover_interrupted_requests() -> int:
    """При старте сервера все queued/processing запросы считаются
    прерванными и переводятся в failed. Возвращает число затронутых строк."""
    async with AsyncSessionLocal() as session:
        result = await session.execute(
            select(AIRequest).where(AIRequest.status.in_(["queued", "processing"]))
        )
        stale = result.scalars().all()
        for req in stale:
            req.status = "failed"
            req.error = "Прервано перезапуском сервера."
            req.completed_at = dt.datetime.now(dt.timezone.utc)
        await session.commit()
        return len(stale)
