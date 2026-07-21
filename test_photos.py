"""Проверка: активная вкладка в меню, русские статусы и очистка каталога.

Выставление и форма книги убраны, поэтому проверяем только то, что осталось:
навигацию, локализацию статусов, подтверждение массовых действий и wipe.
"""
from starlette.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import Book, BookStatus

c = TestClient(app)
c.post("/login", data={"password": "050620"})

# 1. Активная вкладка: на "/" подсвечен Каталог, на /import — Обновление каталога
r = c.get("/")
assert 'href="/" class="active"' in r.text, "Каталог не подсвечен на главной"
r = c.get("/import")
assert 'href="/import" class="active"' in r.text, "Обновление каталога не подсвечено"
# Кнопки «Добавить книгу» больше нет.
assert 'href="/books/new"' not in r.text, "осталась удалённая вкладка «Добавить книгу»"
print("[ok] активная вкладка подсвечивается, кнопки добавления книги нет")

# 2. Заводим книгу напрямую в базе (импорт/сверка — единственные пути наполнения).
with SessionLocal() as s:
    s.query(Book).delete()
    s.add(Book(sku="PHOTO-1", title="Книга", status=BookStatus.DRAFT, price=100))
    s.commit()

# 3. Статусы в каталоге на русском + подтверждение массовых действий
r = c.get("/")
assert "Черновик" in r.text, "статус книги не локализован (ожидали 'Черновик')"
assert "draft</span>" not in r.text, "в каталоге остался английский статус"
assert "data-confirm" in r.text, "нет атрибута подтверждения на кнопках"
assert "confirm(this.dataset.confirm" in r.text, "нет скрипта подтверждения"
# Кнопки «Выставить» быть не должно, а «Снять»/«Обновить каталог» — должны.
assert 'value="publish"' not in r.text, "осталась кнопка «Выставить»"
assert 'value="withdraw"' in r.text, "нет кнопки «Снять»"
assert "Обновить каталог" in r.text, "нет кнопки «Обновить каталог»"
print("[ok] статусы по-русски, выставление убрано, снятие и сверка на месте")

# 4. Карточка книги — только просмотр (нет формы сохранения)
with SessionLocal() as s:
    bid = s.query(Book).filter_by(sku="PHOTO-1").one().id
r = c.get(f"/books/{bid}")
assert r.status_code == 200, r.status_code
assert 'action="/books/save"' not in r.text, "в карточке осталась форма сохранения"
print("[ok] карточка книги открывается только на просмотр")

# 5. Полная очистка каталога по паролю 2601
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
