"""Дымовой тест основного потока: вход, каталог, импорт CSV, проверка данных."""
import io

from starlette.testclient import TestClient

from app.main import app
from app.models import UserRole
from test_helpers import TEST_PASSWORD, ensure_user, login

# Вход по телефону и паролю: общего пароля «на склад» больше нет.
OWNER_PHONE = ensure_user(UserRole.ADMIN)

c = TestClient(app)

# 1. Без входа "/" редиректит на /login
r = c.get("/", follow_redirects=False)
assert r.status_code == 303 and r.headers["location"] == "/login", r.status_code
print("[ok] неавторизованный редирект на логин")

# 2. Неверный пароль — обратно на форму входа с пометкой об ошибке
r = c.post(
    "/login",
    data={"phone": OWNER_PHONE, "password": "wrong"},
    follow_redirects=False,
)
assert r.status_code == 303 and r.headers["location"] == "/login?error=bad", r.status_code
print("[ok] неверный пароль отклонён")

# 3. Незнакомый телефон отклоняется так же, как неверный пароль (не выдаём, кто есть в базе)
r = c.post(
    "/login",
    data={"phone": "79990000000", "password": TEST_PASSWORD},
    follow_redirects=False,
)
assert r.status_code == 303 and r.headers["location"] == "/login?error=bad", r.status_code
print("[ok] незнакомый телефон отклонён")

# 4. Верный телефон + пароль
login(c, UserRole.ADMIN)
print("[ok] вход выполнен")

# 4. Каталог открывается
r = c.get("/")
assert r.status_code == 200 and "Каталог" in r.text, r.status_code
print("[ok] каталог доступен")

# 5. Импорт CSV: две книги с Ozon. Колонки распознаются автоматически, поэтому
# импорт завершается за один шаг (экран сопоставления не нужен).
csv_data = (
    "Артикул;Наименование;Автор;ISBN;Цена\n"
    "OZ-001;Мастер и Маргарита;Булгаков;9785171123451;450\n"
    "OZ-002;Преступление и наказание;Достоевский;9785171123452;390\n"
)
files = {"file": ("ozon.csv", io.BytesIO(csv_data.encode("utf-8")), "text/csv")}
r = c.post("/import/upload", data={"marketplace": "ozon"}, files=files)
assert r.status_code == 200 and "Импорт завершён" in r.text, r.status_code
print("[ok] импорт файлом одним шагом (автосопоставление колонок)")

# 6. Проверяем, что книги в базе и видны в каталоге (поиск по названию)
r = c.get("/?q=Мастер")
assert "Мастер и Маргарита" in r.text, "книга не найдена в каталоге"
print("[ok] импортированная книга видна в каталоге")

# 7. Повторный импорт той же книги с WB по ISBN — не должно быть дубля
csv_wb = "sku;title;isbn;price\nWB-999;Мастер и Маргарита;9785171123451;500\n"
files = {"file": ("wb.csv", io.BytesIO(csv_wb.encode("utf-8")), "text/csv")}
r = c.post("/import/upload", data={"marketplace": "wildberries"}, files=files, follow_redirects=True)
assert "обновлено" in r.text.lower(), "дедупликация по ISBN не сработала"
print("[ok] дедупликация по ISBN: книга обновлена, не задвоена")

print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
