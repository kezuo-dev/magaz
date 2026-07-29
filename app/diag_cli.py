"""Диагностика одной книги: почему кросс-снятие не сработало.

Печатает состояние лотов (статус, external_id, stock_key), состояние рубильника
автоснятия, заказы и последние записи журнала по этой книге. Живёт в app/, чтобы
попадать в образ и вызываться одной короткой строкой:

    docker compose exec app python3 -m app.diag_cli цычыйнаЛ-759
"""
from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.flags import is_auto_withdraw_enabled
from app.models import Book, MarketplaceAccount, Order, SyncLog


def main() -> int:
    if len(sys.argv) < 2:
        print("Использование: python3 -m app.diag_cli <SKU>")
        return 1

    sku = sys.argv[1]
    db = SessionLocal()
    try:
        book = db.scalar(
            select(Book).options(selectinload(Book.listings)).where(Book.sku == sku)
        )
        if book is None:
            print(f"✗ Книга с SKU «{sku}» не найдена")
            return 1

        print(f"Книга: {book.title}")
        print(f"  sku={book.sku}  статус={book.status}  quantity={book.quantity}")
        print(f"Автоснятие: {'ВКЛ' if is_auto_withdraw_enabled(db) else 'ВЫКЛ'}")

        print("Лоты:")
        for lot in book.listings:
            print(f"  {lot.marketplace}: статус={lot.status}")
            print(f"     external_id={lot.external_id}  stock_key={lot.stock_key}")
            print(f"     синхр={lot.last_synced_at}  ошибка={lot.last_error}")

        print("Площадки:")
        for acc in db.scalars(select(MarketplaceAccount)).all():
            has_keys = bool(acc.credentials_encrypted)
            print(f"  {acc.marketplace}: включена={acc.enabled}  ключи={has_keys}")

        orders = db.scalars(select(Order).where(Order.book_id == book.id)).all()
        print(f"Заказы: {len(orders)}")
        for o in orders:
            print(f"  {o.marketplace} {o.external_order_id} отменён={o.cancelled}")

        logs = db.scalars(
            select(SyncLog)
            .where(SyncLog.book_id == book.id)
            .order_by(SyncLog.created_at.desc())
            .limit(15)
        ).all()
        print(f"Журнал по книге (последние {len(logs)}):")
        for entry in reversed(logs):
            mark = "ok" if entry.ok else "ERR"
            print(f"  {entry.created_at} [{mark}] {entry.marketplace} {entry.action}: {entry.message}")

        watch = db.scalars(
            select(SyncLog)
            .where(
                SyncLog.action.in_(
                    [
                        "watch_stocks",
                        "watch_removed",
                        "reconcile_removed",
                        "withdraw_skipped",
                        "auto_withdraw_toggle",
                    ]
                )
            )
            .order_by(SyncLog.created_at.desc())
            .limit(20)
        ).all()
        print(f"Журнал слежения и автоснятия (последние {len(watch)}):")
        for entry in reversed(watch):
            mark = "ok" if entry.ok else "ERR"
            print(f"  {entry.created_at} [{mark}] {entry.marketplace} {entry.action}: {entry.message}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
