#!/usr/bin/env python3
"""Проверка удаления карточек в корзину WB.

Использование:
    python scripts/check_wb_trash.py "аолнепт ДУБЛЬ-172"
    python scripts/check_wb_trash.py --all
"""
import sys
from pathlib import Path

# Добавить корень проекта в путь импорта
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select
from app.db import SessionLocal
from app.models import Book, Listing, SyncLog, BookStatus, ListingStatus


def check_book(sku: str):
    """Проверить удаление конкретной книги."""
    db = SessionLocal()
    try:
        # Найти книгу
        book = db.scalar(select(Book).where(Book.sku == sku))

        if not book:
            print(f"❌ Книга '{sku}' НЕ НАЙДЕНА в базе")
            return

        print(f"=== ПРОВЕРКА КНИГИ: {sku} ===\n")
        print(f"ID: {book.id}")
        print(f"Название: {book.title}")
        print(f"Статус: {book.status}")
        print(f"Обновлена: {book.updated_at}")
        print()

        # Найти лот WB
        wb_listing = next((l for l in book.listings if l.marketplace == "wildberries"), None)

        if not wb_listing:
            print("❌ Лот WB не найден")
            return

        print(f"Лот WB:")
        print(f"  Status: {wb_listing.status}")
        print(f"  external_id (nmID): {wb_listing.external_id}")
        print(f"  stock_key: {wb_listing.stock_key}")
        print()

        # Проверить логи удаления. С 2026-08 удалённые карточки пишутся ОДНОЙ
        # итоговой записью со списком SKU (без book_id) — ищем по SKU в сообщении,
        # а не по book_id (старые записи остались с book_id, ищем оба).
        logs = db.scalars(
            select(SyncLog).where(
                SyncLog.action == "wb_trash",
                SyncLog.message.like(f"%{sku}%")
            ).order_by(SyncLog.created_at.desc())
        ).all()

        print(f"Логи удаления ({len(logs)}):")
        if not logs:
            print("  ❌ Логов удаления НЕТ")
            print()
            print("⚠️  ВОЗМОЖНЫЕ ПРИЧИНЫ:")

            if wb_listing.status != "withdrawn":
                print(f"  1. Статус лота != withdrawn: {wb_listing.status}")

            if not wb_listing.external_id:
                print("  2. Нет external_id (nmID)")
            else:
                try:
                    int(wb_listing.external_id)
                except ValueError:
                    print(f"  2. external_id не число: {wb_listing.external_id}")

            if book.status not in ["sold", "withdrawn"]:
                print(f"  3. Статус книги не SOLD/WITHDRAWN: {book.status}")

        else:
            for log in logs:
                ok_mark = "✓" if log.ok else "✗"
                print(f"  {ok_mark} {log.created_at}: {log.message}")
            print()

            # Проверить, была ли успешная удаление (старые записи — per-book,
            # новые — итоговая со списком SKU)
            success_logs = [l for l in logs if l.ok and ("удалена в корзину" in l.message or "Удалены:" in l.message)]
            if success_logs:
                print(f"✅ Книга БЫЛА УДАЛЕНА ({len(success_logs)} записей в журнале)")
                print(f"   Последний раз: {success_logs[0].created_at}")
            else:
                print("❌ Успешных удалений НЕТ")

    finally:
        db.close()


def check_all():
    """Проверить все книги в корзине."""
    db = SessionLocal()
    try:
        # Все снятые книги с лотом WB
        books = db.scalars(
            select(Book).where(
                Book.status.in_([BookStatus.SOLD, BookStatus.WITHDRAWN]),
                Book.listings.any(Listing.marketplace == "wildberries")
            ).order_by(Book.updated_at.desc())
        ).all()

        print(f"=== ВСЕ КНИГИ В КОРЗИНЕ WB ===\n")
        print(f"Всего: {len(books)}\n")

        deleted_count = 0
        not_deleted_count = 0
        no_logs_count = 0

        for book in books[:50]:  # Первые 50
            wb_listing = next((l for l in book.listings if l.marketplace == "wildberries"), None)
            if not wb_listing:
                continue

            # Новый формат: итоговая запись со списком SKU (без per-book записей).
            # Проверяем статус лота TRASHED — это признак фактического удаления,
            # который ставится после успешного API-вызова. Журнал смотрим как
            # дополнение (старые записи с book_id или итоговые списки).
            if wb_listing.status == ListingStatus.TRASHED:
                deleted_count += 1
                status = "✅"
            elif wb_listing.external_id:
                not_deleted_count += 1
                status = "❌"
            else:
                no_logs_count += 1
                status = "⚠️"

            print(f"{status} {book.sku:30} | {wb_listing.external_id or 'нет nmID'}")

        print()
        print(f"Удалено: {deleted_count}")
        print(f"Не удалено (есть nmID): {not_deleted_count}")
        print(f"Нет nmID: {no_logs_count}")

    finally:
        db.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Проверка удаления в корзину WB")
    parser.add_argument("sku", nargs="?", help="SKU книги для проверки")
    parser.add_argument("--all", action="store_true", help="Проверить все книги")

    args = parser.parse_args()

    if args.all:
        check_all()
    elif args.sku:
        check_book(args.sku)
    else:
        parser.print_help()
