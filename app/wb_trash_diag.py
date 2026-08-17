"""Диагностика очереди корзины WB: что реально видит cleanup_wb_trash.

Проходит РОВНО тем же запросом, что и move_withdrawn_to_trash, и печатает числа:
сколько книг прошло каждый фильтр и первые кандидаты. Ничего не удаляет и не
меняет — только читает. Запуск короткой строкой:

    docker compose exec app python3 -m app.wb_trash_diag

Отдельный модуль (а не правка wb_trash.py), чтобы диагностика не влияла на
боевую логику и вызывалась из контейнера без многострочного python -c.
"""
from __future__ import annotations

from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.models import Book, BookStatus, Listing, ListingStatus, Order, utcnow
from app.wb_trash import CANCEL_GRACE_DAYS, MAX_BOOKS_PER_RUN


def main() -> int:
    db = SessionLocal()
    try:
        # Тот же запрос, что и в move_withdrawn_to_trash (без удаления).
        query = (
            select(Book)
            .options(selectinload(Book.listings))
            .where(
                Book.status.in_([BookStatus.SOLD, BookStatus.WITHDRAWN]),
                Book.listings.any(
                    (Listing.marketplace == "wildberries")
                    & (Listing.status != ListingStatus.TRASHED)
                    & (Listing.trash_blocked == False)  # noqa: E712
                    & Listing.external_id.regexp_match(r"^\d+$")
                ),
            )
        )
        all_books = db.scalars(query).all()
        print(f"Всего кандидатов по ORM-запросу (без LIMIT): {len(all_books)}")

        # Книги со свежим не-отменённым заказом (окно отмены).
        book_ids = [b.id for b in all_books]
        cutoff = utcnow() - timedelta(days=CANCEL_GRACE_DAYS)
        active = set(
            db.scalars(
                select(Order.book_id).where(
                    Order.book_id.in_(book_ids),
                    Order.cancelled == False,  # noqa: E712
                    Order.created_at >= cutoff,
                ).distinct()
            ).all()
        )
        print(f"  из них в окне отмены (grace {CANCEL_GRACE_DAYS} дн): {len(active)}")
        ready = [b for b in all_books if b.id not in active]
        print(f"  готовы к удалению: {len(ready)}")
        print(f"  берётся за проход (MAX_BOOKS_PER_RUN={MAX_BOOKS_PER_RUN}): "
              f"{min(len(ready), MAX_BOOKS_PER_RUN)}")

        # Первые 5, которых хватило бы на проход (самые старые по updated_at).
        ready_sorted = sorted(ready, key=lambda b: b.updated_at)[:5]
        print("\nПервые 5 кандидатов (то, что взял бы cleanup):")
        for b in ready_sorted:
            wb = next((l for l in b.listings if l.marketplace == "wildberries"), None)
            if wb:
                print(f"  {b.sku}: book={b.status} listing={wb.status} "
                      f"nm={wb.external_id} upd={b.updated_at} trash_f={wb.trash_failures} "
                      f"blocked={wb.trash_blocked}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
