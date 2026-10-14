"""Parser for the single-message Python Sandbox protocol."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

MAX_BLOCKS = 100
MAX_PYTHON_TASKS = 30
MAX_SOURCE_BYTES = 900_000
MAX_FILE_BLOCK_BYTES = 2_000_000
TASK_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,48}$")
BLOCK_RE = re.compile(
    r"(?ms)^\[\[\[(PYTHON|INPUT|FILE):([^\]\r\n]+)\]\]\][ \t]*\r?\n"
    r"(.*?)^\[\[\[END\]\]\][ \t]*\r?$"
)


class ProtocolError(ValueError):
    """Raised when a submission doesn't follow the documented protocol."""


@dataclass(frozen=True, slots=True)
class Block:
    kind: str
    name: str
    body: str


@dataclass(frozen=True, slots=True)
class Submission:
    blocks: tuple[Block, ...]

    @property
    def python_tasks(self) -> tuple[Block, ...]:
        return tuple(block for block in self.blocks if block.kind == "PYTHON")

    @property
    def input_by_task(self) -> dict[str, str]:
        return {block.name: block.body for block in self.blocks if block.kind == "INPUT"}

    @property
    def files(self) -> tuple[Block, ...]:
        return tuple(block for block in self.blocks if block.kind == "FILE")


def _validate_file_path(value: str) -> str:
    value = value.strip()
    if not value or "\\" in value or "\x00" in value:
        raise ProtocolError("В FILE указан некорректный путь.")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ProtocolError(f"Запрещён путь файла: {value!r}. Используй относительный путь без '..'.")
    if len(path.parts) > 8 or len(value) > 180:
        raise ProtocolError("Слишком длинный путь файла.")
    if any(part.startswith(".") for part in path.parts):
        raise ProtocolError("Имена файлов и каталогов, начинающиеся с точки, запрещены.")
    return path.as_posix()


def parse_submission(source: str) -> Submission:
    """Parse marker blocks; an unmarked message is treated as one Python script."""
    if not source or not source.strip():
        raise ProtocolError("Сообщение пустое. Отправь Python-код или блоки по шаблону /start.")
    if len(source.encode("utf-8")) > MAX_SOURCE_BYTES:
        raise ProtocolError(f"Сообщение слишком большое (лимит {MAX_SOURCE_BYTES // 1000} КБ).")

    matches = list(BLOCK_RE.finditer(source))
    if not matches:
        if "[[[" in source or "]]]" in source:
            raise ProtocolError("Не удалось разобрать маркеры. Сверь формат с инструкцией /start.")
        return Submission((Block("PYTHON", "main", source.rstrip() + "\n"),))

    blocks: list[Block] = []
    previous_end = 0
    for match in matches:
        if source[previous_end:match.start()].strip():
            raise ProtocolError(
                "Нашёл текст вне блоков. Оставь только блоки [[[PYTHON:name]]], "
                "[[[INPUT:name]]] / [[[FILE:path]]] и [[[END]]]."
            )
        kind = match.group(1)
        raw_name = match.group(2).strip()
        body = match.group(3)
        if body.startswith("\r\n"):
            body = body[2:]
        elif body.startswith("\n"):
            body = body[1:]
        if body.endswith("\r\n"):
            body = body[:-2]
        elif body.endswith("\n"):
            body = body[:-1]

        if kind == "FILE":
            name = _validate_file_path(raw_name)
            if len(body.encode("utf-8")) > MAX_FILE_BLOCK_BYTES:
                raise ProtocolError(f"Файл {name!r} слишком большой для текстового блока.")
        else:
            name = raw_name
            if not TASK_ID_RE.fullmatch(name):
                raise ProtocolError(f"Некорректное имя задачи: {name!r}.")
            if kind == "PYTHON" and not body.strip():
                raise ProtocolError(f"Задача {name!r} не содержит кода.")
        blocks.append(Block(kind, name, body))
        previous_end = match.end()

    if source[previous_end:].strip():
        raise ProtocolError("После последнего блока найден лишний текст.")
    if len(blocks) > MAX_BLOCKS:
        raise ProtocolError(f"Слишком много блоков: максимум {MAX_BLOCKS}.")

    python_names = [b.name for b in blocks if b.kind == "PYTHON"]
    input_names = [b.name for b in blocks if b.kind == "INPUT"]
    file_names = [b.name.casefold() for b in blocks if b.kind == "FILE"]
    if len(python_names) > MAX_PYTHON_TASKS:
        raise ProtocolError(f"Максимум Python-задач: {MAX_PYTHON_TASKS}.")
    if len(python_names) != len(set(python_names)):
        raise ProtocolError("Имена блоков PYTHON должны быть уникальными.")
    if len(input_names) != len(set(input_names)):
        raise ProtocolError("Для каждой задачи допускается не более одного блока INPUT.")
    if len(file_names) != len(set(file_names)):
        raise ProtocolError("Один и тот же путь FILE указан несколько раз.")
    unknown_inputs = set(input_names) - set(python_names)
    if unknown_inputs:
        raise ProtocolError("INPUT ссылается на неизвестные Python-задачи: " + ", ".join(sorted(unknown_inputs)))
    if not python_names and not any(b.kind == "FILE" for b in blocks):
        raise ProtocolError("Нет блоков PYTHON или FILE для выполнения.")
    return Submission(tuple(blocks))
