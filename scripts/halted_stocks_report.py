#!/usr/bin/env python3
"""Полный отчёт о срабатываниях предохранителя слежения за остатками.

Использование:
    python scripts/halted_stocks_report.py
    python scripts/halted_stocks_report.py --last 5
    python scripts/halted_stocks_report.py --since "2026-08-13"
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

# Добавить корень проекта в путь импорта
sys.path.insert(0, str(Path(__file__).parent.parent))

from sqlalchemy import select, desc
from app.db import SessionLocal
from app.models import SyncLog


def format_timestamp(dt):
    """Форматировать timestamp в локальное время."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    local = dt.astimezone()
    return local.strftime("%Y-%m-%d %H:%M:%S")


def print_report(limit=None, since=None):
    db = SessionLocal()
    try:
        query = select(SyncLog).where(
            SyncLog.action == "watch_stocks",
            SyncLog.ok == False,
            SyncLog.message.like("%ОСТАНОВЛЕНО%")
        )

        if since:
            query = query.where(SyncLog.created_at >= since)

        query = query.order_by(desc(SyncLog.created_at))

        if limit:
            query = query.limit(limit)

        logs = db.scalars(query).all()

        if not logs:
            print("Срабатываний предохранителя не найдено.")
            return

        print(f"=== ОТЧЁТ О СРАБАТЫВАНИЯХ ПРЕДОХРАНИТЕЛЯ ===\n")
        print(f"Найдено срабатываний: {len(logs)}\n")

        for i, log in enumerate(logs, 1):
            print(f"{'='*70}")
            print(f"Срабатывание #{i}")
            print(f"{'='*70}")
            print(f"Время: {format_timestamp(log.created_at)}")
            print(f"Площадка: {log.marketplace}")
            print()

            # Извлечь числа из сообщения
            msg = log.message
            if "у " in msg and " из " in msg:
                parts = msg.split("у ")[1].split(" из ")
                halted = parts[0]
                total = parts[1].split(" книг")[0]
                print(f"Остановлено книг: {halted}")
                print(f"Всего проверено: {total}")

                try:
                    h = int(halted)
                    t = int(total)
                    pct = (h / t) * 100
                    print(f"Процент: {pct:.2f}%")
                except (ValueError, ZeroDivisionError):
                    pass

            print()
            print("Примеры SKU:")
            if "Проверьте остатки вручную:" in msg:
                skus_part = msg.split("Проверьте остатки вручную:")[1].strip()
                skus_part = skus_part.rstrip("…")
                skus = [s.strip() for s in skus_part.split(",")]
                for j, sku in enumerate(skus, 1):
                    print(f"  {j:2}. {sku}")

            # Попробовать найти полный список
            full_log = db.scalar(
                select(SyncLog).where(
                    SyncLog.marketplace == log.marketplace,
                    SyncLog.action == "watch_stocks_halted_full",
                    SyncLog.created_at >= log.created_at,
                    SyncLog.created_at <= log.created_at
                )
            )

            if full_log and full_log.message:
                print()
                print("ПОЛНЫЙ СПИСОК:")
                full_msg = full_log.message
                if ":" in full_msg:
                    all_skus_str = full_msg.split(":", 1)[1].strip()
                    all_skus = [s.strip() for s in all_skus_str.split(",")]
                    print(f"Всего книг: {len(all_skus)}")
                    print()
                    for j, sku in enumerate(all_skus, 1):
                        print(f"  {j:3}. {sku}")

            print()

    finally:
        db.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Отчёт о срабатываниях предохранителя")
    parser.add_argument("--last", type=int, help="Показать последние N срабатываний")
    parser.add_argument("--since", help="Показать с даты (YYYY-MM-DD)")

    args = parser.parse_args()

    since_dt = None
    if args.since:
        try:
            since_dt = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        except ValueError:
            print(f"Ошибка: неверный формат даты '{args.since}'. Используйте YYYY-MM-DD")
            sys.exit(1)

    print_report(limit=args.last, since=since_dt)
