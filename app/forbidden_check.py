"""Проверка каталога на запрещённые темы: экстремизм, терроризм, ЛГБТ и т.д.

Сканирует названия и описания книг в продаже, ищет ключевые слова. Возвращает
список найденных книг с причиной (какая категория). Используется для ручной
модерации перед снятием — автоматического снятия нет (слишком опасно).
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Book, BookStatus


@dataclass
class ForbiddenMatch:
    """Одна найденная книга с проблемной темой."""

    book_id: int
    sku: str
    title: str
    author: str | None
    category: str  # «Экстремизм», «ЛГБТ» и т.д.
    matched_word: str  # какое именно слово нашлось


# Словари запрещённых тем. Регистронезависимый поиск по целым словам (граница \b).
# Ищем в названии и описании. Слова подобраны так, чтобы минимизировать ложные
# срабатывания, но при этом поймать проблемные книги.
PATTERNS = {
    "Экстремизм": [
        r"\bэкстремизм",
        r"\bэкстремист",
        r"\bрадикал",
        r"\bфашизм",
        r"\bнацизм",
        r"\bнаци\b",
    ],
    "Терроризм": [
        r"\bтеррор",
        r"\bджихад",
        r"\bбоевик",
        r"\bтеракт",
    ],
    "Иноагенты": [
        r"\bиноагент",
        r"\bиностранный агент",
    ],
    "ЛГБТ": [
        r"\bлгбт",
        r"\bгей\b",
        r"\bлесбиянк",
        r"\bтрансгенд",
        r"\bквир",
        r"\bгомосексуал",
    ],
    "Экстремистские организации": [
        r"\bигил",
        r"\bисис",
        r"\bаль[-\s]каид",
        r"\bталибан",
    ],
    "Наркотики": [
        r"\bнаркотик",
        r"\bкокаин",
        r"\bгероин",
        r"\bметамфетамин",
        r"\bкурительн",
        r"\bпсихотроп",
        r"\bнаркоман",
    ],
    "Грубые слова": [
        r"\bпроституц",
        r"\bотребье",
        r"\bподонок",
        r"\bсволоч",
        r"\bублюдок",
    ],
}


def check_catalog_for_forbidden(db: Session) -> list[ForbiddenMatch]:
    """Просканировать каталог в продаже, вернуть список книг с проблемными темами.

    Ищет только в книгах со статусом IN_STOCK — снятое и проданное уже не актуально.
    Сканирует title, author, description. Возвращает список совпадений с указанием
    категории и конкретного слова.
    """
    books = db.scalars(
        select(Book).where(Book.status == BookStatus.IN_STOCK)
    ).all()

    results: list[ForbiddenMatch] = []
    for book in books:
        # Собираем текст для поиска: название + автор + описание (если есть)
        text_parts = [book.title or ""]
        if book.author:
            text_parts.append(book.author)
        if book.description:
            text_parts.append(book.description)
        full_text = " ".join(text_parts)

        # Проверяем каждую категорию
        for category, patterns in PATTERNS.items():
            for pattern in patterns:
                match = re.search(pattern, full_text, re.IGNORECASE)
                if match:
                    results.append(
                        ForbiddenMatch(
                            book_id=book.id,
                            sku=book.sku,
                            title=book.title,
                            author=book.author,
                            category=category,
                            matched_word=match.group(0),
                        )
                    )
                    # Нашли одно совпадение в этой категории — переходим к следующей
                    # (не дублируем книгу, если в ней 2 слова из одной категории)
                    break
    return results
