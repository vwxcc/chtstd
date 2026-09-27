"""
ChatStudio — /api/routing/* — админ-панель AI Router (разделы 24-33 ТЗ).

Доступно только администраторам (require_admin). Раздел 33 — защитные
инварианты соблюдаются явными проверками перед каждым изменением/удалением.
"""

from __future__ import annotations

import os
import json

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
    Plan,
    AIRequest,
    get_session,
)
from app.schemas import (
    ModelCreate,
    ModelPublic,
    ModelUpdate,
    PlanPublic,
    PlanUpdate,
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
# Subscription plans
# --------------------------------------------------------------------------


@router.get("/plans", response_model=list[PlanPublic])
async def list_plans(
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> list[PlanPublic]:
    result = await session.execute(select(Plan).order_by(Plan.created_at.asc()))
    return [PlanPublic.model_validate(p) for p in result.scalars().all()]


@router.patch("/plans/{plan_name}", response_model=PlanPublic)
async def update_plan(
    plan_name: str,
    payload: PlanUpdate,
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> PlanPublic:
    enforce_csrf(request, settings)
    plan = (await session.execute(select(Plan).where(Plan.name == plan_name))).scalar_one_or_none()
    if not plan:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Тариф не найден.")
    for field in ("display_name", "requests_per_day", "max_tokens", "max_prompt_length",
                  "max_files_per_request", "max_total_file_size", "enabled"):
        value = getattr(payload, field)
        if value is not None:
            setattr(plan, field, value)
    await session.commit()
    await session.refresh(plan)
    return PlanPublic.model_validate(plan)


@router.get("/users")
async def list_users(
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> list[dict]:
    result = await session.execute(select(User).order_by(User.created_at.asc()))
    return [{"id": u.id, "name": u.name, "email": u.email, "plan_name": u.plan_name, "created_at": u.created_at}
            for u in result.scalars().all()]


@router.patch("/users/{user_id}/plan")
async def set_user_plan(
    user_id: str,
    payload: dict,
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    enforce_csrf(request, settings)
    plan_name = str(payload.get("plan_name") or "").strip().lower()
    if plan_name not in {"free", "plus", "pro", "max"}:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Неизвестный тариф.")
    user = await session.get(User, user_id)
    if not user:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Пользователь не найден.")
    user.plan_name = plan_name
    await session.commit()
    return {"ok": True, "user_id": user.id, "plan_name": user.plan_name}


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
        request_prefix=m.request_prefix, temperature=m.temperature, max_tokens=m.max_tokens, timeout=m.timeout, enabled=m.enabled,
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
        request_prefix=payload.request_prefix, temperature=payload.temperature, max_tokens=payload.max_tokens, timeout=payload.timeout, enabled=payload.enabled,
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
    if payload.request_prefix is not None:
        model.request_prefix = payload.request_prefix
    model.temperature = new_temp
    model.max_tokens = new_max_tokens
    model.timeout = new_timeout
    if payload.enabled is not None:
        model.enabled = payload.enabled

    await session.commit()
    await session.refresh(model)
    return _model_public(model)


@router.delete("/models/{model_id}", status_code=status.HTTP_200_OK)
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
# Runtime ENV editor
# --------------------------------------------------------------------------

@router.get("/runtime-env")
async def get_runtime_env(admin: User = Depends(_admin)) -> dict:
    path = get_settings().data_dir / "runtime.env"
    return {"content": path.read_text(encoding="utf-8") if path.exists() else ""}


@router.post("/runtime-env")
async def apply_runtime_env(
    payload: dict,
    request: Request,
    settings: Settings = Depends(get_settings),
    admin: User = Depends(_admin),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Apply a simple model catalog.

    Each MODEL_* definition is a complete model profile. The model selected
    by the user is used for all three operations: main answer, title and
    follow-up suggestions. There are no clusters/fallback chains in this UI.
    """
    enforce_csrf(request, settings)
    content = str(payload.get("content") or "")
    if len(content) > 200000:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Конфигурация слишком большая.")

    values: dict[str, str] = {}
    for raw in content.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Неверная строка ENV: {line[:80]}")
        key, value = line.split("=", 1)
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if not Settings.validate_env_var_name(key):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Недопустимое имя переменной: {key}")
        values[key] = value

    import re
    model_keys = sorted(
        {m.group(1) for key in values for m in [re.match(r"^MODEL_(.+)_ID$", key)] if m},
    )
    if not model_keys:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Нужна хотя бы одна MODEL_<id>_ID.")

    # Expose values to the current process immediately and persist them.
    for key, value in values.items():
        os.environ[key] = value

    model_by_key: dict[str, ModelConfig] = {}
    for model_key in model_keys:
        prefix = f"MODEL_{model_key}_"
        model_name = values.get(prefix + "NAME", model_key).strip()
        real_id = values[prefix + "ID"].strip()
        base_url = values.get(prefix + "BASE", "").strip().rstrip("/")
        api_key = values.get(prefix + "KEY", "")
        file_info = values.get(prefix + "FILE_INFO", "").strip()

        if not base_url.startswith(("http://", "https://")):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{model_key}: BASE должен начинаться с http:// или https://.")
        if not real_id:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"{model_key}: ID не может быть пустым.")

        provider_name = f"runtime-{model_key}"
        provider = (await session.execute(
            select(Provider).where(Provider.name == provider_name)
        )).scalar_one_or_none()

        if provider is None:
            provider = Provider(
                name=provider_name,
                base_url=base_url,
                api_key_env="CHATSTUDIO_RUNTIME_" + prefix + "KEY",
                enabled=True,
            )
            session.add(provider)
            await session.flush()
        else:
            provider.base_url = base_url
            provider.api_key_env = "CHATSTUDIO_RUNTIME_" + prefix + "KEY"
            provider.enabled = True

        os.environ[provider.api_key_env] = api_key

        model = (await session.execute(
            select(ModelConfig).where(ModelConfig.provider_id == provider.id)
        )).scalar_one_or_none()

        if model is None:
            model = ModelConfig(
                provider_id=provider.id,
                display_name=model_name,
                model_name=real_id,
                request_prefix=file_info,
                temperature=0.2,
                max_tokens=120000,
                timeout=300,
                enabled=True,
            )
            session.add(model)
        else:
            model.display_name = model_name
            model.model_name = real_id
            model.request_prefix = file_info
            model.enabled = True
        model_by_key[model_key] = model

    active_provider_names = {f"runtime-{k}" for k in model_keys}
    for provider in (await session.execute(select(Provider))).scalars().all():
        if provider.name.startswith("runtime-"):
            provider.enabled = provider.name in active_provider_names

    # Persist only the simple model catalog. Old routing sets may remain in DB,
    # but direct model selection in ai_router takes precedence for every task.
    path = settings.data_dir / "runtime.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content.strip() + "\n", encoding="utf-8")

    await session.commit()
    return {
        "ok": True,
        "models": len(model_keys),
        "model_ids": [model_by_key[k].id for k in model_keys],
        "restart_required": False,
    }


@router.get("/model-status")
async def model_status(
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Health dots from the last 10 requests involving each model.

    100% success = green, exactly 90% = yellow, below 90% = red.
    With fewer than 10 observations the percentage is calculated from the
    available observations; no observations are treated as green.
    """
    result = await session.execute(
        select(ModelConfig, Provider)
        .join(Provider, Provider.id == ModelConfig.provider_id)
        .where(ModelConfig.enabled.is_(True), Provider.enabled.is_(True), Provider.name.like("runtime-%"))
        .order_by(ModelConfig.created_at.asc())
    )
    models = result.all()

    recent_result = await session.execute(
        select(AIRequest)
        .where(AIRequest.task.in_(["main_generation", "title_generation", "suggestions_generation"]))
        .order_by(AIRequest.created_at.desc())
        .limit(200)
    )
    recent = recent_result.scalars().all()

    out = []
    for model, provider in models:
        observations: list[bool] = []
        for req in recent:
            attempts = []
            if req.fallback_attempts_json:
                try:
                    attempts = json.loads(req.fallback_attempts_json) or []
                except Exception:
                    attempts = []

            attempt_failed = any(
                a.get("provider") == provider.name or a.get("model") == model.display_name
                for a in attempts
            )
            final_match = req.model == model.display_name or req.provider == provider.name

            if attempt_failed:
                observations.append(False)
            elif final_match:
                observations.append(req.status == "completed")

            if len(observations) >= 10:
                break

        observations = observations[:10]
        total = len(observations)
        success_count = sum(1 for ok in observations if ok)
        rate = (success_count / total * 100) if total else 100.0
        status_name = "green" if total == 0 or rate == 100 else ("yellow" if rate >= 90 else "red")
        out.append({
            "id": model.id,
            "display_name": model.display_name,
            "status": status_name,
            "success_rate": round(rate, 1),
            "sample_size": total,
        })
    return {"models": out, "window": 10}

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


@router.delete("/routing-sets/{routing_set_id}", status_code=status.HTTP_200_OK)
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
