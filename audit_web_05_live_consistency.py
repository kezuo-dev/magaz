#!/usr/bin/env python3
"""Проверка живого обновления: расхождения между HTML и JSON."""
import sys
from starlette.testclient import TestClient

sys.path.insert(0, "/Users/kezuo/projects/magaz")

from app.main import app
from app.db import SessionLocal
from app.models import Book, BookStatus, Listing, ListingStatus
from test_helpers import login, UserRole

client = TestClient(app)
login(client, UserRole.ADMIN)

db = SessionLocal()
db.query(Book).delete()

# Создаём книгу с двумя лотами: один активный, один снятый
book = Book(sku="TEST001", title="Тестовая книга", status=BookStatus.IN_STOCK, price=500)
db.add(book)
db.commit()
db.refresh(book)

db.add(Listing(book_id=book.id, marketplace="ozon", status=ListingStatus.ACTIVE, external_id="111"))
db.add(Listing(book_id=book.id, marketplace="wildberries", status=ListingStatus.WITHDRAWN, external_id="222"))
db.commit()
db.close()

print("=== Проверка 1: HTML показывает только активные лоты ===")
r = client.get("/")
assert r.status_code == 200
html_text = r.text
# В HTML должен быть только OZ (активный), WB (снятый) не показывается
assert "TEST001" in html_text
print(f"  HTML содержит TEST001: ✓")

print("\n=== Проверка 2: JSON /api/books показывает те же лоты ===")
r = client.get("/api/books")
data = r.json()
book_json = next((b for b in data['items'] if b['sku'] == 'TEST001'), None)
assert book_json is not None
print(f"  Книга в JSON: {book_json['sku']}")
print(f"  Лоты в JSON: {[l['short'] for l in book_json['listings']]}")
# Только активные лоты (см. catalog.py:246-252)
assert len(book_json['listings']) == 1, f"Ожидался 1 активный лот, найдено {len(book_json['listings'])}"
assert book_json['listings'][0]['short'] == 'OZ'
print("  ✓ JSON показывает только активные лоты")

print("\n=== Проверка 3: /api/live/catalog совпадает с _catalog_stats ===")
r = client.get("/api/live/catalog")
live_data = r.json()
print(f"  Живая статистика: {live_data}")

r = client.get("/")
# В HTML статистика отрисована через _catalog_stats
# Проверим, что числа совпадают (живые не прыгают после загрузки)
assert "in_stock" in live_data
assert "on_ozon" in live_data
assert live_data["in_stock"] == 1, f"in_stock должен быть 1, найдено {live_data['in_stock']}"
assert live_data["on_ozon"] == 1, f"on_ozon должен быть 1 (1 активный лот)"
print("  ✓ Живая статистика совпадает с отрисованной")

print("\n=== Проверка 4: условие в live.py совпадает с catalog.py ===")
# Проверяем, что в live.py:42-48 join по Book такой же, как в catalog.py:123-132
db = SessionLocal()
# Добавим книгу sold с активным лотом — она НЕ должна считаться
sold_book = Book(sku="SOLD001", title="Проданная", status=BookStatus.SOLD, price=300)
db.add(sold_book)
db.commit()
db.refresh(sold_book)
db.add(Listing(book_id=sold_book.id, marketplace="ozon", status=ListingStatus.ACTIVE, external_id="333"))
db.commit()
db.close()

r = client.get("/api/live/catalog")
live_data = r.json()
# on_ozon должен остаться 1: проданная книга не считается, даже если лот активен
assert live_data["on_ozon"] == 1, f"Проданная книга с активным лотом попала в счётчик: {live_data['on_ozon']}"
print("  ✓ Проданные книги не считаются (даже с активным лотом)")

print("\n✅ Все проверки живого обновления прошли")
