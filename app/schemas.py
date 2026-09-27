"""
ChatStudio — Pydantic-схемы API (запросы/ответы).

Держим схемы отдельно от SQLAlchemy-моделей (app.database), чтобы никогда
не отдавать наружу внутренние поля (пароль, соль, служебные пути к файлам и т.д.).
"""

from __future__ import annotations

import datetime as dt
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------
# Auth
# --------------------------------------------------------------------------


class RegisterRequest(BaseModel):
    name: str
    email: str
    password: str
    password_confirm: str


class LoginRequest(BaseModel):
    email: str
    password: str


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    email: str
    created_at: dt.datetime
    is_admin: bool = False
    plan_name: str = "free"
    plan_display_name: str = "Free"
    plan_requests_per_day: int = 20
    plan_max_tokens: int = 120000


class CsrfResponse(BaseModel):
    csrf_token: str


# --------------------------------------------------------------------------
# Chats
# --------------------------------------------------------------------------


class ChatCreate(BaseModel):
    title: Optional[str] = None


class ChatRename(BaseModel):
    title: str = Field(min_length=1, max_length=200)


class ChatPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    title: str
    archived: bool
    created_at: dt.datetime
    updated_at: dt.datetime
    is_shared: bool = False
    share_token: Optional[str] = None


class ChatSearchResult(BaseModel):
    chat: ChatPublic
    matched_in: str  # "title" | "message" | "file"
    snippet: Optional[str] = None


class ShareInfo(BaseModel):
    chat_id: str
    token: str
    enabled: bool
    url_path: str


# --------------------------------------------------------------------------
# Messages
# --------------------------------------------------------------------------


class AttachedFileRef(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    original_name: str
    extension: str
    mime_type: str
    size_bytes: int


class MessagePublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chat_id: str
    role: str
    content: str
    model: Optional[str] = None
    provider: Optional[str] = None
    routing_set: Optional[str] = None
    parent_message_id: Optional[str] = None
    created_at: dt.datetime
    files: list[AttachedFileRef] = []


class SendMessageRequest(BaseModel):
    content: str
    file_ids: list[str] = Field(default_factory=list)
    model_id: Optional[str] = None
    temperature: Optional[float] = Field(default=None, ge=0, le=2)
    effort: Optional[str] = Field(default=None, pattern="^(low|medium|high|max)$")
    top_p: Optional[float] = Field(default=None, ge=0.05, le=1)


class EditMessageRequest(BaseModel):
    content: str
    file_ids: list[str] = Field(default_factory=list)


class BranchRequest(BaseModel):
    message_id: str


class AIRequestPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    chat_id: str
    message_id: Optional[str]
    task: str
    status: str
    provider: Optional[str]
    model: Optional[str]
    routing_set: Optional[str]
    error: Optional[str]
    created_at: dt.datetime
    started_at: Optional[dt.datetime]
    completed_at: Optional[dt.datetime]
    options_json: Optional[str] = None


# --------------------------------------------------------------------------
# Subscription plans
# --------------------------------------------------------------------------


class PlanPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    display_name: str
    requests_per_day: int
    max_tokens: int
    max_prompt_length: int
    max_files_per_request: int
    max_total_file_size: int
    enabled: bool


class PlanUpdate(BaseModel):
    display_name: Optional[str] = None
    requests_per_day: Optional[int] = Field(default=None, ge=0)
    max_tokens: Optional[int] = Field(default=None, ge=256, le=200000)
    max_prompt_length: Optional[int] = Field(default=None, ge=1, le=1000000)
    max_files_per_request: Optional[int] = Field(default=None, ge=0, le=100)
    max_total_file_size: Optional[int] = Field(default=None, ge=0, le=1024*1024*1024)
    enabled: Optional[bool] = None


# --------------------------------------------------------------------------
# AI Router admin (раздел 25-33)
# --------------------------------------------------------------------------


class ProviderCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    base_url: str
    api_key_env: str
    enabled: bool = True


class ProviderUpdate(BaseModel):
    name: Optional[str] = None
    base_url: Optional[str] = None
    api_key_env: Optional[str] = None
    enabled: Optional[bool] = None


class ProviderPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    base_url: str
    api_key_env: str
    enabled: bool
    has_api_key: bool = False


class ModelCreate(BaseModel):
    provider_id: str
    display_name: str = Field(min_length=1, max_length=120)
    model_name: str = Field(min_length=1, max_length=200)
    request_prefix: Optional[str] = None
    temperature: float = 0.2
    max_tokens: int = 32000
    timeout: int = 300
    enabled: bool = True


class ModelUpdate(BaseModel):
    display_name: Optional[str] = None
    model_name: Optional[str] = None
    request_prefix: Optional[str] = None
    temperature: Optional[float] = None
    max_tokens: Optional[int] = None
    timeout: Optional[int] = None
    enabled: Optional[bool] = None


class ModelPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    provider_id: str
    display_name: str
    model_name: str
    temperature: float
    max_tokens: int
    timeout: int
    enabled: bool


class RoutingSetCreate(BaseModel):
    name: str = Field(min_length=1, max_length=120)


class RoutingSetModelEntry(BaseModel):
    model_config = ConfigDict(protected_namespaces=())

    model_config_id: str
    priority: int = 0


class RoutingSetEntryPublic(BaseModel):
    model_config = ConfigDict(protected_namespaces=(), from_attributes=True)

    model_config_id: str
    display_name: str
    priority: int
    enabled: bool


class RoutingSetPublic(BaseModel):
    id: str
    name: str
    entries: list[RoutingSetEntryPublic] = []


class TaskRouteAssign(BaseModel):
    routing_set_id: Optional[str] = None
    enabled: bool = True


class TaskRoutePublic(BaseModel):
    task_name: str
    routing_set_id: Optional[str] = None
    routing_set_name: Optional[str] = None
    enabled: bool
