"""Отложенный перенос проданных/снятых книг в архив.

Идея: книга не исчезает из каталога в момент продажи или снятия. Она остаётся
на виду ещё какое-то время (archive_after_days) с пометкой, когда уедет в архив.
За это время можно заметить ошибку или выставить книгу заново. По истечении окна
фоновый планировщик сам переносит книгу в архив; можно перенести и вручную.
"""
from __future__ import annotations

from datetime import datetime, timezone, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Book, BookStatus, utcnow


def _as_aware(dt: datetime) -> datetime:
    """SQLite отдаёт наивные datetime — приводим к UTC-aware для сравнения."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt

# Статусы, при которых книга считается ушедшей с продажи и подлежит архивации.
REMOVED_STATUSES = (BookStatus.SOLD, BookStatus.WITHDRAWN)


def mark_removed(book: Book) -> None:
    """Отметить, что книга ушла с продажи. Ставит точку отсчёта, если её ещё нет."""
    if book.removed_at is None:
        book.removed_at = utcnow()


def clear_removed(book: Book) -> None:
    """Книга снова в продаже — убираем и отметку ухода, и архив."""
    book.removed_at = None
    book.archived_at = None


def archive_due_at(book: Book):
    """Когда книга уедет в архив (aware datetime), либо None, если она не на выходе."""
    if book.archived_at is not None or book.removed_at is None:
        return None
    return _as_aware(book.removed_at) + timedelta(days=settings.archive_after_days)


def days_until_archive(book: Book) -> int | None:
    """Сколько целых дней осталось до автопереноса (0 — уедет в ближайший проход).
    None — книга не на выходе в архив."""
    due = archive_due_at(book)
    if due is None:
        return None
    remaining = due - utcnow()
    return max(0, remaining.days)


def sweep_to_archive(db: Session) -> int:
    """Перенести в архив книги, у которых истекло окно ожидания. Возвращает их число."""
    # Naive-UTC граница: SQLite хранит datetime без tz, сравниваем в том же виде.
    cutoff = (utcnow() - timedelta(days=settings.archive_after_days)).replace(tzinfo=None)
    due = db.scalars(
        select(Book).where(
            Book.archived_at.is_(None),
            Book.removed_at.is_not(None),
            Book.removed_at <= cutoff,
            Book.status.in_([s.value for s in REMOVED_STATUSES]),
        )
    ).all()
    now = utcnow()
    for book in due:
        book.archived_at = now
    return len(due)
