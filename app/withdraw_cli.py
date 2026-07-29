"""Ручное снятие книги с площадки одной короткой командой.

Живёт внутри app/, потому что в образ копируется только этот пакет — скрипт из
корня проекта внутри контейнера недоступен. Вызов намеренно короткий: длинные
строки терминал при вставке рвёт переносами, и python -c падает на отступах.

    docker compose exec app python3 -m app.withdraw_cli непмекН-725 ozon

Площадка по умолчанию — ozon.
"""
from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models import Book
from app.sync import withdraw_book


def main() -> int:
    if len(sys.argv) < 2:
        print("Использование: python3 -m app.withdraw_cli <SKU> [ozon|wildberries]")
        return 1

    sku = sys.argv[1]
    marketplace = sys.argv[2] if len(sys.argv) > 2 else "ozon"

    db = SessionLocal()
    try:
        book = db.scalar(
            select(Book).options(selectinload(Book.listings)).where(Book.sku == sku)
        )
        if book is None:
            print(f"✗ Книга с SKU «{sku}» не найдена")
            return 1

        listing = next((l for l in book.listings if l.marketplace == marketplace), None)
        if listing is None:
            print(f"✗ У книги «{sku}» нет лота на площадке «{marketplace}»")
            return 1

        print(f"Снимаем «{book.title}» ({sku}) с {marketplace}")
        print(f"  статус лота до: {listing.status}, external_id: {listing.external_id}")

        ok = withdraw_book(db, book, marketplace)
        db.commit()

        print(f"{'✓' if ok else '✗'} Снятие {'выполнено' if ok else 'не выполнено'}")
        print(f"  статус лота: {listing.status}, статус книги: {book.status}")
        if listing.last_error:
            print(f"  ошибка: {listing.last_error}")
        return 0 if ok else 1
    except Exception as exc:  # noqa: BLE001 — ручной инструмент, показываем всё
        db.rollback()
        print(f"✗ Ошибка: {exc}")
        import traceback

        traceback.print_exc()
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
