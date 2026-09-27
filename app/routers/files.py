"""
ChatStudio — /api/files/* (разделы 34-42 ТЗ).
"""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, Request, UploadFile, status
from fastapi.responses import FileResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.auth import enforce_csrf, get_current_user
from app.config import Settings, get_settings
from app.database import FileRecord, MessageFile, User, get_session
from app.file_processing import (
    ZIP_BASED_EXTENSIONS,
    extract_text,
    guess_mime_type,
    inspect_zip_archive,
    validate_magic_bytes,
)
from app.schemas import AttachedFileRef

router = APIRouter(prefix="/api/files", tags=["files"])


def _to_public(f: FileRecord) -> AttachedFileRef:
    return AttachedFileRef(
        id=f.id, original_name=f.original_name, extension=f.extension,
        mime_type=f.mime_type, size_bytes=f.size_bytes,
    )


def _safe_user_dir(settings: Settings, user_id: str) -> Path:
    """Каталог пользователя внутри UPLOAD_DIR, с защитой от path traversal (раздел 41)."""
    base = settings.upload_dir.resolve()
    user_dir = (base / user_id).resolve()
    if base not in user_dir.parents and user_dir != base:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Некорректный путь пользователя.")
    user_dir.mkdir(parents=True, exist_ok=True)
    return user_dir


async def _get_owned_file(session: AsyncSession, file_id: str, user_id: str) -> FileRecord:
    result = await session.execute(select(FileRecord).where(FileRecord.id == file_id, FileRecord.user_id == user_id))
    f = result.scalar_one_or_none()
    if not f:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл не найден.")
    return f


@router.post("", status_code=status.HTTP_201_CREATED)
async def upload_files(
    request: Request,
    files: list[UploadFile],
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    enforce_csrf(request, settings)

    if len(files) > settings.max_files_per_request:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Не более {settings.max_files_per_request} файлов за раз.")

    user_dir = _safe_user_dir(settings, user.id)

    created: list[FileRecord] = []
    written_paths: list[Path] = []
    warnings: list[str] = []
    total_size = 0

    try:
        for upload in files:
            original_name = upload.filename or "file"
            extension = original_name.rsplit(".", 1)[-1].lower() if "." in original_name else ""

            if extension not in settings.allowed_file_extensions:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Расширение .{extension} не поддерживается.")

            data = await upload.read()
            size = len(data)
            total_size += size

            if size > settings.max_file_size:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, f"Файл «{original_name}» превышает {settings.max_file_size} байт.")
            if total_size > settings.max_total_file_size:
                raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "Превышен суммарный размер загружаемых файлов.")

            # 1) magic bytes (раздел 37)
            validate_magic_bytes(extension, data)

            # 2) защита архивов (раздел 38) — для zip-подобных контейнеров
            if extension in ZIP_BASED_EXTENSIONS:
                inspect_zip_archive(data, settings)

            # 3) сохранение на диск, путь строго внутри UPLOAD_DIR/user_id/ (раздел 41)
            stored_name = f"{uuid.uuid4().hex}.{extension}" if extension else uuid.uuid4().hex
            dest_path = (user_dir / stored_name).resolve()
            if user_dir not in dest_path.parents:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Недопустимое имя файла.")
            dest_path.write_bytes(data)
            written_paths.append(dest_path)

            # 4) извлечение текста (раздел 39) — не фатально при ошибке
            processed = extract_text(extension, data, settings)
            if processed.warning:
                warnings.append(f"{original_name}: {processed.warning}")

            record = FileRecord(
                user_id=user.id,
                original_name=original_name[:255],
                stored_path=str(Path(user.id) / stored_name),
                extension=extension,
                mime_type=upload.content_type or guess_mime_type(extension),
                size_bytes=size,
                extracted_text=processed.extracted_text,
            )
            session.add(record)
            created.append(record)

        await session.flush()
        await session.commit()
    except Exception:
        # If validation/processing fails after a previous file was written,
        # remove those files so the filesystem cannot diverge from the DB.
        for path in written_paths:
            path.unlink(missing_ok=True)
        raise

    return {"files": [_to_public(f) for f in created], "warnings": warnings}


@router.get("", response_model=list[AttachedFileRef])
async def list_files(
    q: str | None = Query(default=None),
    user: User = Depends(get_current_user),
    settings: Settings = Depends(get_settings),
    session: AsyncSession = Depends(get_session),
) -> list[AttachedFileRef]:
    stmt = select(FileRecord).where(FileRecord.user_id == user.id)
    if q:
        stmt = stmt.where(FileRecord.original_name.ilike(f"%{q.strip()[:settings.max_search_length]}%"))
    stmt = stmt.order_by(FileRecord.created_at.desc())

    result = await session.execute(stmt)
    return [_to_public(f) for f in result.scalars().all()]


@router.get("/{file_id}", response_model=AttachedFileRef)
async def get_file_metadata(
    file_id: str,
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> AttachedFileRef:
    f = await _get_owned_file(session, file_id, user.id)
    return _to_public(f)


@router.get("/{file_id}/download")
async def download_file(
    file_id: str,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    f = await _get_owned_file(session, file_id, user.id)

    full_path = (settings.upload_dir / f.stored_path).resolve()
    base = settings.upload_dir.resolve()
    if base not in full_path.parents or not full_path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "Файл отсутствует на диске.")

    return FileResponse(path=full_path, filename=f.original_name, media_type=f.mime_type)


@router.delete("/{file_id}", status_code=status.HTTP_200_OK)
async def delete_file(
    file_id: str,
    request: Request,
    settings: Settings = Depends(get_settings),
    user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_session),
) -> dict:
    enforce_csrf(request, settings)
    f = await _get_owned_file(session, file_id, user.id)

    usage = await session.execute(select(MessageFile.id).where(MessageFile.file_id == f.id).limit(1))
    if usage.scalar_one_or_none() is not None:
        # раздел 42: нельзя удалить файл, уже привязанный к историческому сообщению
        raise HTTPException(status.HTTP_409_CONFLICT, "Файл уже используется в переписке и не может быть удалён.")

    full_path = (settings.upload_dir / f.stored_path).resolve()
    base = settings.upload_dir.resolve()

    await session.delete(f)
    await session.commit()

    if base in full_path.parents and full_path.exists():
        full_path.unlink(missing_ok=True)
    return {"ok": True}
