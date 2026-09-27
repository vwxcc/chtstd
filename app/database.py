"""
ChatStudio — слой данных.

SQLite + SQLAlchemy 2.0 (async, aiosqlite).

Таблицы (раздел 47 ТЗ):
    users, chats, messages, files, message_files,
    providers, model_configs, routing_sets, routing_set_models, task_routes,
    chat_shares, chat_suggestions, ai_requests

Ключевые инварианты:
    - раздел 23: в одном чате одновременно может быть только один активный
      main_generation. Реализовано partial unique index на ai_requests.
    - раздел 50: индексы для routing/поиска/истории/branching.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import AsyncIterator, Optional

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.config import get_settings


def _uuid() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    pass


# --------------------------------------------------------------------------
# Пользователи
# --------------------------------------------------------------------------


class User(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(80), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)

    # scrypt: храним соль и хэш отдельно (раздел 5)
    password_hash: Mapped[str] = mapped_column(String(256), nullable=False)
    password_salt: Mapped[str] = mapped_column(String(64), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chats: Mapped[list["Chat"]] = relationship(back_populates="owner", cascade="all, delete-orphan")
    files: Mapped[list["FileRecord"]] = relationship(back_populates="owner", cascade="all, delete-orphan")


# --------------------------------------------------------------------------
# Чаты
# --------------------------------------------------------------------------


class Chat(Base):
    __tablename__ = "chats"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    title: Mapped[str] = mapped_column(String(200), nullable=False, default="Новый чат")
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    owner: Mapped["User"] = relationship(back_populates="chats")
    messages: Mapped[list["Message"]] = relationship(back_populates="chat", cascade="all, delete-orphan")
    share: Mapped[Optional["ChatShare"]] = relationship(back_populates="chat", cascade="all, delete-orphan", uselist=False)

    __table_args__ = (
        # быстрый список "мои чаты, последние сверху" (раздел 11, 50)
        Index("ix_chats_user_updated", "user_id", "archived", "updated_at"),
    )


class ChatShare(Base):
    """Read-only ссылка на чат (раздел 44-46)."""

    __tablename__ = "chat_shares"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False, unique=True)
    token: Mapped[str] = mapped_column(String(64), nullable=False, unique=True, index=True, default=lambda: uuid.uuid4().hex)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chat: Mapped["Chat"] = relationship(back_populates="share")


# --------------------------------------------------------------------------
# Сообщения / ветвление
# --------------------------------------------------------------------------


class Message(Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    role: Mapped[str] = mapped_column(String(16), nullable=False)  # user | assistant | system
    content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    model: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    provider: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    routing_set: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    # branching (раздел 19, 48)
    parent_message_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL"), nullable=True
    )

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chat: Mapped["Chat"] = relationship(back_populates="messages")
    message_files: Mapped[list["MessageFile"]] = relationship(back_populates="message", cascade="all, delete-orphan")

    __table_args__ = (
        CheckConstraint("role IN ('user','assistant','system')", name="ck_messages_role"),
        Index("ix_messages_chat_created", "chat_id", "created_at"),
        Index("ix_messages_parent", "parent_message_id"),
    )


# --------------------------------------------------------------------------
# Файлы
# --------------------------------------------------------------------------


class FileRecord(Base):
    __tablename__ = "files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)

    original_name: Mapped[str] = mapped_column(String(255), nullable=False)
    stored_path: Mapped[str] = mapped_column(String(500), nullable=False)  # относительный путь внутри UPLOAD_DIR
    extension: Mapped[str] = mapped_column(String(16), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)

    # извлечённый текст (усечённый по MAX_FILE_CONTEXT_CHARS), для поиска и вложения в промпт
    extracted_text: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    owner: Mapped["User"] = relationship(back_populates="files")
    message_files: Mapped[list["MessageFile"]] = relationship(back_populates="file")

    __table_args__ = (
        Index("ix_files_user_created", "user_id", "created_at"),
    )


class MessageFile(Base):
    """Связь сообщение <-> файл (раздел 43). Файл может быть в нескольких сообщениях."""

    __tablename__ = "message_files"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    message_id: Mapped[str] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), nullable=False)
    file_id: Mapped[str] = mapped_column(ForeignKey("files.id", ondelete="RESTRICT"), nullable=False)

    message: Mapped["Message"] = relationship(back_populates="message_files")
    file: Mapped["FileRecord"] = relationship(back_populates="message_files")

    __table_args__ = (
        UniqueConstraint("message_id", "file_id", name="uq_message_file"),
        Index("ix_message_files_file", "file_id"),
    )


# --------------------------------------------------------------------------
# AI Router: providers / models / routing sets / task routes
# --------------------------------------------------------------------------


class Provider(Base):
    __tablename__ = "providers"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    base_url: Mapped[str] = mapped_column(String(500), nullable=False)
    api_key_env: Mapped[str] = mapped_column(String(120), nullable=False)  # имя переменной окружения
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    models: Mapped[list["ModelConfig"]] = relationship(back_populates="provider")


class ModelConfig(Base):
    __tablename__ = "model_configs"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    provider_id: Mapped[str] = mapped_column(ForeignKey("providers.id", ondelete="RESTRICT"), nullable=False)

    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    model_name: Mapped[str] = mapped_column(String(200), nullable=False)  # реальное имя модели у провайдера

    temperature: Mapped[float] = mapped_column(default=0.2)
    max_tokens: Mapped[int] = mapped_column(Integer, default=32000)
    timeout: Mapped[int] = mapped_column(Integer, default=300)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    provider: Mapped["Provider"] = relationship(back_populates="models")
    routing_entries: Mapped[list["RoutingSetModel"]] = relationship(back_populates="model")

    __table_args__ = (
        CheckConstraint("temperature >= 0 AND temperature <= 2", name="ck_model_temperature"),
        CheckConstraint("max_tokens >= 1 AND max_tokens <= 200000", name="ck_model_max_tokens"),
        CheckConstraint("timeout >= 1 AND timeout <= 3600", name="ck_model_timeout"),
    )


class RoutingSet(Base):
    __tablename__ = "routing_sets"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    name: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    entries: Mapped[list["RoutingSetModel"]] = relationship(
        back_populates="routing_set", order_by="RoutingSetModel.priority", cascade="all, delete-orphan"
    )
    task_routes: Mapped[list["TaskRoute"]] = relationship(back_populates="routing_set")


class RoutingSetModel(Base):
    """Модель внутри routing set с приоритетом (раздел 28)."""

    __tablename__ = "routing_set_models"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    routing_set_id: Mapped[str] = mapped_column(ForeignKey("routing_sets.id", ondelete="CASCADE"), nullable=False)
    model_config_id: Mapped[str] = mapped_column(ForeignKey("model_configs.id", ondelete="RESTRICT"), nullable=False)

    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)  # меньше = раньше

    routing_set: Mapped["RoutingSet"] = relationship(back_populates="entries")
    model: Mapped["ModelConfig"] = relationship(back_populates="routing_entries")

    __table_args__ = (
        UniqueConstraint("routing_set_id", "model_config_id", name="uq_routing_set_model"),
        Index("ix_routing_set_priority", "routing_set_id", "priority"),
    )


class TaskRoute(Base):
    """Назначение routing set на задачу (раздел 29): main_generation / title_generation / suggestions_generation."""

    __tablename__ = "task_routes"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    task_name: Mapped[str] = mapped_column(String(60), nullable=False, unique=True)
    routing_set_id: Mapped[Optional[str]] = mapped_column(
        ForeignKey("routing_sets.id", ondelete="RESTRICT"), nullable=True
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    routing_set: Mapped[Optional["RoutingSet"]] = relationship(back_populates="task_routes")

    __table_args__ = (
        Index("ix_task_routes_routing_set", "routing_set_id"),
    )


# --------------------------------------------------------------------------
# AI-запросы (раздел 22, 49)
# --------------------------------------------------------------------------


class AIRequest(Base):
    __tablename__ = "ai_requests"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[Optional[str]] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"), nullable=True)

    task: Mapped[str] = mapped_column(String(60), nullable=False)  # main_generation | title_generation | suggestions_generation
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="queued")

    provider: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)
    model: Mapped[Optional[str]] = mapped_column(String(200), nullable=True)
    routing_set: Mapped[Optional[str]] = mapped_column(String(120), nullable=True)

    fallback_attempts_json: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    error: Mapped[Optional[str]] = mapped_column(Text, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    started_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[Optional[dt.datetime]] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('queued','processing','completed','failed','cancelled')",
            name="ck_ai_requests_status",
        ),
        Index("ix_ai_requests_chat_status", "chat_id", "status"),
        Index("ix_ai_requests_user_created", "user_id", "created_at"),
    )


# раздел 23: в одном чате одновременно может быть только один активный main_generation.
# Partial unique index — стандартными средствами SQLite (sqlite_where), объявляется
# отдельно от __table_args__, так как это условное (не обычное составное) ограничение.
Index(
    "uq_active_main_generation_per_chat",
    AIRequest.__table__.c.chat_id,
    unique=True,
    sqlite_where=(
        (AIRequest.__table__.c.task == "main_generation")
        & (AIRequest.__table__.c.status.in_(["queued", "processing"]))
    ),
)


# --------------------------------------------------------------------------
# Suggestions (раздел 29)
# --------------------------------------------------------------------------


class ChatSuggestion(Base):
    __tablename__ = "chat_suggestions"

    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=_uuid)
    chat_id: Mapped[str] = mapped_column(ForeignKey("chats.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[Optional[str]] = mapped_column(ForeignKey("messages.id", ondelete="CASCADE"), nullable=True)

    suggestion_text: Mapped[str] = mapped_column(String(300), nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_chat_suggestions_chat", "chat_id"),
    )


# --------------------------------------------------------------------------
# Engine / session
# --------------------------------------------------------------------------

_settings = get_settings()

engine = create_async_engine(_settings.sqlalchemy_url, echo=False, future=True)

AsyncSessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def init_db() -> None:
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def get_session() -> AsyncIterator[AsyncSession]:
    async with AsyncSessionLocal() as session:
        yield session
