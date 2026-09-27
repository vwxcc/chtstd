from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from app.auth import enforce_csrf, get_current_user
from app.config import Settings, get_settings
from app.database import User

router = APIRouter(prefix="/api/code", tags=["code"])

class ExecuteRequest(BaseModel):
    language: str = "python"
    code: str = Field(min_length=1, max_length=50000)

@router.post("/execute")
async def execute_code(
    payload: ExecuteRequest,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
):
    enforce_csrf(request, settings)
    if payload.language.lower() not in {"python", "py"}:
        raise HTTPException(400, "Пока выполняется только Python.")
    with tempfile.TemporaryDirectory(prefix="chatstudio-code-") as td:
        script = Path(td) / "main.py"
        script.write_text(payload.code, encoding="utf-8")
        env = {"PATH": os.environ.get("PATH", ""), "PYTHONIOENCODING": "utf-8"}
        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                sys.executable, "-I", str(script),
                cwd=td, env=env,
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            )
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=8)
        except asyncio.TimeoutError:
            if proc:
                proc.kill()
                await proc.wait()
            return {"ok": False, "stdout": "", "stderr": "Превышено ограничение выполнения: 8 секунд.", "exit_code": -1}
        return {
            "ok": proc.returncode == 0,
            "stdout": stdout.decode("utf-8", "replace")[-20000:],
            "stderr": stderr.decode("utf-8", "replace")[-20000:],
            "exit_code": proc.returncode,
        }
