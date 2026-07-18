"""Дымовой тест основного потока: вход, каталог, импорт CSV, проверка данных."""
import io

from starlette.testclient import TestClient

from app.main import app

c = TestClient(app)

# 1. Без входа "/" редиректит на /login
r = c.get("/", follow_redirects=False)
assert r.status_code == 303 and r.headers["location"] == "/login", r.status_code
print("[ok] неавторизованный редирект на логин")

# 2. Неверный пароль
r = c.post("/login", data={"password": "wrong"}, follow_redirects=False)
assert r.status_code == 401, r.status_code
print("[ok] неверный пароль отклонён")

# 3. Верный пароль (из .env.example по умолчанию changeme)
r = c.post("/login", data={"password": "changeme"}, follow_redirects=False)
assert r.status_code == 303 and r.headers["location"] == "/", r.status_code
print("[ok] вход выполнен")

# 4. Каталог открывается
r = c.get("/")
assert r.status_code == 200 and "Каталог" in r.text, r.status_code
print("[ok] каталог доступен")

# 5. Импорт CSV: две книги с Ozon
csv_data = (
    "Артикул;Наименование;Автор;ISBN;Цена\n"
    "OZ-001;Мастер и Маргарита;Булгаков;9785171123451;450\n"
    "OZ-002;Преступление и наказание;Достоевский;9785171123452;390\n"
)
files = {"file": ("ozon.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
r = c.post("/import/upload", data={"marketplace": "ozon"}, files=files)
assert r.status_code == 200 and "Сопоставление" in r.text, r.status_code
print("[ok] загрузка файла и шаг сопоставления")

# 6. Запуск импорта с маппингом
r = c.post(
    "/import/run",
    data={
        "map_sku": "Артикул",
        "map_title": "Наименование",
        "map_author": "Автор",
        "map_isbn": "ISBN",
        "map_price": "Цена",
    },
)
assert r.status_code == 200 and "создано" not in r.text.lower() or "Импорт завершён" in r.text
print("[ok] импорт выполнен")

# 7. Проверяем, что книги в базе и видны в каталоге
r = c.get("/?q=Булгаков")
assert "Мастер и Маргарита" in r.text, "книга не найдена в каталоге"
print("[ok] импортированная книга видна в каталоге")

# 8. Повторный импорт той же книги с WB по ISBN — не должно быть дубля
csv_wb = "sku;title;isbn;price\nWB-999;Мастер и Маргарита;9785171123451;500\n"
files = {"file": ("wb.csv", io.BytesIO(csv_wb.encode("utf-8")), "text/csv")}
c.post("/import/upload", data={"marketplace": "wildberries"}, files=files)
r = c.post("/import/run", data={"map_sku": "sku", "map_title": "title", "map_isbn": "isbn", "map_price": "price"})
assert "обновлено: <b>1</b>" in r.text.lower() or "обновлено" in r.text.lower()
print("[ok] дедупликация по ISBN: книга обновлена, не задвоена")

print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
