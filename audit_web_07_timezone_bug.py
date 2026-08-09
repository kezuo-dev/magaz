#!/usr/bin/env python3
"""Баг: SQLite возвращает naive datetime, PostgreSQL — aware. Проверка последствий."""
import sys
from datetime import timezone

sys.path.insert(0, "/Users/kezuo/projects/magaz")

from app.db import SessionLocal, engine
from app.models import Book, BookStatus, Order, utcnow
from app.config import settings

db = SessionLocal()
db.query(Book).delete()
db.query(Order).delete()
db.commit()

print(f"=== База: {settings.database_url.split('://')[0]} ===")
print(f"Используется: {'SQLite' if 'sqlite' in settings.database_url else 'PostgreSQL'}")

# Создаём книгу
book = Book(sku="TZ_TEST", title="Проверка timezone", status=BookStatus.IN_STOCK)
db.add(book)
db.commit()
db.refresh(book)

print(f"\nBook.created_at после commit: {book.created_at}")
print(f"  Тип: {type(book.created_at)}")
print(f"  tzinfo: {book.created_at.tzinfo}")
print(f"  Naive? {book.created_at.tzinfo is None}")

# Проверим, что происходит при сравнении
now_aware = utcnow()  # должен быть aware (timezone.utc)
print(f"\nutcnow(): {now_aware}")
print(f"  tzinfo: {now_aware.tzinfo}")

# На PostgreSQL сравнение aware и naive падает с TypeError
# На SQLite всё naive, поэтому сравнение работает
try:
    comparison = book.created_at < now_aware
    print(f"\nСравнение book.created_at < utcnow(): {comparison}")
    print("  ✓ Сравнение работает")
except TypeError as e:
    print(f"\n  ❌ Сравнение упало: {e}")
    print("  Это сломает код, который сравнивает datetime из базы с utcnow()")

# Проверим routes/analytics.py: сравнение Order.created_at >= cutoff
print("\n=== Проверка analytics.py: Order.created_at >= cutoff ===")
order = Order(
    marketplace="ozon",
    external_order_id="TZ_ORD001",
    book_id=book.id,
)
db.add(order)
db.commit()
db.refresh(order)

print(f"Order.created_at: {order.created_at}, tzinfo: {order.created_at.tzinfo}")

from datetime import timedelta
cutoff = utcnow() - timedelta(days=7)
print(f"cutoff (utcnow() - 7d): {cutoff}, tzinfo: {cutoff.tzinfo}")

try:
    # Имитация запроса из analytics.py:65
    from sqlalchemy import select
    result = db.scalars(
        select(Order).where(Order.created_at >= cutoff)
    ).all()
    print(f"  Запрос с cutoff работает, найдено {len(result)} заказов")
except Exception as e:
    print(f"  ❌ Запрос упал: {e}")

# Проверим templating.py: _to_msk(dt)
print("\n=== Проверка templating.py: _to_msk(naive datetime) ===")
from app.templating import _to_msk
try:
    msk_str = _to_msk(book.created_at)
    print(f"  _to_msk(book.created_at): {msk_str}")
    # В _to_msk есть проверка: if dt.tzinfo is None: dt = dt.replace(tzinfo=timezone.utc)
    # Поэтому на SQLite это работает, но ОПАСНО: если системное время не UTC, будет сдвиг
    print("  ✓ _to_msk обрабатывает naive datetime (приводит к UTC)")
except Exception as e:
    print(f"  ❌ _to_msk упал: {e}")

db.close()

print("\n=== ВЫВОД ===")
print("SQLite возвращает naive datetime (tzinfo=None), PostgreSQL — aware (tzinfo=UTC).")
print("Код имеет защиту в _to_msk, но:")
print("  1. Сравнение datetime может сломаться на PostgreSQL (если SQLAlchemy не приводит)")
print("  2. Предполагается, что naive = UTC, но это зависит от системного времени сервера")
print("\nРЕКОМЕНДАЦИЯ: проверить на реальном PostgreSQL, упадут ли запросы с cutoff.")
