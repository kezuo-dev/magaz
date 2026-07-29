#!/usr/bin/env python3
"""Ручное снятие книги с указанной площадки для тестирования.

Использование:
    python3 manual_withdraw.py непмекН-725 ozon
    python3 manual_withdraw.py непмекН-725 wildberries
"""
import sys

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models import Book
from app.sync import withdraw_book


def main():
    if len(sys.argv) < 3:
        print("Использование: python3 manual_withdraw.py <SKU> <marketplace>")
        print("Marketplace: ozon или wildberries")
        sys.exit(1)

    sku = sys.argv[1]
    marketplace = sys.argv[2]

    db = SessionLocal()
    try:
        book = db.scalar(
            select(Book).options(selectinload(Book.listings)).where(Book.sku == sku)
        )
        if not book:
            print(f"✗ Книга с SKU «{sku}» не найдена")
            sys.exit(1)

        listing = next((l for l in book.listings if l.marketplace == marketplace), None)
        if not listing:
            print(f"✗ У книги «{sku}» нет лота на площадке «{marketplace}»")
            sys.exit(1)

        print(f"Снимаем книгу «{book.title}» ({sku})")
        print(f"  Площадка: {marketplace}")
        print(f"  Статус лота до: {listing.status}")
        print(f"  external_id: {listing.external_id}")
        print(f"  stock_key: {listing.stock_key}")

        success = withdraw_book(db, book, marketplace)
        db.commit()

        print(f"\n{'✓' if success else '✗'} Снятие {'выполнено' if success else 'не выполнено'}")
        print(f"  Статус лота после: {listing.status}")
        if listing.last_error:
            print(f"  Ошибка: {listing.last_error}")

    except Exception as exc:
        db.rollback()
        print(f"✗ Ошибка: {exc}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    main()
