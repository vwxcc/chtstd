"""
ChatStudio — /api/auth/* (разделы 4, 5, 6 ТЗ).
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import (
    client_ip,
    clear_session_cookie,
    enforce_csrf,
    enforce_register_rate_limit,
    get_current_user,
    hash_password,
    normalize_email,
    set_csrf_cookie,
    set_session_cookie,
    validate_registration_input,
    verify_password,
)
from app.config import Settings, get_settings
from app.database import User, get_session
from app.schemas import CsrfResponse, LoginRequest, RegisterRequest, UserPublic

router = APIRouter(prefix="/api/auth", tags=["auth"])


PLAN_LABELS = {"free": "Free", "plus": "Plus", "pro": "Pro", "max": "Max"}

def _to_public(user: User, settings: Settings) -> UserPublic:
    plan_name = getattr(user, "plan_name", "free") or "free"
    return UserPublic(
        id=user.id,
        name=user.name,
        email=user.email,
        created_at=user.created_at,
        is_admin=settings.is_admin(user.email),
        plan_name=plan_name,
        plan_display_name=PLAN_LABELS.get(plan_name, plan_name.title()),
        plan_requests_per_day=0,
        plan_max_tokens=120000,
    )


@router.get("/csrf", response_model=CsrfResponse)
async def get_csrf_token(response: Response, settings: Settings = Depends(get_settings)) -> CsrfResponse:
    """Frontend вызывает это до отправки любых POST/PUT/PATCH/DELETE запросов
    (в т.ч. до логина/регистрации), чтобы получить CSRF cookie + токен."""
    token = set_csrf_cookie(response, settings)
    return CsrfResponse(csrf_token=token)


@router.post("/register", response_model=UserPublic, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> UserPublic:
    enforce_csrf(request, settings)
    enforce_register_rate_limit(request, settings)

    validate_registration_input(
        payload.name, payload.email, payload.password, payload.password_confirm, settings
    )

    email = normalize_email(payload.email)
    existing = await session.execute(select(User.id).where(User.email == email))
    if existing.scalar_one_or_none() is not None:
        # Не уточняем "email уже занят" отдельным кодом ошибки без причины —
        # это осознанный компромисс UX (раздел 4) vs enumeration; ТЗ явно
        # требует сообщать о занятой регистрации (раздел 53), поэтому сообщаем.
        raise HTTPException(status.HTTP_409_CONFLICT, "Этот email уже зарегистрирован.")

    salt_hex, hash_hex = hash_password(payload.password)
    user = User(name=payload.name.strip(), email=email, password_hash=hash_hex, password_salt=salt_hex)
    session.add(user)

    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Этот email уже зарегистрирован.")

    await session.refresh(user)

    set_session_cookie(response, user.id, settings)
    # Не ротируем CSRF-токен здесь: браузер уже получил его через /api/auth/csrf,
    # а клиент продолжает отправлять именно этот токен после регистрации.
    return _to_public(user, settings)


@router.post("/login", response_model=UserPublic)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> UserPublic:
    enforce_csrf(request, settings)
    email = normalize_email(payload.email)
    result = await session.execute(select(User).where(User.email == email))
    user = result.scalar_one_or_none()

    if not user or not verify_password(payload.password, user.password_salt, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Неверный email или пароль.")

    set_session_cookie(response, user.id, settings)
    # CSRF-токен не ротируем после логина: клиент уже держит токен,
    # полученный через /api/auth/csrf, и использует его для следующих запросов.
    return _to_public(user, settings)


@router.post("/logout")
async def logout(
    request: Request,
    response: Response,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
) -> dict:
    enforce_csrf(request, settings)
    clear_session_cookie(response, settings)
    return {"ok": True}


@router.get("/me", response_model=UserPublic)
async def me(user: User = Depends(get_current_user), settings: Settings = Depends(get_settings)) -> UserPublic:
    return _to_public(user, settings)
