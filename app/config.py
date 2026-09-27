"""
ChatStudio — конфигурация приложения.

Все настройки читаются из переменных окружения (.env) через pydantic-settings.
Ничего не хардкодим: лимиты, таймауты, флаги безопасности — всё настраиваемо
через .env, как описано в разделе 54 ТЗ.
"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Имя переменной API-ключа провайдера должно соответствовать этому паттерну (раздел 26 ТЗ)
ENV_VAR_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Сервер ---
    port: int = Field(default=8000, alias="PORT")
    data_dir: Path = Field(default=Path("./data"), alias="DATA_DIR")
    upload_dir: Path = Field(default=Path("./uploads"), alias="UPLOAD_DIR")
    frontend_dir: Path = Field(default=Path("./frontend"), alias="FRONTEND_DIR")

    # --- Сессии / безопасность ---
    session_secret: str = Field(default="", alias="SESSION_SECRET")
    session_cookie_name: str = Field(default="chatstudio_session", alias="SESSION_COOKIE_NAME")
    session_max_age_days: int = Field(default=30, alias="SESSION_MAX_AGE_DAYS")
    session_cookie_secure: bool = Field(default=True, alias="SESSION_COOKIE_SECURE")
    csrf_protection: bool = Field(default=True, alias="CSRF_PROTECTION")

    # --- Rate limiting ---
    auth_rate_window: int = Field(default=900, alias="AUTH_RATE_WINDOW")
    auth_login_limit: int = Field(default=10, alias="AUTH_LOGIN_LIMIT")
    auth_register_limit: int = Field(default=5, alias="AUTH_REGISTER_LIMIT")

    # --- Ограничения ввода ---
    max_name_length: int = Field(default=80, alias="MAX_NAME_LENGTH")
    max_prompt_length: int = Field(default=30000, alias="MAX_PROMPT_LENGTH")
    max_search_length: int = Field(default=200, alias="MAX_SEARCH_LENGTH")

    # --- Файлы ---
    max_file_size: int = Field(default=20 * 1024 * 1024, alias="MAX_FILE_SIZE")
    max_total_file_size: int = Field(default=50 * 1024 * 1024, alias="MAX_TOTAL_FILE_SIZE")
    max_files_per_request: int = Field(default=20, alias="MAX_FILES_PER_REQUEST")
    max_file_context_chars: int = Field(default=500_000, alias="MAX_FILE_CONTEXT_CHARS")

    max_archive_entries: int = Field(default=2000, alias="MAX_ARCHIVE_ENTRIES")
    max_archive_unpacked_size: int = Field(default=100 * 1024 * 1024, alias="MAX_ARCHIVE_UNPACKED_SIZE")

    max_document_pages: int = Field(default=500, alias="MAX_DOCUMENT_PAGES")
    max_spreadsheet_sheets: int = Field(default=100, alias="MAX_SPREADSHEET_SHEETS")
    max_presentation_slides: int = Field(default=500, alias="MAX_PRESENTATION_SLIDES")

    allowed_file_extensions: tuple[str, ...] = (
        "pdf", "docx", "txt", "md", "csv", "xls", "xlsx",
        "ppt", "pptx", "json", "xml", "zip",
        "png", "jpg", "jpeg", "gif", "webp",
    )

    # --- AI / генерация ---
    request_timeout: int = Field(default=300, alias="REQUEST_TIMEOUT")
    connection_timeout: int = Field(default=20, alias="CONNECTION_TIMEOUT")
    global_ai_concurrency: int = Field(default=2, alias="GLOBAL_AI_CONCURRENCY")
    ai_send_images: bool = Field(default=True, alias="AI_SEND_IMAGES")

    # --- Администраторы ---
    admin_emails_raw: str = Field(default="", alias="ADMIN_EMAILS")

    @field_validator("session_secret")
    @classmethod
    def _require_session_secret(cls, v: str) -> str:
        if not v or len(v) < 16:
            raise ValueError(
                "SESSION_SECRET должен быть задан и содержать не менее 16 символов. "
                "Сгенерируйте его, например: python -c \"import secrets;print(secrets.token_hex(32))\""
            )
        return v

    @property
    def admin_emails(self) -> set[str]:
        return {
            e.strip().lower()
            for e in self.admin_emails_raw.split(",")
            if e.strip()
        }

    def is_admin(self, email: str) -> bool:
        return email.strip().lower() in self.admin_emails

    @staticmethod
    def validate_env_var_name(name: str) -> bool:
        """Проверка имени переменной окружения для API-ключа провайдера (раздел 26)."""
        return bool(ENV_VAR_NAME_RE.match(name))

    def ensure_dirs(self) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.upload_dir.mkdir(parents=True, exist_ok=True)

    @property
    def sqlite_path(self) -> Path:
        return self.data_dir / "chatstudio.db"

    @property
    def sqlalchemy_url(self) -> str:
        return f"sqlite+aiosqlite:///{self.sqlite_path}"


@lru_cache
def get_settings() -> Settings:
    settings = Settings()
    settings.ensure_dirs()
    return settings
