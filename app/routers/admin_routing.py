"""
ChatStudio — /api/routing/* — админ-панель AI Router (разделы 24-33 ТЗ).

Доступно только администраторам (require_admin). Раздел 33 — защитные
инварианты соблюдаются явными проверками перед каждым изменением/удалением.
"""

from __future__ import annotations

import os

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import enforce_csrf, get_current_user, require_admin
from app.config import Settings, get_settings
from app.database import (
    ModelConfig,
    Provider,
    RoutingSet,
    RoutingSetModel,
    TaskRoute,
    User,
    get_session,
)
from app.schemas import (
    ModelCreate,
    ModelPublic,
    ModelUpdate,
    ProviderCreate,
    ProviderPublic,
    ProviderUpdate,
    RoutingSetCreate,
    RoutingSetEntryPublic,
    RoutingSetModelEntry,
    RoutingSetPublic,
    TaskRoutePublic,
    TaskRouteAssign,
)

router = APIRouter(prefix="/api/routing", tags=["routing-admin"])

VALID_TASK_NAMES = {"main_generation", "title_generation", "suggestions_generation"}


async def _admin(user: User = Depends(get_current_user), settings: Settings = Depends(get_settings)) -> User:
    require_admin(user, settings)
    return user


# --------------------------------------------------------------------------
# Providers (раздел 25-26, 33)
# --------------------------------------------------------------------------


def _provider_public(p: Provider) -> ProviderPublic:
    return ProviderPublic(
        id=p.id, name=p.name, base_url=p.base_url, api_key_env=p.api_key_env,
        enabled=p.enabled, has_api_key=bool(os.getenv(p.api_key_env)),
    )


@router.post("/providers", response_model=ProviderPublic, status_code=status.HTTP_201_CREATED)
async def create_provider(
    payload: ProviderCreate,
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> ProviderPublic:
    enforce_csrf(request, settings)

    if not payload.base_url.startswith(("http://", "https://")):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "base_url должен начинаться с http:// или https://.")
    if not Settings.validate_env_var_name(payload.api_key_env):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Имя переменной API-ключа должно соответствовать [A-Za-z_][A-Za-z0-9_]*.")

    provider = Provider(
        name=payload.name.strip(), base_url=payload.base_url.strip(),
        api_key_env=payload.api_key_env.strip(), enabled=payload.enabled,
    )
    session.add(provider)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Провайдер с таким именем уже существует.")
    await session.refresh(provider)
    return _provider_public(provider)


@router.get("/providers", response_model=list[ProviderPublic])
async def list_providers(
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ProviderPublic]:
    result = await session.execute(select(Provider).order_by(Provider.created_at.asc()))
    return [_provider_public(p) for p in result.scalars().all()]


@router.patch("/providers/{provider_id}", response_model=ProviderPublic)
async def update_provider(
    provider_id: str,
    payload: ProviderUpdate,
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> ProviderPublic:
    enforce_csrf(request, settings)
    provider = await session.get(Provider, provider_id)
    if not provider:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Провайдер не найден.")

    if payload.base_url is not None:
        if not payload.base_url.startswith(("http://", "https://")):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "base_url должен начинаться с http:// или https://.")
        provider.base_url = payload.base_url.strip()
    if payload.api_key_env is not None:
        if not Settings.validate_env_var_name(payload.api_key_env):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Недопустимое имя переменной API-ключа.")
        provider.api_key_env = payload.api_key_env.strip()
    if payload.name is not None:
        provider.name = payload.name.strip()
    if payload.enabled is not None:
        provider.enabled = payload.enabled

    await session.commit()
    await session.refresh(provider)
    return _provider_public(provider)


@router.delete("/providers/{provider_id}", status_code=status.HTTP_200_OK)
async def delete_provider(
    provider_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    enforce_csrf(request, settings)
    provider = await session.get(Provider, provider_id)
    if not provider:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Провайдер не найден.")

    in_use = await session.execute(select(ModelConfig.id).where(ModelConfig.provider_id == provider.id).limit(1))
    if in_use.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Провайдер используется моделями — сначала удалите/перепривяжите их.")

    await session.delete(provider)
    await session.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# Models (раздел 27, 33)
# --------------------------------------------------------------------------


def _model_public(m: ModelConfig) -> ModelPublic:
    return ModelPublic(
        id=m.id, provider_id=m.provider_id, display_name=m.display_name, model_name=m.model_name,
        temperature=m.temperature, max_tokens=m.max_tokens, timeout=m.timeout, enabled=m.enabled,
    )


def _validate_model_limits(temperature: float, max_tokens: int, timeout: int) -> None:
    if not (0 <= temperature <= 2):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "temperature должен быть в диапазоне 0-2.")
    if not (1 <= max_tokens <= 200_000):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "max_tokens должен быть в диапазоне 1-200000.")
    if not (1 <= timeout <= 3600):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "timeout должен быть в диапазоне 1-3600 секунд.")


