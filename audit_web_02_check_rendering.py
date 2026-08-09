#!/usr/bin/env python3
"""Проверка, что страница вообще отдаёт книги."""
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
b = Book(sku="TEST001", title="Тестовая книга", status=BookStatus.IN_STOCK, price=500)
db.add(b)
db.commit()
db.refresh(b)

# Добавим листинг
listing = Listing(book_id=b.id, marketplace="ozon", status=ListingStatus.ACTIVE, external_id="12345")
db.add(listing)
db.commit()
db.close()

r = client.get("/")
print(f"Статус: {r.status_code}")
print(f"Есть 'TEST001': {'TEST001' in r.text}")
print(f"Есть 'Тестовая книга': {'Тестовая книга' in r.text}")

# Проверим JSON API
r = client.get("/api/books")
data = r.json()
print(f"\nJSON API items: {len(data['items'])}")
if data['items']:
    print(f"Первый элемент: {data['items'][0]}")
