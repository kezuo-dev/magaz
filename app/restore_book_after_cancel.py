"""Вручную восстановить отменённые книги в продажу.

После фикса «cancelled_after_ship не признак отгрузки» (5ef81b6) книги,
отменённые до реальной отгрузки, снова возвращаются в продажу при очередном
опросе отмен. НО уже обнулённые карточки (пкеиптАА-619, пуеАА-869) программа
не перевыставит сама — обнулённые остатки Ozon и карточку WB в корзине надо
вернуть вручную.

Запуск:  docker compose exec app python3 -m app.restore_book_after_cancel <SKU> [<SKU> ...]
"""
from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.marketplaces import get_client
from app.models import Book, BookStatus, Listing, ListingStatus, MarketplaceAccount, SyncLog, utcnow
from app.security import decrypt_credentials
from app.sync import refresh_book_status


def _get_client(db, marketplace):
    acc = db.scalar(
        select(MarketplaceAccount).where(MarketplaceAccount.marketplace == marketplace)
    )
    if not acc or not acc.enabled or not acc.credentials_encrypted:
        return None
    creds = decrypt_credentials(acc.credentials_encrypted)
    return get_client(marketplace, creds)


def _restore_listing(db, book, listing, client) -> None:
    if client is None:
        return
    try:
        client.restore(listing)  # Ozon: остаток 1; WB: recover из корзины + остаток 1
        listing.status = ListingStatus.ACTIVE
        listing.last_synced_at = utcnow()
        listing.last_error = None
        listing.removed_from_sale = False
        db.add(SyncLog(marketplace=listing.marketplace, book_id=book.id,
                       action="order_cancelled", ok=True,
                       message=f"Ручное восстановление после отмены: карточка {book.sku} "
                               f"на {listing.marketplace} возвращена в продажу"))
    except Exception as exc:
        db.add(SyncLog(marketplace=listing.marketplace, book_id=book.id,
                       action="order_cancelled", ok=False,
                       message=f"Ручное восстановление не удалось на {listing.marketplace}: {exc}"))


def main() -> int:
    if len(sys.argv) < 2:
        print("Использование: python3 -m app.restore_book_after_cancel <SKU> [<SKU> ...]")
        return 1

    db = SessionLocal()
    try:
        for sku in sys.argv[1:]:
            book = db.scalar(
                select(Book).options(selectinload(Book.listings)).where(Book.sku == sku)
            )
            if book is None:
                print(f"  ✗ {sku}: книга не найдена")
                continue
            print(f"Книга {book.sku}: {book.title} (статус {book.status})")
            for listing in book.listings:
                client = _get_client(db, listing.marketplace)
                _restore_listing(db, book, listing, client)
                print(f"  {listing.marketplace}: → {listing.status}")
            refresh_book_status(db, book)
            print(f"  новый статус книги: {book.status}")
        db.commit()
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())