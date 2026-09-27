"""ChatStudio authentication and security helpers."""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time
from collections import defaultdict, deque

from fastapi import HTTPException, Request, Response, status
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

from app.config import Settings
from app.database import User, get_session

# In-process rate limiting is sufficient for the single-container ZimaOS deployment.
_rate_hits: dict[str, deque[float]] = defaultdict(deque)


def _serializer(settings: Settings) -> URLSafeTimedSerializer:
    return URLSafeTimedSerializer(settings.session_secret, salt="chatstudio-session")


def client_ip(request: Request) -> str:
    # Do not trust forwarded headers by default; ZimaOS runs the application directly.
    return request.client.host if request.client else "unknown"


def _check_rate(key: str, limit: int, window: int) -> None:
    now = time.monotonic()
    q = _rate_hits[key]
    while q and now - q[0] > window:
        q.popleft()
    if len(q) >= limit:
        raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "Слишком много попыток. Попробуйте позже.")
    q.append(now)


def enforce_login_rate_limit(request: Request, settings: Settings) -> None:
    _check_rate(f"login:{client_ip(request)}", settings.auth_login_limit, settings.auth_rate_window)


def enforce_register_rate_limit(request: Request, settings: Settings) -> None:
    _check_rate(f"register:{client_ip(request)}", settings.auth_register_limit, settings.auth_rate_window)


def normalize_email(email: str) -> str:
    return email.strip().lower()


def validate_registration_input(
    name: str,
    email: str,
    password: str,
    password_confirm: str,
    settings: Settings,
) -> None:
    name = name.strip()
    email = normalize_email(email)
    if not name or len(name) > settings.max_name_length:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Некорректное имя.")
    if len(email) > 255 or "@" not in email or email.startswith("@") or email.endswith("@"):
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Некорректный email.")
    if len(password) < 8:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Пароль должен содержать не менее 8 символов.")
    if len(password) > 256:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Пароль слишком длинный.")
    if password != password_confirm:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "Пароли не совпадают.")


def hash_password(password: str) -> tuple[str, str]:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, n=2**14, r=8, p=1, dklen=64)
    return salt.hex(), digest.hex()


def verify_password(password: str, salt_hex: str, hash_hex: str) -> bool:
    try:
        digest = hashlib.scrypt(
            password.encode("utf-8"), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=64
        )
        return hmac.compare_digest(digest.hex(), hash_hex)
    except (ValueError, TypeError):
        return False


def set_session_cookie(response: Response, user_id: str, settings: Settings) -> None:
    token = _serializer(settings).dumps({"user_id": user_id})
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=settings.session_max_age_days * 86400,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_session_cookie(response: Response, settings: Settings) -> None:
    response.delete_cookie(settings.session_cookie_name, path="/")


def set_csrf_cookie(response: Response, settings: Settings) -> str:
    token = secrets.token_urlsafe(32)
    response.set_cookie(
        "chatstudio_csrf",
        token,
        max_age=settings.session_max_age_days * 86400,
        httponly=False,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )
    return token


def enforce_csrf(request: Request, settings: Settings) -> None:
    if not settings.csrf_protection:
        return
    cookie = request.cookies.get("chatstudio_csrf")
    header = request.headers.get("X-CSRF-Token")
    if not cookie or not header or not hmac.compare_digest(cookie, header):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "CSRF-проверка не пройдена.")


async def get_current_user(request: Request) -> User:
    settings = __import__("app.config", fromlist=["get_settings"]).get_settings()
    token = request.cookies.get(settings.session_cookie_name)
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется авторизация.")
    try:
        data = _serializer(settings).loads(token, max_age=settings.session_max_age_days * 86400)
    except (BadSignature, SignatureExpired):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Сессия недействительна или истекла.")

    user_id = data.get("user_id")
    if not user_id:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Сессия недействительна.")

    async for session in get_session():
        user = await session.get(User, user_id)
        if not user:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Пользователь не найден.")
        return user
    raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Требуется авторизация.")


def require_admin(user: User, settings: Settings) -> None:
    if not settings.is_admin(user.email):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "Доступ только для администратора.")
