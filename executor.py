"""Run marked Python tasks in a temporary workspace and collect their outputs."""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Awaitable, Callable

from protocol import Block, Submission

StatusCallback = Callable[[str], Awaitable[None]]
InputCallback = Callable[[str], Awaitable[str]]
MAX_FILES = int(os.getenv("MAX_ARTIFACT_FILES", "40"))
MAX_TOTAL = int(os.getenv("MAX_ARTIFACT_TOTAL_BYTES", str(40 * 1024 * 1024)))
MAX_ONE = int(os.getenv("MAX_ARTIFACT_FILE_BYTES", str(15 * 1024 * 1024)))


@dataclass(slots=True)
class TaskResult:
    name: str
    returncode: int
    stdout: str
    stderr: str
    duration: float
    timed_out: bool = False


@dataclass(slots=True)
class ExecutionResult:
    tasks: list[TaskResult]
    artifacts: list[Path]
    warnings: list[str]
    cancelled: bool = False
    workspace: Path | None = None


def _kill(proc: asyncio.subprocess.Process) -> None:
    if proc.returncode is not None:
        return
    try:
        if os.name == "posix":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (ProcessLookupError, PermissionError):
        try:
            proc.kill()
        except ProcessLookupError:
            pass


def _env(workspace: Path, timeout: int, memory: int, preset: Path) -> dict[str, str]:
    return {
        "PATH": os.defpath,
        "HOME": str(workspace),
        "TMPDIR": str(workspace / ".tmp"),
        "PYTHONIOENCODING": "utf-8",
        "PYTHONUNBUFFERED": "1",
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "SANDBOX_CPU_SECONDS": str(timeout),
        "SANDBOX_MEMORY_MB": str(memory),
        "SANDBOX_FILE_MB": "16",
        "SANDBOX_PRESET_INPUT": str(preset),
        "SANDBOX_INPUT_TIMEOUT": os.getenv("INPUT_TIMEOUT_SECONDS", "120"),
    }


