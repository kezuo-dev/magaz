#!/usr/bin/env python3
"""Проверка пагинации: кириллица, спецсимволы & # в URL, потеря параметров фильтра."""
import sys
from urllib.parse import urlencode, quote
from starlette.testclient import TestClient

sys.path.insert(0, "/Users/kezuo/projects/magaz")

from app.main import app
from app.db import SessionLocal
from app.models import Book, BookStatus
from test_helpers import login, UserRole

client = TestClient(app)
login(client, UserRole.ADMIN)

# Чистим базу и заполняем тестовыми данными
db = SessionLocal()
db.query(Book).delete()
for i in range(55):  # 2 страницы по 50
    db.add(Book(
        sku=f"SKU{i:03d}",
        title=f"Книга #{i} тест",
        status=BookStatus.IN_STOCK,
    ))
db.commit()
db.close()

print("=== Проверка 1: обычная пагинация ===")
r = client.get("/?page=1")
assert r.status_code == 200
assert "SKU000" in r.text or "SKU001" in r.text, "Первая страница не содержит книг"
r = client.get("/?page=2")
assert r.status_code == 200
assert "SKU054" in r.text or "SKU053" in r.text, "Вторая страница не содержит книг"
print("  Страницы 1 и 2 работают ✓")

print("=== Проверка 2: кириллица в поиске + пагинация ===")
# Добавляем книгу с кириллицей в названии
db = SessionLocal()
db.add(Book(sku="CYR001", title="Война и мир 50%", status=BookStatus.IN_STOCK))
db.commit()
db.close()

# Прямой переход на страницу 2 с кириллическим поиском
search = "Война"
params = urlencode({"q": search, "page": 2})
r = client.get(f"/?{params}")
assert r.status_code == 200, f"Кириллица в URL сломала запрос: {r.status_code}"
# Если найдена одна книга, то страница 2 пустая — но не должна падать
print(f"  Статус {r.status_code} ✓")

print("=== Проверка 3: спецсимволы & # в поиске ===")
db = SessionLocal()
db.add(Book(sku="AMP001", title="Книга про A&B и #тег", status=BookStatus.IN_STOCK))
db.commit()
db.close()

# & и # в невалидированном виде могут сломать URL
search_amp = "A&B"
params = urlencode({"q": search_amp, "page": 1})
r = client.get(f"/?{params}")
assert r.status_code == 200, f"Спецсимвол & в поиске: {r.status_code}"
print(f"  Поиск 'A&B': {r.status_code} ✓")

search_hash = "#тег"
params = urlencode({"q": search_hash, "page": 1})
r = client.get(f"/?{params}")
assert r.status_code == 200, f"Спецсимвол # в поиске: {r.status_code}"
print(f"  Поиск '#тег': {r.status_code} ✓")

print("=== Проверка 4: потеря параметров фильтра при переходе на страницу 2 ===")
# Фильтр по статусу + поиск + пагинация: все параметры должны сохраниться
params = urlencode({"q": "Книга", "status": "in_stock", "marketplace": "ozon", "page": 2})
r = client.get(f"/?{params}")
assert r.status_code == 200
# Проверяем, что ссылка на страницу 1 сохранила все параметры
assert "status=in_stock" in r.text, "Потеря параметра status в пагинаторе"
assert "marketplace=ozon" in r.text, "Потеря параметра marketplace в пагинаторе"
print("  Параметры фильтра сохранены в пагинаторе ✓")

print("=== Проверка 5: номер страницы за границами (page=999) ===")
r = client.get("/?page=999")
assert r.status_code == 200, "Страница за границами должна зажиматься, а не падать"
# Код в catalog.py: page = min(max(1, page), pages) — зажимает в диапазон
print(f"  page=999 зажата: {r.status_code} ✓")

print("=== Проверка 6: JSON API /api/books — те же параметры ===")
params = urlencode({"q": "Война", "page": 1})
r = client.get(f"/api/books?{params}")
assert r.status_code == 200
data = r.json()
assert "items" in data and "page" in data
print(f"  JSON API с кириллицей: {r.status_code} ✓")

print("\n✅ Все проверки пагинации прошли")
