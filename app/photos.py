"""Сохранение загруженных фото книг на диск и выдача ссылок на них.

Файлы кладём в app/static/uploads/<book_id>/, чтобы отдавать через уже
смонтированный /static. В поле Book.photos храним публичные ссылки
(вида /static/uploads/12/uuid.jpg) — вперемешку с внешними URL, если их вписали руками.
"""
import uuid
from pathlib import Path

from fastapi import UploadFile

from app.config import BASE_DIR, settings

UPLOAD_DIR = BASE_DIR / "app" / "static" / "uploads"

# Разрешённые расширения — чтобы не сохранять произвольные файлы.
ALLOWED_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
MAX_BYTES = 10 * 1024 * 1024  # 10 МБ на фото


def save_photos(book_id: int, files: list[UploadFile]) -> list[str]:
    """Сохраняет пришедшие файлы и возвращает список ссылок на них."""
    urls: list[str] = []
    dest_dir = UPLOAD_DIR / str(book_id)

    for file in files:
        if not file or not file.filename:
            continue
        ext = Path(file.filename).suffix.lower()
        if ext not in ALLOWED_EXT:
            continue

        data = file.file.read()
        if not data or len(data) > MAX_BYTES:
            continue

        dest_dir.mkdir(parents=True, exist_ok=True)
        name = f"{uuid.uuid4().hex}{ext}"
        (dest_dir / name).write_bytes(data)
        urls.append(f"/static/uploads/{book_id}/{name}")

    return urls


def to_public_url(url: str) -> str:
    """Превратить нашу относительную ссылку на фото в абсолютную.

    Площадки (Ozon, WB) скачивают изображения по URL со своей стороны, поэтому
    относительный путь вида /static/uploads/12/a.jpg им не годится — нужен полный
    адрес с хостом. Внешние ссылки (http/https) оставляем как есть.
    """
    url = (url or "").strip()
    if not url or url.startswith(("http://", "https://")):
        return url
    base = settings.public_base_url.rstrip("/")
    if not url.startswith("/"):
        url = "/" + url
    return f"{base}{url}"


def public_photo_list(book) -> list[str]:
    """Ссылки на фото книги в абсолютном виде — для отправки на площадки."""
    return [to_public_url(u) for u in book.photo_list]


def delete_photo_file(book_id: int, url: str) -> None:
    """Удаляет файл нашего загруженного фото с диска. Внешние URL пропускаем.

    Защита от выхода за пределы папки книги: имя берём только из последнего
    сегмента ссылки и проверяем, что итоговый путь лежит внутри uploads/<book_id>.
    """
    prefix = f"/static/uploads/{book_id}/"
    if not url.startswith(prefix):
        return
    name = Path(url).name
    dest_dir = (UPLOAD_DIR / str(book_id)).resolve()
    target = (dest_dir / name).resolve()
    if target.parent == dest_dir and target.is_file():
        target.unlink(missing_ok=True)