async def _run_one(
    block: Block, workspace: Path, input_text: str, cancel: asyncio.Event,
    timeout: int, memory: int, on_input: InputCallback | None,
) -> TaskResult:
    start = time.monotonic()
    tmp = workspace / ".tmp"
    tmp.mkdir(exist_ok=True)
    preset = tmp / f"input-{block.name}.txt"
    request = tmp / "input-request.json"
    response = tmp / "input-response.json"
    preset.write_text(input_text, encoding="utf-8")
    script = tmp / f"script-{block.name}.py"
    script.write_text(block.body, encoding="utf-8")
    runner = Path(__file__).with_name("sandbox_runner.py").resolve()
    out_path, err_path = tmp / f"stdout-{block.name}.log", tmp / f"stderr-{block.name}.log"
    timed_out = False
    with out_path.open("wb") as out, err_path.open("wb") as err:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-I", str(runner), str(script), str(workspace),
            stdin=asyncio.subprocess.DEVNULL, stdout=out, stderr=err,
            cwd=workspace, env=_env(workspace, timeout, memory, preset),
            start_new_session=(os.name == "posix"),
        )
        wait_task = asyncio.create_task(proc.wait())
        cancel_task = asyncio.create_task(cancel.wait())
        deadline = time.monotonic() + timeout
        seen_request: str | None = None
        try:
            while not wait_task.done():
                if cancel_task.done() and cancel.is_set():
                    _kill(proc)
                    await wait_task
                    raise asyncio.CancelledError
                if request.exists():
                    try:
                        payload = json.loads(request.read_text(encoding="utf-8"))
                        request.unlink(missing_ok=True)
                    except (OSError, json.JSONDecodeError):
                        payload = {}
                    request_id = payload.get("id")
                    if isinstance(request_id, str) and request_id != seen_request:
                        seen_request = request_id
                        if on_input is None:
                            raise EOFError("input() ожидает значение, но обработчик ввода не задан")
                        prompt = payload.get("prompt", "")
                        answer = await on_input(prompt if isinstance(prompt, str) else "")
                        if cancel.is_set():
                            _kill(proc)
                            await wait_task
                            raise asyncio.CancelledError
                        temporary = response.with_suffix(".tmp")
                        temporary.write_text(json.dumps({"id": request_id, "value": answer}, ensure_ascii=False), encoding="utf-8")
                        os.replace(temporary, response)
                        deadline = time.monotonic() + timeout
                        continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    timed_out = True
                    _kill(proc)
                    await wait_task
                    break
                await asyncio.wait({wait_task, cancel_task}, timeout=min(0.1, remaining), return_when=asyncio.FIRST_COMPLETED)
        finally:
            cancel_task.cancel()
            if not wait_task.done():
                _kill(proc)
                await asyncio.gather(wait_task, return_exceptions=True)
        code = proc.returncode if proc.returncode is not None else -9

    def read_output(path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8", errors="replace")[-18000:].strip()
        except OSError:
            return ""

    stdout, stderr = read_output(out_path), read_output(err_path)
    if timed_out:
        stderr = (stderr + "\n" if stderr else "") + f"Лимит выполнения {timeout} с исчерпан; процесс остановлен."
        code = -9
    out_path.unlink(missing_ok=True)
    err_path.unlink(missing_ok=True)
    script.unlink(missing_ok=True)
    preset.unlink(missing_ok=True)
    return TaskResult(block.name, code, stdout, stderr, time.monotonic() - start, timed_out)


def _write_files(submission: Submission, workspace: Path) -> None:
    root = workspace.resolve()
    for block in submission.files:
        destination = (root / Path(*block.name.split("/"))).resolve()
        if not destination.is_relative_to(root):
            raise ValueError(f"Unsafe FILE path: {block.name}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(block.body, encoding="utf-8")


def _artifacts(workspace: Path) -> tuple[list[Path], list[str]]:
    found: list[Path] = []
    warnings: list[str] = []
    total = 0
    root = workspace.resolve()
    for current, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = [d for d in dirs if not d.startswith(".") and not (Path(current) / d).is_symlink()]
        for filename in files:
            path = Path(current) / filename
            if filename.startswith(".") or path.is_symlink():
                continue
            try:
                resolved = path.resolve()
                size = resolved.stat().st_size
                if not resolved.is_relative_to(root) or not resolved.is_file():
                    continue
            except OSError:
                continue
            if size > MAX_ONE:
                warnings.append(f"{filename}: файл больше {MAX_ONE // 1048576} МБ — пропущен.")
                continue
            if len(found) >= MAX_FILES or total + size > MAX_TOTAL:
                warnings.append("Достигнут лимит выдачи файлов.")
                return found, warnings
            found.append(resolved)
            total += size
    return found, warnings


async def execute_submission(
    submission: Submission, cancel_event: asyncio.Event,
    on_status: StatusCallback, on_input: InputCallback | None = None,
) -> ExecutionResult:
    timeout = max(2, min(int(os.getenv("TASK_TIMEOUT_SECONDS", "12")), 30))
    memory = max(128, min(int(os.getenv("TASK_MEMORY_MB", "256")), 384))
    workspace = Path(tempfile.mkdtemp(prefix="telegram-sandbox-")).resolve()
    try:
        try:
            _write_files(submission, workspace)
        except (OSError, ValueError) as exc:
            return ExecutionResult([], [], [f"Не удалось подготовить FILE-блоки: {exc}"], workspace=workspace)
        results: list[TaskResult] = []
        for index, block in enumerate(submission.python_tasks, 1):
            if cancel_event.is_set():
                return ExecutionResult(results, [], [], cancelled=True, workspace=workspace)
            await on_status(f"Выполняю Python-задачу {index}/{len(submission.python_tasks)}: {block.name}")
            try:
                result = await _run_one(block, workspace, submission.input_by_task.get(block.name, ""), cancel_event, timeout, memory, on_input)
            except asyncio.CancelledError:
                return ExecutionResult(results, [], [], cancelled=True, workspace=workspace)
            except Exception as exc:
                result = TaskResult(block.name, 1, "", f"Ошибка исполнителя: {type(exc).__name__}: {exc}", 0.0)
            results.append(result)
        artifacts, warnings = await asyncio.to_thread(_artifacts, workspace)
        return ExecutionResult(results, artifacts, warnings, workspace=workspace)
    except Exception:
        shutil.rmtree(workspace, ignore_errors=True)
        raise
