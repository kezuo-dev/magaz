"""Загрузка заявок из Google-формы.

Как это устроено без ключей и библиотек Google: таблицу ответов формы публикуют
в интернет как CSV (Файл → Поделиться → Опубликовать в интернете → лист ответов,
формат CSV). Получается ссылка, по которой любой может скачать ответы. Эту ссылку
один раз сохраняют в настройках, а программа по кнопке скачивает CSV и заводит
заявки с ролью «pending».

Пароль в форме приходит в открытом виде (Google сам предупреждает, что так делать
не стоит). Поэтому мы его сразу хешируем и в базу кладём только хеш — открытый
пароль нигде не сохраняем.
"""
from __future__ import annotations

import csv
import io

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import User, UserRole
from app.security import hash_password, normalize_phone


# Заголовки столбцов в форме. Сопоставляем по вхождению подстроки (без учёта
# регистра), чтобы мелкие правки формулировок в форме не ломали импорт.
COL_NAME = "фамилия"       # «Фамилия Имя»
COL_POSITION = "должность"  # «Должность»
COL_PHONE = "телефон"       # «Номер телефона»
COL_PASSWORD = "пароль"     # «Пароль»


def _find_col(headers: list[str], needle: str) -> int | None:
    """Индекс первого столбца, в заголовке которого встречается needle."""
    needle = needle.lower()
    for i, h in enumerate(headers):
        if needle in (h or "").lower():
            return i
    return None


def fetch_rows(csv_url: str) -> list[dict]:
    """Скачать CSV и вернуть строки как список словарей с нужными полями.

    Каждая строка: {full_name, position, phone (нормализован), password}.
    Первый столбец Google-формы — «Отметка времени», используем его как ключ
    защиты от повторного импорта одной и той же заявки.
    """
    resp = httpx.get(csv_url, follow_redirects=True, timeout=30)
    resp.raise_for_status()

    reader = csv.reader(io.StringIO(resp.text))
    all_rows = list(reader)
    if not all_rows:
        return []

    headers = all_rows[0]
    idx_name = _find_col(headers, COL_NAME)
    idx_pos = _find_col(headers, COL_POSITION)
    idx_phone = _find_col(headers, COL_PHONE)
    idx_pass = _find_col(headers, COL_PASSWORD)

    if idx_phone is None or idx_pass is None:
        raise ValueError(
            "В CSV не найдены столбцы «Номер телефона» и/или «Пароль». "
            "Проверьте, что опубликован лист ответов формы."
        )

    def cell(row: list[str], idx: int | None) -> str:
        if idx is None or idx >= len(row):
            return ""
        return (row[idx] or "").strip()

    rows = []
    for raw in all_rows[1:]:
        if not raw or not any(c.strip() for c in raw):
            continue  # пустая строка
        # Первый столбец — отметка времени (уникальна для каждого ответа).
        timestamp = (raw[0] or "").strip() if raw else ""
        rows.append({
            "response_id": timestamp,
            "full_name": cell(raw, idx_name),
            "position": cell(raw, idx_pos),
            "phone": normalize_phone(cell(raw, idx_phone)),
            "password": cell(raw, idx_pass),
        })
    return rows


def import_applications(db: Session, csv_url: str) -> dict:
    """Скачать ответы формы и завести новые заявки. Возвращает сводку.

    Пропускаем строки:
    - уже импортированные (по response_id или по номеру телефона);
    - с кривым телефоном (не 11 цифр, не начинается с 7);
    - с пустым паролем.

    Возвращает {"added": N, "skipped": M, "errors": [...]}.
    """
    rows = fetch_rows(csv_url)

    # Что уже есть в базе — чтобы не заводить дубли.
    existing_phones = set(db.scalars(select(User.phone)).all())
    existing_response_ids = {
        rid for rid in db.scalars(
            select(User.form_response_id).where(User.form_response_id.is_not(None))
        ).all()
    }

    added = 0
    skipped = 0
    errors: list[str] = []

    for row in rows:
        phone = row["phone"]
        response_id = row["response_id"] or None

        # Уже импортировали эту же заявку или этот номер уже есть.
        if response_id and response_id in existing_response_ids:
            skipped += 1
            continue
        if phone in existing_phones:
            skipped += 1
            continue

        # Валидация телефона и пароля.
        if len(phone) != 11 or not phone.startswith("7"):
            errors.append(f"{row['full_name'] or 'без имени'}: неверный телефон «{row['phone']}»")
            continue
        if not row["password"]:
            errors.append(f"{row['full_name'] or 'без имени'} ({phone}): пустой пароль")
            continue

        user = User(
            phone=phone,
            full_name=row["full_name"],
            password_hash=hash_password(row["password"]),
            role=UserRole.PENDING,
            source="form",
            form_response_id=response_id,
            comment=row["position"] or None,
        )
        db.add(user)
        added += 1
        # Помечаем в локальных множествах, чтобы дубли внутри одного CSV тоже отсеялись.
        existing_phones.add(phone)
        if response_id:
            existing_response_ids.add(response_id)

    db.commit()
    return {"added": added, "skipped": skipped, "errors": errors}
