"""
ChatStudio — точка входа (раздел 51-59 ТЗ).

Запуск (локально, без Docker):
    pip install -r requirements.txt
    uvicorn app.main:app --host 0.0.0.0 --port 8000

В Docker — см. Dockerfile/docker-compose.yml в корне проекта.
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.ai_router import recover_interrupted_requests, router_service
from app.config import get_settings
from app.database import AsyncSessionLocal, init_db
from app.bootstrap import bootstrap_default_routing
from app.routers import admin_routing, auth, chats, files, messages, requests, shared, search, code_exec

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("chatstudio")

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()

    async with AsyncSessionLocal() as bootstrap_session:
        await bootstrap_default_routing(bootstrap_session)

    recovered = await recover_interrupted_requests()
    if recovered:
        logger.warning("Восстановление после рестарта: %d запрос(ов) переведено в failed.", recovered)

    router_service.start()
    logger.info("ChatStudio запущен. GLOBAL_AI_CONCURRENCY=%d", settings.global_ai_concurrency)

    yield

    await router_service.stop()


app = FastAPI(title="ChatStudio", lifespan=lifespan)

# --------------------------------------------------------------------------
# API-роутеры (раздел 51)
# --------------------------------------------------------------------------

app.include_router(auth.router)
app.include_router(chats.router)
app.include_router(messages.router)
app.include_router(files.router)
app.include_router(requests.router)
app.include_router(shared.router)
app.include_router(admin_routing.router)
app.include_router(search.router)
app.include_router(code_exec.router)


# --------------------------------------------------------------------------
# Единый формат ошибок (раздел 53) — никаких traceback наружу
# --------------------------------------------------------------------------


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Необработанная ошибка на %s %s", request.method, request.url.path)
    return JSONResponse(status_code=500, content={"detail": "Внутренняя ошибка сервера."})


# --------------------------------------------------------------------------
# Frontend (раздел 7-9) — статика + отдача index.html для клиентского роутинга
# --------------------------------------------------------------------------

frontend_dir = settings.frontend_dir.resolve()

if frontend_dir.exists():
    assets_dir = frontend_dir / "assets"
    if assets_dir.exists():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/{full_path:path}", include_in_schema=False)
    async def serve_frontend(full_path: str) -> FileResponse:
        candidate = (frontend_dir / full_path).resolve()
        if frontend_dir in candidate.parents and candidate.is_file():
            return FileResponse(candidate)
        return FileResponse(frontend_dir / "index.html")
else:
    logger.warning("FRONTEND_DIR (%s) не найден — отдаётся только API.", frontend_dir)
