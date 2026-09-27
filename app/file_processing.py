"""
ChatStudio — обработка загружаемых файлов (разделы 35-41 ТЗ).

Пайплайн одного файла:
    1) расширение в белом списке (раздел 35)
    2) размер <= MAX_FILE_SIZE (раздел 36)
    3) magic bytes соответствуют заявленному расширению (раздел 37)
    4) если это zip-контейнер (docx/xlsx/pptx/zip) — защита от архивных
       бомб: лимит записей, лимит распакованного размера, запрет
       зашифрованных/повреждённых архивов (раздел 38)
    5) извлечение текста с ограничениями по страницам/листам/слайдам
       и итоговой длине (раздел 39)

Ошибки на шагах 1-4 — фатальны (400/413). Ошибка на шаге 5 (не удалось
распарсить документ) — НЕ фатальна: файл всё равно сохраняется, просто
extracted_text останется пустым, о чём вызывающий код должен предупредить
пользователя (раздел 53 — не показывать трейсбек, но и не блокировать
сохранение валидного, но нечитаемого contentwise файла).
"""

from __future__ import annotations

import io
import logging
import zipfile
from dataclasses import dataclass

from fastapi import HTTPException, status

from app.config import Settings

logger = logging.getLogger("chatstudio.files")

ZIP_BASED_EXTENSIONS = {"docx", "xlsx", "pptx", "zip"}
IMAGE_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
TEXT_EXTENSIONS = {
    "txt", "md", "csv", "json", "xml", "html", "htm", "css", "js", "mjs", "cjs",
    "ts", "tsx", "jsx", "py", "java", "c", "cpp", "h", "hpp", "cs", "go", "rs",
    "php", "rb", "swift", "kt", "kts", "sh", "bash", "zsh", "sql", "yaml", "yml",
    "toml", "ini", "cfg", "conf", "env", "log", "vue", "svelte", "astro",
}


@dataclass
class ProcessedFile:
    extracted_text: str | None
    warning: str | None = None


# --------------------------------------------------------------------------
# Magic bytes (раздел 37)
# --------------------------------------------------------------------------


