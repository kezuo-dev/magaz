#!/usr/bin/env python3
"""Исправить некорректные статусы книг в базе после багов синхронизации.

Проблема: книги с WITHDRAWN статусом, у которых есть неотменённый заказ,
должны быть SOLD. Это происходило, когда watch_stocks снимал книгу раньше,
чем poll_marketplace_orders обрабатывал заказ.

Также исправляем книги, у которых все лоты WITHDRAWN, но есть активные лоты
на самом деле (из-за race condition в poll_marketplace_orders без selectinload).
"""
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models import Book, BookStatus, ListingStatus, Order
from app.sync import refresh_book_status


def main():
    db = SessionLocal()
    try:
        # 1. Найти все WITHDRAWN книги с неотменёнными заказами → должны быть SOLD
        withdrawn_with_orders = db.scalars(
            select(Book)
            .options(selectinload(Book.listings))
            .join(Order, Order.book_id == Book.id)
            .where(
                Book.status == BookStatus.WITHDRAWN,
                Order.cancelled == False,  # noqa: E712
            )
            .distinct()
        ).all()

        print(f"Найдено {len(withdrawn_with_orders)} книг WITHDRAWN с неотменёнными заказами")
        for book in withdrawn_with_orders:
            old_status = book.status
            refresh_book_status(db, book)
            if book.status != old_status:
                print(f"  {book.sku}: {old_status} → {book.status}")

        # 2. Пересчитать статусы всех книг, у которых есть хотя бы один активный лот,
        #    но статус не IN_STOCK (из-за race condition)
        books_with_active_listings = db.scalars(
            select(Book)
            .options(selectinload(Book.listings))
            .join(Book.listings)
            .where(
                Book.status != BookStatus.IN_STOCK,
                Book.listings.any(ListingStatus.ACTIVE),
            )
            .distinct()
        ).all()

        print(f"\nНайдено {len(books_with_active_listings)} книг с активными лотами, но статус не IN_STOCK")
        for book in books_with_active_listings:
            old_status = book.status
            refresh_book_status(db, book)
            if book.status != old_status:
                print(f"  {book.sku}: {old_status} → {book.status}")

        db.commit()
        print("\n✓ Все статусы исправлены")

    except Exception as exc:
        db.rollback()
        print(f"✗ Ошибка: {exc}")
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
