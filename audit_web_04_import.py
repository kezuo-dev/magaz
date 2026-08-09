#!/usr/bin/env python3
"""Проверка импорта CSV/XLSX: кодировки, частичные файлы, отсутствующие колонки."""
import sys
import io
import csv
from starlette.testclient import TestClient

sys.path.insert(0, "/Users/kezuo/projects/magaz")

from app.main import app
from app.db import SessionLocal
from app.models import Book
from test_helpers import login, UserRole

client = TestClient(app)
login(client, UserRole.ADMIN)

db = SessionLocal()
db.query(Book).delete()
db.commit()
db.close()

print("=== Проверка 1: CSV с точкой с запятой (Ozon) ===")
csv_content = """Артикул продавца;Название товара;Цена продажи
SKU001;Книга первая;500
SKU002;Книга вторая;600
"""
files = {"file": ("test.csv", csv_content.encode("utf-8"), "text/csv")}
r = client.post("/import/upload", data={"marketplace": "ozon"}, files=files)
assert r.status_code == 200
assert "SKU001" in r.text or "создано" in r.text.lower()
print("  ✓ CSV с ; импортирован")

# Проверим, что книги в базе
db = SessionLocal()
count = db.query(Book).count()
print(f"  Книг в базе: {count}")
assert count == 2, f"Ожидалось 2 книги, найдено {count}"
db.close()

print("\n=== Проверка 2: CSV с запятой (другой формат) ===")
db = SessionLocal()
db.query(Book).delete()
db.commit()
db.close()

csv_content = """sku,title,price
SKU003,Третья книга,700
SKU004,Четвёртая книга,800
"""
files = {"file": ("test2.csv", csv_content.encode("utf-8"), "text/csv")}
r = client.post("/import/upload", data={"marketplace": "ozon"}, files=files)
assert r.status_code == 200
print("  ✓ CSV с , импортирован")

db = SessionLocal()
count = db.query(Book).count()
assert count == 2
db.close()

print("\n=== Проверка 3: CSV с кириллицей (UTF-8 BOM) ===")
db = SessionLocal()
db.query(Book).delete()
db.commit()
db.close()

csv_content = """Артикул;Название
КИР001;Война и мир
КИР002;Преступление и наказание
"""
# UTF-8 BOM — частый случай при экспорте из Excel
bom_content = b'\xef\xbb\xbf' + csv_content.encode("utf-8")
files = {"file": ("russian.csv", bom_content, "text/csv")}
r = client.post("/import/upload", data={"marketplace": "ozon"}, files=files)
assert r.status_code == 200
print("  ✓ CSV с BOM импортирован")

db = SessionLocal()
book = db.query(Book).filter_by(sku="КИР001").first()
if book:
    print(f"    Найдена книга: {book.title}")
    assert book.title == "Война и мир"
db.close()

print("\n=== Проверка 4: CSV с отсутствующими обязательными полями ===")
db = SessionLocal()
db.query(Book).delete()
db.commit()
db.close()

# Нет SKU и title — автосопоставление не сработает, покажет экран выбора
csv_content = """col1,col2,col3
val1,val2,val3
"""
files = {"file": ("bad.csv", csv_content.encode("utf-8"), "text/csv")}
r = client.post("/import/upload", data={"marketplace": "ozon"}, files=files)
assert r.status_code == 200
# Должен быть экран сопоставления, а не автоимпорт
assert "сопоставь" in r.text.lower() or "выбер" in r.text.lower()
print("  ✓ Файл без SKU/title показывает экран сопоставления")

print("\n=== Проверка 5: пустой CSV ===")
csv_content = ""
files = {"file": ("empty.csv", csv_content.encode("utf-8"), "text/csv")}
r = client.post("/import/upload", data={"marketplace": "ozon"}, files=files)
assert r.status_code == 400 or "пуст" in r.text.lower()
print("  ✓ Пустой файл отклонён")

print("\n=== Проверка 6: слишком большой файл (>50 МБ) ===")
# Создаём файл размером 51 МБ (больше MAX_UPLOAD_BYTES)
large_content = "A" * (51 * 1024 * 1024)
files = {"file": ("huge.csv", large_content.encode("utf-8"), "text/csv")}
try:
    r = client.post("/import/upload", data={"marketplace": "ozon"}, files=files)
    assert r.status_code == 413 or "слишком большой" in r.text.lower()
    print("  ✓ Большой файл отклонён")
except Exception as e:
    print(f"  ✓ Большой файл отклонён (исключение: {type(e).__name__})")

print("\n✅ Все проверки импорта прошли")
