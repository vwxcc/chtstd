"""Idempotent first-run configuration for the built-in Qwen provider."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.database import ModelConfig, Provider, RoutingSet, RoutingSetModel, TaskRoute


async def bootstrap_default_routing(session: AsyncSession) -> None:
    settings = get_settings()
    provider = (await session.execute(select(Provider).where(Provider.name == "Qwen"))).scalar_one_or_none()
    if provider is None:
        provider = Provider(name="Qwen", base_url="https://llm.stage.satel.org/v1", api_key_env="QWEN_API_KEY", enabled=True)
        session.add(provider)
        await session.flush()

    model = (await session.execute(select(ModelConfig).where(ModelConfig.provider_id == provider.id, ModelConfig.model_name == "qwen36-35b"))).scalar_one_or_none()
    if model is None:
        model = ModelConfig(provider_id=provider.id, display_name="Qwen 3.5 35B", model_name="qwen36-35b", temperature=0.2, max_tokens=32000, timeout=settings.request_timeout, enabled=True)
        session.add(model)
        await session.flush()

    routing = (await session.execute(select(RoutingSet).where(RoutingSet.name == "default"))).scalar_one_or_none()
    if routing is None:
        routing = RoutingSet(name="default")
        session.add(routing)
        await session.flush()

    entry = (await session.execute(select(RoutingSetModel).where(RoutingSetModel.routing_set_id == routing.id, RoutingSetModel.model_config_id == model.id))).scalar_one_or_none()
    if entry is None:
        session.add(RoutingSetModel(routing_set_id=routing.id, model_config_id=model.id, priority=0))

    for task_name in ("main_generation", "title_generation", "suggestions_generation"):
        route = (await session.execute(select(TaskRoute).where(TaskRoute.task_name == task_name))).scalar_one_or_none()
        if route is None:
            session.add(TaskRoute(task_name=task_name, routing_set_id=routing.id, enabled=True))
        elif route.routing_set_id is None:
            route.routing_set_id = routing.id
            route.enabled = True

    await session.commit()