def _looks_like_zip(data: bytes) -> bool:
    return data[:4] in (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")


def validate_magic_bytes(extension: str, data: bytes) -> None:
    ext = extension.lower()

    if ext == "pdf":
        if not data.startswith(b"%PDF-"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Файл не похож на настоящий PDF.")

    elif ext in ZIP_BASED_EXTENSIONS:
        if not _looks_like_zip(data):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Файл .{ext} повреждён или не является ZIP-контейнером.")

    elif ext in ("xls",):
        # legacy OLE2 формат
        if not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Файл .xls повреждён.")

    elif ext == "ppt":
        if not data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Файл .ppt повреждён.")

    elif ext == "png":
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Файл не является PNG-изображением.")

    elif ext in ("jpg", "jpeg"):
        if not data.startswith(b"\xff\xd8\xff"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Файл не является JPEG-изображением.")

    elif ext == "gif":
        if data[:6] not in (b"GIF87a", b"GIF89a"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Файл не является GIF-изображением.")

    elif ext == "webp":
        if not (data[:4] == b"RIFF" and data[8:12] == b"WEBP"):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "Файл не является WEBP-изображением.")

    # Известные текстовые и исходные файлы не требуют magic bytes.
    elif ext in TEXT_EXTENSIONS:
        try:
            data[: 65536].decode("utf-8")
        except UnicodeDecodeError:
            try:
                data[:65536].decode("cp1251")
            except UnicodeDecodeError:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, "Не удалось распознать текстовую кодировку файла.")


# --------------------------------------------------------------------------
# Защита архивов (раздел 38)
# --------------------------------------------------------------------------


def inspect_zip_archive(data: bytes, settings: Settings) -> None:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            infos = zf.infolist()

            if len(infos) > settings.max_archive_entries:
                raise HTTPException(
                    status.HTTP_400_BAD_REQUEST,
                    f"В архиве больше {settings.max_archive_entries} записей — отклонено.",
                )

            total_uncompressed = 0
            for info in infos:
                if info.flag_bits & 0x1:
                    raise HTTPException(status.HTTP_400_BAD_REQUEST, "Зашифрованные архивы не поддерживаются.")
                total_uncompressed += info.file_size
                if total_uncompressed > settings.max_archive_unpacked_size:
                    raise HTTPException(
                        status.HTTP_400_BAD_REQUEST,
                        "Суммарный распакованный размер архива превышает допустимый лимит.",
                    )

            # быстрая проверка целостности без полной распаковки
            bad_file = zf.testzip()
            if bad_file is not None:
                raise HTTPException(status.HTTP_400_BAD_REQUEST, f"Архив повреждён: {bad_file}")

    except zipfile.BadZipFile:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "Архив повреждён или имеет неверный формат.")


# --------------------------------------------------------------------------
# Извлечение текста (раздел 39)
# --------------------------------------------------------------------------


def _truncate(text: str, settings: Settings) -> str:
    return text[: settings.max_file_context_chars]


def _extract_pdf(data: bytes, settings: Settings) -> ProcessedFile:
    try:
        from pypdf import PdfReader
    except ImportError:  # pragma: no cover
        from PyPDF2 import PdfReader  # type: ignore

    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        return ProcessedFile(extracted_text=None, warning=f"Не удалось открыть PDF: {e}")

    if getattr(reader, "is_encrypted", False):
        return ProcessedFile(extracted_text=None, warning="PDF зашифрован и не может быть прочитан.")

    pages = reader.pages[: settings.max_document_pages]
    chunks: list[str] = []
    for page in pages:
        try:
            chunks.append(page.extract_text() or "")
        except Exception:  # noqa: BLE001
            continue
    return ProcessedFile(extracted_text=_truncate("\n".join(chunks), settings))


def _extract_docx(data: bytes, settings: Settings) -> ProcessedFile:
    from docx import Document

    try:
        doc = Document(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        return ProcessedFile(extracted_text=None, warning=f"Не удалось открыть DOCX: {e}")

    paragraphs = [p.text for p in doc.paragraphs if p.text]
    text = "\n".join(paragraphs)
    return ProcessedFile(extracted_text=_truncate(text, settings))


def _extract_xlsx(data: bytes, settings: Settings) -> ProcessedFile:
    from openpyxl import load_workbook

    try:
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as e:  # noqa: BLE001
        return ProcessedFile(extracted_text=None, warning=f"Не удалось открыть XLSX: {e}")

    chunks: list[str] = []
    for sheet_name in wb.sheetnames[: settings.max_spreadsheet_sheets]:
        ws = wb[sheet_name]
        chunks.append(f"# {sheet_name}")
        for row in ws.iter_rows(values_only=True):
            cells = ["" if c is None else str(c) for c in row]
            if any(cells):
                chunks.append("\t".join(cells))
            if sum(len(c) for c in chunks) > settings.max_file_context_chars:
                break
    return ProcessedFile(extracted_text=_truncate("\n".join(chunks), settings))


def _extract_pptx(data: bytes, settings: Settings) -> ProcessedFile:
    from pptx import Presentation

    try:
        prs = Presentation(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        return ProcessedFile(extracted_text=None, warning=f"Не удалось открыть PPTX: {e}")

    chunks: list[str] = []
    for i, slide in enumerate(prs.slides):
        if i >= settings.max_presentation_slides:
            break
        chunks.append(f"# Слайд {i + 1}")
        for shape in slide.shapes:
            if shape.has_text_frame:
                text = shape.text_frame.text
                if text:
                    chunks.append(text)
    return ProcessedFile(extracted_text=_truncate("\n".join(chunks), settings))


def _extract_plain_text(data: bytes, settings: Settings) -> ProcessedFile:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("cp1251", errors="replace")
    return ProcessedFile(extracted_text=_truncate(text, settings))


def extract_text(extension: str, data: bytes, settings: Settings) -> ProcessedFile:
    ext = extension.lower()
    try:
        if ext == "pdf":
            return _extract_pdf(data, settings)
        if ext == "docx":
            return _extract_docx(data, settings)
        if ext == "xlsx":
            return _extract_xlsx(data, settings)
        if ext == "pptx":
            return _extract_pptx(data, settings)
        if ext in TEXT_EXTENSIONS:
            return _extract_plain_text(data, settings)
        # Для неизвестных расширений пытаемся определить обычный UTF-8/CP1251 текст.
        # Бинарные файлы при этом просто сохраняются и передаются как вложение.
        sample = data[:65536]
        try:
            decoded = sample.decode("utf-8")
            if "\x00" not in decoded:
                return ProcessedFile(extracted_text=_truncate(data.decode("utf-8"), settings))
        except UnicodeDecodeError:
            try:
                decoded = sample.decode("cp1251")
                if "\x00" not in decoded:
                    return ProcessedFile(extracted_text=_truncate(data.decode("cp1251"), settings))
            except UnicodeDecodeError:
                pass
        return ProcessedFile(extracted_text=None)
    except Exception as e:  # noqa: BLE001
        # Раздел 53: не показываем traceback пользователю, но и не роняем загрузку
        logger.exception("Ошибка извлечения текста из файла .%s", ext)
        return ProcessedFile(extracted_text=None, warning="Не удалось извлечь текст из файла.")


def guess_mime_type(extension: str) -> str:
    return {
        "pdf": "application/pdf",
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "txt": "text/plain",
        "md": "text/markdown",
        "csv": "text/csv",
        "xls": "application/vnd.ms-excel",
        "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "ppt": "application/vnd.ms-powerpoint",
        "pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "json": "application/json",
        "xml": "application/xml",
        "html": "text/html",
        "htm": "text/html",
        "css": "text/css",
        "js": "text/javascript",
        "py": "text/x-python",
        "svg": "image/svg+xml",
        "zip": "application/zip",
        "png": "image/png",
        "jpg": "image/jpeg",
        "jpeg": "image/jpeg",
        "gif": "image/gif",
        "webp": "image/webp",
    }.get(extension.lower(), "application/octet-stream")
