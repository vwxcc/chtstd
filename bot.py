"""Private Telegram bot + Render health endpoint for a Python code sandbox."""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import time
import uuid
from collections import deque
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Deque

from fastapi import FastAPI
from telegram import InputFile, Update
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from executor import ExecutionResult, execute_submission
from protocol import ProtocolError, Submission, parse_submission

logging.basicConfig(
    level=os.getenv("LOG_LEVEL", "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("python_sandbox_bot")

BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()


def _parse_ids(*names: str) -> set[int]:
    found: set[int] = set()
    for name in names:
        raw = os.getenv(name, "")
        for part in raw.replace(";", ",").split(","):
            value = part.strip()
            if not value:
                continue
            try:
                user_id = int(value)
            except ValueError:
                logger.warning("Ignoring invalid numeric ID in %s", name)
                continue
            if user_id > 0:
                found.add(user_id)
    return found


ALLOWED_USER_IDS = _parse_ids("ALLOWED_USER_IDS", "ADMIN_USER_IDS")
MAX_QUEUE_SIZE = max(1, min(int(os.getenv("MAX_QUEUE_SIZE", "5")), 20))
MAX_SUBMISSION_BYTES = max(10_000, min(int(os.getenv("MAX_SUBMISSION_BYTES", "900000")), 2_000_000))
MAX_INPUT_FILE_BYTES = max(10_000, min(int(os.getenv("MAX_INPUT_FILE_BYTES", "1000000")), 2_000_000))

START_TEXT = """PYTHON SANDBOX — ПРАВИЛА

Пришли полный ответ DeepSeek одним сообщением. Бот выполнит Python-блоки по порядку, сохранит общую рабочую папку и пришлёт вывод, ошибки и созданные файлы. ИИ API на сервере не используется.

ФОРМАТ ДЛЯ DEEPSEEK

Каждую Python-задачу оформляй так:
[[[PYTHON:main]]]
print("Привет")
[[[END]]]

Для нескольких задач используй разные имена: main, step2, report. Задачи выполняются последовательно в одной рабочей папке, поэтому следующие блоки могут использовать файлы предыдущих.

ДАННЫЕ ДЛЯ input()

Если хочешь задать значения заранее, добавь блок с таким же именем задачи:
[[[INPUT:main]]]
12
8
[[[END]]]

Если блока INPUT нет или значений не хватает, бот спросит данные в Telegram по мере выполнения программы. Отправляй каждое значение отдельным сообщением.

СОЗДАНИЕ ТЕКСТОВЫХ ФАЙЛОВ

[[[FILE:results/report.txt]]]
Содержимое файла UTF-8
[[[END]]]

Путь FILE должен быть относительным и не может содержать две точки подряд (..). Python-код может создавать другие файлы самостоятельно.

ПРАВИЛА ФОРМАТИРОВАНИЯ
• Используй маркеры отдельной строкой, без Markdown-ограждений и пояснений вне блоков.
• Не используй сами строки [[[PYTHON:...]]], [[[INPUT:...]]], [[[FILE:...]]] и [[[END]]] внутри содержимого блоков.
• Для небольшого скрипта разрешён и обычный Python-код без маркеров.
• Если ответ длиннее лимита Telegram, отправь весь код файлом .py или .txt.
• Файлы DOCX/PPTX/XLSX, изображения, SVG, CSV, JSON, архивы и другие результаты создавай кодом — бот вернёт их как вложения.

КОМАНДЫ
/start — эти инструкции
/status — текущая задача и очередь
/cancel — отменить свою текущую или ожидающую задачу
/id — узнать свой Telegram ID

Выполнение ограничено по времени, памяти, размеру и числу файлов. Не отправляй секреты в код."""


@dataclass(slots=True)
class Job:
    job_id: str
    user_id: int
    username: str
    submission: Submission
    submitted_at: float = field(default_factory=time.time)
    cancel_event: asyncio.Event = field(default_factory=asyncio.Event)
    input_queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    waiting_for_input: bool = False
    status_message: object | None = None


class SandboxQueue:
    def __init__(self) -> None:
        self.pending: Deque[Job] = deque()
        self.condition = asyncio.Condition()
        self.active: Job | None = None
        self.worker_task: asyncio.Task | None = None
        self.stopping = False

    async def start(self) -> None:
        self.worker_task = asyncio.create_task(self._worker(), name="sandbox-worker")

    async def stop(self) -> None:
        self.stopping = True
        async with self.condition:
            self.pending.clear()
            self.condition.notify_all()
        if self.active:
            self.active.cancel_event.set()
        if self.worker_task:
            try:
                await asyncio.wait_for(self.worker_task, timeout=5)
            except asyncio.TimeoutError:
                self.worker_task.cancel()
                await asyncio.gather(self.worker_task, return_exceptions=True)

    async def enqueue(self, job: Job) -> int:
        async with self.condition:
            if len(self.pending) >= MAX_QUEUE_SIZE:
                raise OverflowError(f"Очередь заполнена (максимум {MAX_QUEUE_SIZE} ожидающих задач).")
            self.pending.append(job)
            position = len(self.pending) + (1 if self.active else 0)
            self.condition.notify()
            return position

    async def cancel_user(self, user_id: int) -> tuple[int, bool]:
        async with self.condition:
            old_count = len(self.pending)
            self.pending = deque(job for job in self.pending if job.user_id != user_id)
            removed = old_count - len(self.pending)
            running = bool(self.active and self.active.user_id == user_id)
            if running and self.active:
                self.active.cancel_event.set()
            return removed, running

    async def snapshot(self) -> tuple[Job | None, list[Job]]:
        async with self.condition:
            return self.active, list(self.pending)

    async def provide_input(self, user_id: int, value: str) -> bool:
        async with self.condition:
            job = self.active
            if not job or job.user_id != user_id or not job.waiting_for_input:
                return False
            if job.input_queue.full():
                return False
            job.waiting_for_input = False
            job.input_queue.put_nowait(value)
            return True

    async def _take(self) -> Job | None:
        async with self.condition:
            await self.condition.wait_for(lambda: self.pending or self.stopping)
            if self.stopping and not self.pending:
                return None
            return self.pending.popleft()

    async def _set_status(self, job: Job, text: str) -> None:
        status_message = job.status_message
        if status_message is not None:
            try:
                await status_message.edit_text(text, parse_mode=None)  # type: ignore[attr-defined]
            except Exception:
                logger.debug("Unable to edit status message for job %s", job.job_id, exc_info=True)

    async def _worker(self) -> None:
        while True:
            job = await self._take()
            if job is None:
                return
            self.active = job
            try:
                await self._execute_job(job)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Unexpected worker error for job %s", job.job_id)
                await self._set_status(job, f"❌ Задача {job.job_id}: внутренняя ошибка исполнителя. Подробности есть в логах сервиса.")
            finally:
                self.active = None

    async def _execute_job(self, job: Job) -> None:
        await self._set_status(job, f"🟡 Задача {job.job_id} началась. Подготавливаю рабочую папку.")
        context = _BOT_APPLICATION.bot_data.get("application_context")
        if context is None:
            await self._set_status(job, "❌ Бот ещё запускается, задачу не удалось выполнить. Отправь код снова.")
            return
        bot = context.bot

        async def status_callback(text: str) -> None:
            await self._set_status(job, f"⚙️ Задача {job.job_id}\n{text}")

        async def input_callback(prompt: str) -> str:
            job.waiting_for_input = True
            prompt_text = prompt.strip() or "Программа запросила ввод."
            timeout = max(10, min(int(os.getenv("INPUT_TIMEOUT_SECONDS", "120")), 300))
            await bot.send_message(
                chat_id=job.user_id,
                text=f"⌨️ Задача {job.job_id} ждёт input():\n{prompt_text}\n\n"
                     f"Отправь значение отдельным сообщением. Тайм-аут ввода: {timeout} с.\n"
                     f"Для отмены используй /cancel.",
            )
            input_task = asyncio.create_task(job.input_queue.get())
            cancel_task = asyncio.create_task(job.cancel_event.wait())
            try:
                done, _ = await asyncio.wait(
                    {input_task, cancel_task},
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if cancel_task in done and job.cancel_event.is_set():
                    raise TimeoutError("Задача отменена во время ожидания input().")
                if input_task not in done:
                    raise TimeoutError(f"Ввод не получен за {timeout} секунд.")
                answer = input_task.result()
                await bot.send_message(chat_id=job.user_id, text="↳ Ввод принят, продолжаю выполнение.")
                return answer
            finally:
                job.waiting_for_input = False
                for task in (input_task, cancel_task):
                    if not task.done():
                        task.cancel()
                await asyncio.gather(input_task, cancel_task, return_exceptions=True)

        if job.cancel_event.is_set():
            await self._set_status(job, f"🚫 Задача {job.job_id} отменена до запуска.")
            return

        try:
            result = await execute_submission(
                job.submission, job.cancel_event, status_callback, input_callback
            )
            if result.cancelled or job.cancel_event.is_set():
                await self._set_status(job, f"🚫 Задача {job.job_id} отменена.")
                return
            await self._send_results(bot, job, result)
        except Exception:
            logger.exception("Execution failed for job %s", job.job_id)
            await self._set_status(job, f"❌ Не удалось выполнить задачу {job.job_id}. Проверь ограничения или попробуй меньший скрипт.")
        finally:
            try:
                if "result" in locals() and result.workspace:
                    shutil.rmtree(result.workspace, ignore_errors=True)
            except Exception:
                logger.warning("Could not clean workspace for job %s", job.job_id, exc_info=True)

    async def _send_results(self, bot, job: Job, result: ExecutionResult) -> None:
        if result.cancelled:
            await self._set_status(job, f"🚫 Задача {job.job_id} отменена.")
            return
        lines = [f"✅ Задача {job.job_id} завершена", f"Python-блоков: {len(result.tasks)}"]
        for task in result.tasks:
            status = "✅ OK" if task.returncode == 0 and not task.timed_out else ("⏱ тайм-аут" if task.timed_out else f"❌ код {task.returncode}")
            lines.append(f"\n[{task.name}] {status} · {task.duration:.1f} с")
            lines.append("stdout:\n" + (task.stdout or "(пусто)"))
            if task.stderr:
                lines.append("stderr:\n" + task.stderr)
        if not result.tasks:
            lines.append("Python-кода не было; подготовлены FILE-блоки.")
        for warning in result.warnings:
            lines.append("⚠️ " + warning)

        await self._set_status(job, "📦 Отправляю вывод и файлы…")
        output = "\n".join(lines)
        for chunk in _split_text(output, 3500):
            await bot.send_message(chat_id=job.user_id, text=chunk)

        files_sent = 0
        for path in result.artifacts:
            if not path.exists() or not path.is_file():
                continue
            try:
                with path.open("rb") as handle:
                    await bot.send_document(
                        chat_id=job.user_id,
                        document=InputFile(handle, filename=path.name),
                        caption=f"Результат задачи {job.job_id}: {path.name}"[:900],
                        read_timeout=45,
                        write_timeout=45,
                    )
                files_sent += 1
            except Exception as exc:
                logger.warning("Could not deliver artifact %s: %s", path, type(exc).__name__)
                await bot.send_message(chat_id=job.user_id, text=f"Не удалось отправить файл {path.name}. Проверь его размер и формат.")
        ending = f"✅ Выдача завершена. Файлов отправлено: {files_sent}."
        if result.warnings:
            ending += "\nЕсть предупреждения по лимитам файлов — см. отчёт выше."
        await self._set_status(job, ending)


def _split_text(text: str, limit: int) -> list[str]:
    if not text:
        return ["(нет вывода)"]
    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    return chunks


queue_manager = SandboxQueue()
_BOT_APPLICATION: Application


def _is_allowed(update: Update) -> bool:
    user = update.effective_user
    return bool(user and user.id in ALLOWED_USER_IDS)


async def _deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if update.effective_message:
        suffix = f"\nТвой Telegram ID: {user.id}" if user else ""
        await update.effective_message.reply_text(
            "⛔ Доступ закрыт. Владелец должен добавить твой ID в ALLOWED_USER_IDS." + suffix,
        )


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return
    await update.effective_message.reply_text(START_TEXT)


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = update.effective_user
    if user and update.effective_message:
        await update.effective_message.reply_text(f"Твой Telegram ID: {user.id}")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return
    active, pending = await queue_manager.snapshot()
    lines = ["СОСТОЯНИЕ ПЕСОЧНИЦЫ"]
    if active:
        lines.append(f"Выполняется: {active.job_id} (владелец ID {active.user_id})")
    else:
        lines.append("Сейчас ничего не выполняется.")
    lines.append(f"В очереди: {len(pending)}/{MAX_QUEUE_SIZE}")
    for position, job in enumerate(pending[:10], 1):
        lines.append(f"{position}. {job.job_id} — ID {job.user_id}")
    await update.effective_message.reply_text("\n".join(lines))


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return
    user = update.effective_user
    removed, running = await queue_manager.cancel_user(user.id)
    if removed or running:
        bits = []
        if running:
            bits.append("запущенный процесс остановлен или останавливается")
        if removed:
            bits.append(f"убрано задач из очереди: {removed}")
        await update.effective_message.reply_text("Отмена: " + "; ".join(bits) + ".")
    else:
        await update.effective_message.reply_text("У тебя нет активной или ожидающей задачи.")


async def _submit(update: Update, context: ContextTypes.DEFAULT_TYPE, source: str) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return
    user = update.effective_user
    message = update.effective_message
    if not user or not message:
        return
    try:
        encoded_size = len(source.encode("utf-8"))
        if encoded_size > MAX_SUBMISSION_BYTES:
            raise ProtocolError(f"Лимит отправки — {MAX_SUBMISSION_BYTES // 1000} КБ.")
        submission = parse_submission(source)
    except ProtocolError as exc:
        await message.reply_text(f"❌ Формат не принят: {exc}\n\nИсправь код по инструкции /start.")
        return

    job = Job(
        job_id=uuid.uuid4().hex[:8],
        user_id=user.id,
        username=user.username or user.first_name or str(user.id),
        submission=submission,
    )
    try:
        position = await queue_manager.enqueue(job)
    except OverflowError as exc:
        await message.reply_text(f"⏳ {exc} Попробуй отправить код позже.")
        return
    status_message = await message.reply_text(
        f"📥 Задача {job.job_id} принята.\nПозиция в очереди: {position}.\n"
        f"Python-блоков: {len(submission.python_tasks)}; FILE-блоков: {len(submission.files)}.",
    )
    job.status_message = status_message


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    message = update.effective_message
    user = update.effective_user
    text = message.text if message else None
    if text is None:
        return
    if user and await queue_manager.provide_input(user.id, text):
        return
    await _submit(update, context, text)


async def document_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not _is_allowed(update):
        await _deny(update, context)
        return
    message = update.effective_message
    document = message.document if message else None
    if not message or not document:
        return
    filename = (document.file_name or "code.txt").lower()
    if Path(filename).suffix not in {".py", ".txt", ".md", ".code"}:
        await message.reply_text("Отправляй исходный код файлом .py, .txt, .md или .code.")
        return
    if document.file_size and document.file_size > MAX_INPUT_FILE_BYTES:
        await message.reply_text(f"Файл слишком большой. Лимит: {MAX_INPUT_FILE_BYTES // 1000} КБ.")
        return
    try:
        telegram_file = await context.bot.get_file(document.file_id)
        payload = await telegram_file.download_as_bytearray()
        if len(payload) > MAX_INPUT_FILE_BYTES:
            await message.reply_text("Файл превышает установленный лимит.")
            return
        source = bytes(payload).decode("utf-8-sig")
    except UnicodeDecodeError:
        await message.reply_text("Файл должен быть текстовым в кодировке UTF-8.")
        return
    except Exception:
        logger.exception("Could not download source file")
        await message.reply_text("Не удалось прочитать файл из Telegram. Попробуй отправить его ещё раз.")
        return
    await _submit(update, context, source)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _BOT_APPLICATION
    app.state.started_at = time.time()
    await queue_manager.start()
    if BOT_TOKEN:
        if not ALLOWED_USER_IDS:
            logger.error("ALLOWED_USER_IDS and ADMIN_USER_IDS are empty; all code submissions will be denied")
        _BOT_APPLICATION = Application.builder().token(BOT_TOKEN).build()
        _BOT_APPLICATION.bot_data["application_context"] = _BOT_APPLICATION
        _BOT_APPLICATION.add_handler(CommandHandler("start", start_command))
        _BOT_APPLICATION.add_handler(CommandHandler("help", start_command))
        _BOT_APPLICATION.add_handler(CommandHandler("id", id_command))
        _BOT_APPLICATION.add_handler(CommandHandler("status", status_command))
        _BOT_APPLICATION.add_handler(CommandHandler("cancel", cancel_command))
        _BOT_APPLICATION.add_handler(MessageHandler(filters.Document.ALL, document_handler))
        _BOT_APPLICATION.add_handler(MessageHandler(filters.TEXT, text_handler))
        await _BOT_APPLICATION.initialize()
        await _BOT_APPLICATION.start()
        await _BOT_APPLICATION.updater.start_polling(drop_pending_updates=False, poll_interval=0.5)
        _BOT_APPLICATION.bot_data["application_context"] = _BOT_APPLICATION
        app.state.bot_configured = True
        logger.info("Telegram bot started; allowlisted IDs=%d", len(ALLOWED_USER_IDS))
    else:
        _BOT_APPLICATION = None  # type: ignore[assignment]
        app.state.bot_configured = False
        logger.error("TELEGRAM_BOT_TOKEN is missing: health endpoint is up, Telegram polling is disabled")
    try:
        yield
    finally:
        if getattr(app.state, "bot_configured", False):
            try:
                await _BOT_APPLICATION.updater.stop()
                await _BOT_APPLICATION.stop()
                await _BOT_APPLICATION.shutdown()
            except Exception:
                logger.exception("Error while stopping Telegram bot")
        await queue_manager.stop()


app = FastAPI(title="Private Python Sandbox Bot", version="1.0.0", lifespan=lifespan)


@app.get("/")
async def root() -> dict[str, str]:
    return {"service": "private-python-sandbox-bot", "health": "ok"}


@app.get("/healthz")
async def healthz() -> dict[str, object]:
    active, pending = await queue_manager.snapshot()
    return {
        "status": "ok",
        "bot_configured": bool(BOT_TOKEN),
        "allowlisted_users": len(ALLOWED_USER_IDS),
        "active_job": active.job_id if active else None,
        "queued_jobs": len(pending),
        "max_parallel_jobs": 1,
        "uptime_seconds": int(time.time() - getattr(app.state, "started_at", time.time())),
    }
