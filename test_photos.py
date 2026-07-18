"""Проверка: активная вкладка в меню и загрузка фото файлом."""
import io

from starlette.testclient import TestClient

from app.main import app

c = TestClient(app)
c.post("/login", data={"password": "050620"})

# 1. Активная вкладка: на "/" подсвечен Каталог, на /import — Импорт
r = c.get("/")
assert 'href="/" class="active"' in r.text, "Каталог не подсвечен на главной"
r = c.get("/import")
assert 'href="/import" class="active"' in r.text, "Импорт не подсвечен"
r = c.get("/books/new")
assert 'href="/books/new" class="active"' in r.text, "+ Книга не подсвечена"
print("[ok] активная вкладка подсвечивается")

# 2. Создаём книгу с загрузкой фото-файла (крошечный валидный PNG)
png = bytes.fromhex(
    "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
    "890000000d49444154789c6360000002000154a24f5f0000000049454e44ae426082"
)
files = {"photo_files": ("cover.png", io.BytesIO(png), "image/png")}
data = {"book_id": "", "sku": "PHOTO-1", "title": "Книга с фото"}
r = c.post("/books/save", data=data, files=files, follow_redirects=True)
assert r.status_code == 200, r.status_code
assert "/static/uploads/" in r.text, "ссылка на загруженное фото не появилась в карточке"
assert "photo-thumb" in r.text, "превью фото не отрисовалось"
assert "Фото по ссылке" not in r.text, "поле ссылок должно быть удалено"
assert 'id="photo_preview"' in r.text, "контейнер предпросмотра отсутствует"
print("[ok] фото загружается файлом, ссылка сохранена, превью видно, поле ссылок убрано")

# 3. Повторное сохранение без новых файлов не должно стирать уже загруженные фото
from app.db import SessionLocal
from app.models import Book
with SessionLocal() as s:
    bid = s.query(Book).filter_by(sku="PHOTO-1").one().id
r = c.post("/books/save", data={"book_id": str(bid), "sku": "PHOTO-1", "title": "Книга с фото"}, follow_redirects=True)
assert "/static/uploads/" in r.text, "старое фото пропало при сохранении без новых файлов"
print("[ok] существующие фото не теряются при редактировании")

# 4. Статусы в каталоге на русском + подтверждение массовых действий
r = c.get("/")
assert "В наличии" in r.text or "Черновик" in r.text or "Все статусы" in r.text, "нет русских статусов в фильтре"
assert "Черновик" in r.text, "статус книги не локализован (ожидали 'Черновик')"
assert "draft</span>" not in r.text, "в каталоге остался английский статус"
assert "data-confirm" in r.text, "нет атрибута подтверждения на кнопках"
assert "confirm(this.dataset.confirm" in r.text, "нет скрипта подтверждения"
print("[ok] статусы по-русски, подтверждение действий добавлено")

# 5. Полная очистка каталога по паролю 2601
# Кнопка присутствует
r = c.get("/")
assert "Очистить каталог" in r.text, "нет кнопки очистки"

# Неверный пароль — книга остаётся
r = c.post("/catalog/wipe", data={"password": "0000"}, follow_redirects=True)
assert "Неверный пароль" in r.text, "нет сообщения об ошибке пароля"
with SessionLocal() as s:
    assert s.query(Book).count() > 0, "книги удалены при НЕВЕРНОМ пароле!"
print("[ok] неверный пароль очистки не удаляет данные")

# Верный пароль — всё чистится
r = c.post("/catalog/wipe", data={"password": "2601"}, follow_redirects=True)
assert "полностью очищен" in r.text, "нет подтверждения очистки"
with SessionLocal() as s:
    assert s.query(Book).count() == 0, "книги остались после очистки!"
print("[ok] верный пароль 2601 полностью очищает каталог")

print("\nВСЕ ПРОВЕРКИ ПРОЙДЕНЫ")
