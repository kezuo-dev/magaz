"""Проверка: активная вкладка в меню, русские статусы, чистый мониторинг и очистка.

Выставление, форма книги, массовые действия и архив убраны — проверяем навигацию,
локализацию статусов, отсутствие любых действий над карточками и очистку каталога.
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
# Удалённых пунктов меню быть не должно.
assert 'href="/books/new"' not in r.text, "осталась удалённая вкладка «Добавить книгу»"
assert 'href="/archive"' not in r.text, "осталась удалённая вкладка «Архив»"
print("[ok] навигация: только актуальные пункты меню")

# 2. Заводим книгу напрямую в базе (импорт/сверка — единственные пути наполнения).
with SessionLocal() as s:
    s.query(Book).delete()
    s.add(Book(sku="PHOTO-1", title="Книга", status=BookStatus.DRAFT, price=100))
    s.commit()

# 3. Каталог — чистый мониторинг: русские статусы, никаких действий над книгами.
r = c.get("/")
assert "Черновик" in r.text, "статус книги не локализован (ожидали 'Черновик')"
assert "draft</span>" not in r.text, "в каталоге остался английский статус"
assert 'action="/books/bulk"' not in r.text, "осталась форма массовых действий"
assert 'value="publish"' not in r.text and 'value="withdraw"' not in r.text, "остались кнопки действий"
assert "rowcheck" not in r.text, "остались чекбоксы строк"
assert "Обновить каталог" in r.text, "нет кнопки «Обновить каталог»"
print("[ok] каталог — чистый мониторинг без действий над книгами")

# 4. Карточка книги — только просмотр (нет форм сохранения/действий)
with SessionLocal() as s:
    bid = s.query(Book).filter_by(sku="PHOTO-1").one().id
r = c.get(f"/books/{bid}")
assert r.status_code == 200, r.status_code
assert 'action="/books/save"' not in r.text and 'action="/books/bulk"' not in r.text, "в карточке остались формы"
print("[ok] карточка книги открывается только на просмотр")

# 5. Полная очистка каталога по паролю 2601
r = c.get("/")
assert "Очистить" in r.text, "нет кнопки очистки"

# Неверный пароль — книга остаётся
r = c.post("/catalog/wipe", data={"password": "0000"}, follow_redirects=True)
assert "Не удалось очистить" in r.text, "нет сообщения об ошибке пароля"
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
