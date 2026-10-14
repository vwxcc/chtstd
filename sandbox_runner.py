"""Child-process entry point: resource limits and file-based interactive input."""
from __future__ import annotations

import builtins
import json
import os
import runpy
import sys
import time
import uuid
from pathlib import Path


def apply_limits() -> None:
    try:
        import resource
    except ImportError:
        return
    cpu_seconds = max(1, int(os.environ.pop("SANDBOX_CPU_SECONDS", "12")))
    memory_mb = max(128, int(os.environ.pop("SANDBOX_MEMORY_MB", "256")))
    file_mb = max(1, int(os.environ.pop("SANDBOX_FILE_MB", "12")))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (file_mb * 1024 * 1024, file_mb * 1024 * 1024))
    resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    if hasattr(resource, "RLIMIT_AS"):
        memory = memory_mb * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (memory, memory))


def install_input(workspace: Path) -> None:
    tmp = workspace / ".tmp"
    tmp.mkdir(exist_ok=True)
    preset_path = Path(os.environ.get("SANDBOX_PRESET_INPUT", str(tmp / "preset-input.txt")))
    request_path = tmp / "input-request.json"
    response_path = tmp / "input-response.json"
    try:
        preset = preset_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        preset = []
    index = 0
    timeout = max(10, min(int(os.environ.get("SANDBOX_INPUT_TIMEOUT", "120")), 300))

    def sandbox_input(prompt: object = "") -> str:
        nonlocal index
        rendered_prompt = str(prompt)
        if rendered_prompt:
            sys.stdout.write(rendered_prompt)
            sys.stdout.flush()
        if index < len(preset):
            value = preset[index]
            index += 1
            return value
        request_id = uuid.uuid4().hex
        temporary = request_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"id": request_id, "prompt": rendered_prompt}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, request_path)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                response = json.loads(response_path.read_text(encoding="utf-8"))
                if response.get("id") == request_id:
                    response_path.unlink(missing_ok=True)
                    value = response.get("value", "")
                    if not isinstance(value, str):
                        raise EOFError("Input response must be text")
                    return value
            except (OSError, json.JSONDecodeError):
                pass
            time.sleep(0.1)
        request_path.unlink(missing_ok=True)
        raise EOFError(f"Не получен ввод за {timeout} секунд")

    builtins.input = sandbox_input


def main() -> int:
    if len(sys.argv) != 3:
        print("Runner usage error.", file=sys.stderr)
        return 2
    script_path = os.path.abspath(sys.argv[1])
    workspace = Path(os.path.abspath(sys.argv[2]))
    apply_limits()
    os.chdir(workspace)
    sys.path.insert(0, str(workspace))
    sys.argv = [script_path]
    install_input(workspace)
    try:
        runpy.run_path(script_path, run_name="__main__")
        return 0
    except SystemExit as exc:
        if exc.code is None:
            return 0
        if isinstance(exc.code, int):
            return exc.code
        print(exc.code, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