@router.post("/models", response_model=ModelPublic, status_code=status.HTTP_201_CREATED)
async def create_model(
    payload: ModelCreate,
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> ModelPublic:
    enforce_csrf(request, settings)
    _validate_model_limits(payload.temperature, payload.max_tokens, payload.timeout)

    provider = await session.get(Provider, payload.provider_id)
    if not provider:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Провайдер не найден.")

    model = ModelConfig(
        provider_id=provider.id, display_name=payload.display_name.strip(), model_name=payload.model_name.strip(),
        temperature=payload.temperature, max_tokens=payload.max_tokens, timeout=payload.timeout, enabled=payload.enabled,
    )
    session.add(model)
    await session.commit()
    await session.refresh(model)
    return _model_public(model)


@router.get("/models", response_model=list[ModelPublic])
async def list_models(
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> list[ModelPublic]:
    result = await session.execute(select(ModelConfig).order_by(ModelConfig.created_at.asc()))
    return [_model_public(m) for m in result.scalars().all()]


@router.patch("/models/{model_id}", response_model=ModelPublic)
async def update_model(
    model_id: str,
    payload: ModelUpdate,
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> ModelPublic:
    enforce_csrf(request, settings)
    model = await session.get(ModelConfig, model_id)
    if not model:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Модель не найдена.")

    new_temp = payload.temperature if payload.temperature is not None else model.temperature
    new_max_tokens = payload.max_tokens if payload.max_tokens is not None else model.max_tokens
    new_timeout = payload.timeout if payload.timeout is not None else model.timeout
    _validate_model_limits(new_temp, new_max_tokens, new_timeout)

    if payload.display_name is not None:
        model.display_name = payload.display_name.strip()
    if payload.model_name is not None:
        model.model_name = payload.model_name.strip()
    model.temperature = new_temp
    model.max_tokens = new_max_tokens
    model.timeout = new_timeout
    if payload.enabled is not None:
        model.enabled = payload.enabled

    await session.commit()
    await session.refresh(model)
    return _model_public(model)


@router.delete("/models/{model_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_model(
    model_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    enforce_csrf(request, settings)
    model = await session.get(ModelConfig, model_id)
    if not model:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Модель не найдена.")

    in_use = await session.execute(
        select(RoutingSetModel.id).where(RoutingSetModel.model_config_id == model.id).limit(1)
    )
    if in_use.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Модель используется в routing set — сначала удалите её оттуда.")

    await session.delete(model)
    await session.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# Routing sets (раздел 28, 33)
# --------------------------------------------------------------------------


async def _routing_set_public(session: AsyncSession, rs: RoutingSet) -> RoutingSetPublic:
    result = await session.execute(
        select(RoutingSetModel, ModelConfig)
        .join(ModelConfig, ModelConfig.id == RoutingSetModel.model_config_id)
        .where(RoutingSetModel.routing_set_id == rs.id)
        .order_by(RoutingSetModel.priority.asc())
    )
    entries = [
        RoutingSetEntryPublic(
            model_config_id=model.id, display_name=model.display_name,
            priority=rsm.priority, enabled=model.enabled,
        )
        for rsm, model in result.all()
    ]
    return RoutingSetPublic(id=rs.id, name=rs.name, entries=entries)


@router.post("/routing-sets", response_model=RoutingSetPublic, status_code=status.HTTP_201_CREATED)
async def create_routing_set(
    payload: RoutingSetCreate,
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> RoutingSetPublic:
    enforce_csrf(request, settings)
    rs = RoutingSet(name=payload.name.strip())
    session.add(rs)
    try:
        await session.commit()
    except IntegrityError:
        await session.rollback()
        raise HTTPException(status.HTTP_409_CONFLICT, "Routing set с таким именем уже существует.")
    await session.refresh(rs)
    return await _routing_set_public(session, rs)


@router.get("/routing-sets", response_model=list[RoutingSetPublic])
async def list_routing_sets(
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> list[RoutingSetPublic]:
    result = await session.execute(select(RoutingSet).order_by(RoutingSet.created_at.asc()))
    return [await _routing_set_public(session, rs) for rs in result.scalars().all()]


@router.put("/routing-sets/{routing_set_id}/models", response_model=RoutingSetPublic)
async def set_routing_set_models(
    routing_set_id: str,
    entries: list[RoutingSetModelEntry],
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> RoutingSetPublic:
    """Полностью переопределяет состав и порядок моделей в routing set."""
    enforce_csrf(request, settings)
    rs = await session.get(RoutingSet, routing_set_id)
    if not rs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Routing set не найден.")

    model_ids = [e.model_config_id for e in entries]
    if model_ids:
        found = await session.execute(select(ModelConfig.id).where(ModelConfig.id.in_(model_ids)))
        if len(found.scalars().all()) != len(set(model_ids)):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Одна или несколько моделей не найдены.")

    existing = await session.execute(select(RoutingSetModel).where(RoutingSetModel.routing_set_id == rs.id))
    for row in existing.scalars().all():
        await session.delete(row)
    await session.flush()

    for entry in entries:
        session.add(RoutingSetModel(routing_set_id=rs.id, model_config_id=entry.model_config_id, priority=entry.priority))

    await session.commit()
    await session.refresh(rs)
    return await _routing_set_public(session, rs)


@router.delete("/routing-sets/{routing_set_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_routing_set(
    routing_set_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    enforce_csrf(request, settings)
    rs = await session.get(RoutingSet, routing_set_id)
    if not rs:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Routing set не найден.")

    in_use = await session.execute(select(TaskRoute.id).where(TaskRoute.routing_set_id == rs.id).limit(1))
    if in_use.scalar_one_or_none() is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "Routing set используется в task route — сначала отвяжите его.")

    await session.delete(rs)
    await session.commit()
    return {"ok": True}


# --------------------------------------------------------------------------
# Task routes (раздел 29, 33)
# --------------------------------------------------------------------------


@router.get("/task-routes", response_model=list[TaskRoutePublic])
async def list_task_routes(
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> list[TaskRoutePublic]:
    result = await session.execute(select(TaskRoute))
    by_name = {tr.task_name: tr for tr in result.scalars().all()}

    out: list[TaskRoutePublic] = []
    for task_name in VALID_TASK_NAMES:
        tr = by_name.get(task_name)
        routing_set_name = None
        if tr and tr.routing_set_id:
            rs = await session.get(RoutingSet, tr.routing_set_id)
            routing_set_name = rs.name if rs else None
        out.append(
            TaskRoutePublic(
                task_name=task_name,
                routing_set_id=tr.routing_set_id if tr else None,
                routing_set_name=routing_set_name,
                enabled=tr.enabled if tr else False,
            )
        )
    return out


@router.put("/task-routes/{task_name}", response_model=TaskRoutePublic)
async def assign_task_route(
    task_name: str,
    payload: TaskRouteAssign,
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> TaskRoutePublic:
    """Назначение routing set на задачу, с защитными проверками раздела 33:
    нельзя назначить routing set, которого нет, в котором нет enabled-моделей,
    или все модели которого принадлежат disabled-провайдерам."""
    enforce_csrf(request, settings)
    if task_name not in VALID_TASK_NAMES:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Неизвестная задача: {task_name}")

    if payload.routing_set_id:
        rs = await session.get(RoutingSet, payload.routing_set_id)
        if not rs:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Routing set не найден.")

        working = await session.execute(
            select(RoutingSetModel)
            .join(ModelConfig, ModelConfig.id == RoutingSetModel.model_config_id)
            .join(Provider, Provider.id == ModelConfig.provider_id)
            .where(
                RoutingSetModel.routing_set_id == rs.id,
                ModelConfig.enabled.is_(True),
                Provider.enabled.is_(True),
            )
            .limit(1)
        )
        if working.scalar_one_or_none() is None:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "В этом routing set нет ни одной рабочей (enabled) модели с enabled-провайдером.",
            )

    result = await session.execute(select(TaskRoute).where(TaskRoute.task_name == task_name))
    tr = result.scalar_one_or_none()
    if tr is None:
        tr = TaskRoute(task_name=task_name, routing_set_id=payload.routing_set_id, enabled=payload.enabled)
        session.add(tr)
    else:
        tr.routing_set_id = payload.routing_set_id
        tr.enabled = payload.enabled

    await session.commit()

    routing_set_name = None
    if tr.routing_set_id:
        rs = await session.get(RoutingSet, tr.routing_set_id)
        routing_set_name = rs.name if rs else None

    return TaskRoutePublic(
        task_name=task_name, routing_set_id=tr.routing_set_id,
        routing_set_name=routing_set_name, enabled=tr.enabled,
    )
