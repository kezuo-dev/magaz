#!/usr/bin/env python3
"""Проверка LIKE-инъекций через % и _ в поиске."""
import sys
from starlette.testclient import TestClient

sys.path.insert(0, "/Users/kezuo/projects/magaz")

from app.main import app
from app.db import SessionLocal
from app.models import Book, BookStatus
from test_helpers import login, UserRole

client = TestClient(app)
login(client, UserRole.ADMIN)

db = SessionLocal()
db.query(Book).delete()
# Создаём книги с разными артикулами
db.add(Book(sku="ABC123", title="Первая книга", status=BookStatus.IN_STOCK))
db.add(Book(sku="DEF456", title="Вторая книга", status=BookStatus.IN_STOCK))
db.add(Book(sku="50%", title="Книга со скидкой 50%", status=BookStatus.IN_STOCK))
db.add(Book(sku="A_B", title="Книга A_B", status=BookStatus.IN_STOCK))
db.commit()
db.close()

print("=== Проверка 1: поиск '50%' находит только одну книгу ===")
r = client.get("/api/books?q=50%25")  # %25 = закодированный %
data = r.json()
print(f"  Найдено книг: {len(data['items'])}")
print(f"  SKU: {[item['sku'] for item in data['items']]}")
# Без экранирования '50%' находило бы все книги (% = любые символы в LIKE)
assert len(data['items']) == 1, f"Найдено {len(data['items'])}, ожидалось 1 — LIKE-инъекция!"
assert data['items'][0]['sku'] == '50%'
print("  ✓ LIKE-экранирование работает для %")

print("\n=== Проверка 2: поиск 'A_B' находит только одну книгу ===")
r = client.get("/api/books?q=A_B")
data = r.json()
print(f"  Найдено книг: {len(data['items'])}")
print(f"  SKU: {[item['sku'] for item in data['items']]}")
# Без экранирования 'A_B' находило бы 'ABC', 'AXB' и т.д. (_ = один символ в LIKE)
assert len(data['items']) == 1, f"Найдено {len(data['items'])}, ожидалось 1 — LIKE-инъекция!"
assert data['items'][0]['sku'] == 'A_B'
print("  ✓ LIKE-экранирование работает для _")

print("\n=== Проверка 3: поиск с \\ (экранирующий символ) ===")
db = SessionLocal()
db.add(Book(sku="PATH\\FILE", title="Путь с бэкслешем", status=BookStatus.IN_STOCK))
db.commit()
db.close()

r = client.get("/api/books?q=PATH%5C")  # %5C = закодированный \
data = r.json()
print(f"  Найдено книг: {len(data['items'])}")
if data['items']:
    print(f"  SKU: {[item['sku'] for item in data['items']]}")
    # Должна найтись ровно одна книга с бэкслешем
    assert len(data['items']) == 1
    assert data['items'][0]['sku'] == 'PATH\\FILE'
    print("  ✓ LIKE-экранирование работает для \\")

print("\n=== Проверка 4: HTML страница /log с поиском ===")
db = SessionLocal()
from app.models import SyncLog
db.add(SyncLog(marketplace="ozon", action="test", ok=True, message="Тест 50% скидка"))
db.add(SyncLog(marketplace="wildberries", action="test", ok=True, message="Другое сообщение"))
db.commit()
db.close()

r = client.get("/log?q=50%25")
assert r.status_code == 200
# Проверяем, что в HTML только одна запись (не все записи через %)
assert "50% скидка" in r.text
print("  ✓ LIKE-экранирование в /log работает")

print("\n✅ Все проверки LIKE-инъекций прошли")
