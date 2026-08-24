"""Диагностика: почему активные WB-лоты не обновляются полной сверкой.

Сверка обновляет 29k карточек, но ~415 активных лотов висят с застрявшим
last_synced_at (12-13.08) и не снимаются. Показываем:
- сколько активных лотов НЕ пришло в выгрузке за проход;
- примеры таких лотов (sku, статус книги, last_synced_at, stock_key);
- общее число строк в выгрузке каталога WB (реальная видимость);
- входят ли эти лоты в would_remove (книга IN_STOCK и SKU нет в выгрузке).

Запуск:
    docker compose --env-file .env.prod exec app python3 -m app.diag_wb_missing
"""
from app.db import SessionLocal
from app.models import BookStatus, Listing, ListingStatus
from sqlalchemy import func, select


def main() -> int:
    db = SessionLocal()
    try:
        total = db.scalar(
            select(func.count()).select_from(Listing).where(
                Listing.marketplace == "wildberries",
                Listing.status == ListingStatus.ACTIVE,
            )
        )
        stale = db.scalar(
            select(func.count())
            .select_from(Listing)
            .join(Listing.book)
            .where(
                Listing.marketplace == "wildberries",
                Listing.status == ListingStatus.ACTIVE,
                Listing.last_synced_at < func.now() - __import__("sqlalchemy").text("interval '3 days'"),
            )
        )
        print(f"активных WB-лотов: {total}, из них не обновлялись >3 суток: {stale}")

        examples = db.execute(
            select(Book.sku, Book.status, Listing.last_synced_at, Listing.stock_key)
            .join(Listing.book)
            .where(
                Listing.marketplace == "wildberries",
                Listing.status == ListingStatus.ACTIVE,
                Listing.last_synced_at < func.now() - __import__("sqlalchemy").text("interval '3 days'"),
            )
            .limit(10)
        ).all()
        print("\nпримеры зависших:")
        for sku, book_status, last_synced, stock_key in examples:
            print(f"  {sku} | книга={book_status} | synced={last_synced} | key={stock_key}")

        # Сколько из зависших книг реально IN_STOCK (входят в would_remove)
        in_stock = db.scalar(
            select(func.count())
            .select_from(Listing)
            .join(Listing.book)
            .where(
                Listing.marketplace == "wildberries",
                Listing.status == ListingStatus.ACTIVE,
                Listing.last_synced_at < func.now() - __import__("sqlalchemy").text("interval '3 days'"),
                Book.status == BookStatus.IN_STOCK,
            )
        )
        print(f"\nиз них книга в статусе IN_STOCK (входят в would_remove): {in_stock}")
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())