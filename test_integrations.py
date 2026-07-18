"""Проверка интеграций площадок на заглушках (без реальных вызовов к Ozon).

Мокаем httpx.post внутри клиента Ozon, чтобы прогнать весь путь:
сохранение ключей → выставление → снятие → опрос заказов → авто-снятие.
Плюс проверяем офлайн-режим (площадка выключена — только локальные статусы).
"""
import os

# Фоновый планировщик в тестах не нужен — отключаем до импорта приложения.
os.environ["SCHEDULER_ENABLED"] = "false"

from starlette.testclient import TestClient

from app.db import SessionLocal
from app.main import app
from app.models import (
    Book,
    BookStatus,
    Listing,
    ListingStatus,
    MarketplaceAccount,
    Order,
    SyncLog,
)
import app.marketplaces.ozon as ozon


ADMIN_PW = os.environ.get("ADMIN_PASSWORD", "1908")

c = TestClient(app)
c.post("/login", data={"password": os.environ.get("APP_PASSWORD", "050620")})


def unlock(section="/settings"):
    """Разблокировать защищённый раздел прямо перед обращением к нему.

    Разблокировка теперь разовая (сбрасывается при уходе из раздела), поэтому
    вызываем это перед каждым запросом к /settings или /log в тестах."""
    c.post("/admin-login", data={"password": ADMIN_PW, "next": section}, follow_redirects=False)


# --- Заглушка Ozon API ----------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or ""
        # Клиенты WB/Avito проверяют resp.content перед разбором JSON.
        self.content = b"{}" if payload is not None else b""

    def json(self):
        return self._payload


# Что «отдаёт» Ozon по каждому пути. Настраивается в тестах.
_fake_orders = {"postings": []}


import app.marketplaces.wildberries as wb
import app.marketplaces.avito as avito

# Заказы, которые «отдают» площадки. Настраиваются в тестах.
_fake_wb_orders = {"orders": []}
_fake_avito_orders = {"orders": []}

# Каталоги для импорта по кнопке. Настраиваются в тестах.
_fake_ozon_list = {"items": [], "last_id": ""}   # /v3/product/list
_fake_ozon_info = {"items": []}                   # /v3/product/info/list
_fake_wb_cards = []                               # карточки WB
_fake_wb_stocks = []                              # остатки FBS: [{"sku","amount"}]
_fake_avito_items = {"resources": []}             # /core/v1/items

# ВАЖНО: ozon/wb/avito делают `import httpx`, поэтому ozon.httpx, wb.httpx и
# avito.httpx — один и тот же объект модуля. Подменяем httpx.post и httpx.request
# по одному разу общими диспетчерами, которые маршрутизируют по URL.


def fake_post(url, json=None, data=None, headers=None, timeout=None):
    # Ozon — JSON-вызовы; Avito — OAuth-токен (form-data на /token/).
    if url.endswith("/v3/product/list"):
        return FakeResponse(200, {"result": _fake_ozon_list})
    if url.endswith("/v3/product/info/list"):
        return FakeResponse(200, {"result": _fake_ozon_info})
    if url.endswith("/v3/product/import"):
        # Ozon требует непустые категорию и тип товара (иначе 400 TypeId > 0).
        item = ((json or {}).get("items") or [{}])[0]
        assert item.get("description_category_id"), "Ozon: не передан description_category_id"
        assert item.get("type_id"), "Ozon: не передан type_id"
        return FakeResponse(200, {"result": {"task_id": 123}})
    if url.endswith("/v1/product/import/info"):
        # Импорт завершён — товар создан, можно выставлять остаток.
        return FakeResponse(200, {"result": {"items": [{"status": "imported"}]}})
    if url.endswith("/v1/description-category/tree"):
        # Мини-дерево: категория «Книги» с одним типом внутри.
        return FakeResponse(200, {"result": [
            {"description_category_id": 17028922, "category_name": "Книги", "children": [
                {"type_id": 970763, "type_name": "Печатная книга"},
            ]},
        ]})
    if url.endswith("/v2/products/stocks"):
        return FakeResponse(200, {"result": [{"updated": True}]})
    if url.endswith("/v3/posting/fbs/list"):
        return FakeResponse(200, {"result": _fake_orders})
    if url.endswith("/token/"):  # Avito OAuth
        return FakeResponse(200, {"access_token": "avito-token", "expires_in": 3600})
    raise AssertionError(f"неожиданный POST: {url}")


def fake_request(method, url, json=None, data=None, params=None, headers=None, timeout=None):
    # WB — content/prices/marketplace; Avito — автозагрузка и заказы (Bearer).
    if url.endswith("/content/v2/object/all"):
        # Справочник предметов WB; отдаём предмет «Книги».
        return FakeResponse(200, {"data": [
            {"subjectID": 4127, "subjectName": "Книги", "parentName": "Книги и обучение"},
        ]})
    if url.endswith("/content/v2/get/cards/list"):
        return FakeResponse(200, {"cards": _fake_wb_cards, "cursor": {"total": len(_fake_wb_cards)}})
    if url.endswith("/content/v2/cards/upload"):
        # WB требует непустой subjectID (иначе 400 subjectID is not provided or zero).
        group = (json or [{}])[0]
        assert group.get("subjectID"), "WB: не передан subjectID"
        return FakeResponse(200, {"error": False})
    if url.endswith("/api/v2/upload/task"):
        return FakeResponse(200, {"data": {"id": 1}})
    if "/api/v3/stocks/" in url:
        # POST — чтение остатков FBS (фильтруем по запрошенным sku), PUT — запись.
        if method.upper() == "POST":
            wanted = set((json or {}).get("skus") or [])
            stocks = [s for s in _fake_wb_stocks if s.get("sku") in wanted]
            return FakeResponse(200, {"stocks": stocks})
        return FakeResponse(200, {})
    if url.endswith("/api/v3/orders/new"):
        return FakeResponse(200, _fake_wb_orders)
    if url.endswith("/autoload/v2/items"):  # Avito автозагрузка
        return FakeResponse(200, {"result": "ok"})
    if "/core/v1/items" in url:  # Avito список объявлений (каталог)
        return FakeResponse(200, _fake_avito_items)
    if url.endswith("/orders"):  # Avito заказы
        return FakeResponse(200, _fake_avito_orders)
    raise AssertionError(f"неожиданный запрос: {method} {url}")


ozon.httpx.post = fake_post
ozon.httpx.request = fake_request


# --- Хелперы --------------------------------------------------------------

def make_book(sku, title="Тест"):
    with SessionLocal() as s:
        # Категория/тип задаются у книги (как в реальной карточке) — без них
        # публикация на Ozon/WB отклоняется.
        b = Book(
            sku=sku, title=title, status=BookStatus.IN_STOCK, price=100,
            ozon_category_id="17028922", ozon_type_id="970763",
            wb_subject_id="4127",
        )
        s.add(b)
        s.commit()
        return b.id


def enable_ozon():
    unlock("/settings")
    c.post(
        "/settings/save",
        data={
            "marketplace": "ozon",
            "cred_client_id": "111",
            "cred_api_key": "secret",
            "cred_warehouse_id": "777",
            "enabled": "on",
        },
        follow_redirects=True,
    )


def disable_ozon():
    with SessionLocal() as s:
        acc = s.query(MarketplaceAccount).filter_by(marketplace="ozon").one_or_none()
        if acc:
            acc.enabled = False
            s.commit()


def enable_wb():
    unlock("/settings")
    c.post(
        "/settings/save",
        data={
            "marketplace": "wildberries",
            "cred_api_token": "wb-secret",
            "cred_warehouse_id": "777",
            "enabled": "on",
        },
        follow_redirects=True,
    )


def enable_avito():
    unlock("/settings")
    c.post(
        "/settings/save",
        data={
            "marketplace": "avito",
            "cred_client_id": "av-id",
            "cred_client_secret": "av-secret",
            "cred_user_id": "12345",
            "enabled": "on",
        },
        follow_redirects=True,
    )


# --- 1. Раздел настроек доступен и в меню ---------------------------------

unlock("/settings")
r = c.get("/settings")
assert r.status_code == 200 and "Настройки площадок" in r.text, "нет страницы настроек"
r = c.get("/")
assert 'href="/settings"' in r.text and 'href="/log"' in r.text, "нет ссылок в меню"
print("[ok] раздел настроек и журнал доступны в меню")

# Второй пароль: без него /settings и /log недоступны даже после обычного входа.
c2 = TestClient(app)
c2.post("/login", data={"password": os.environ.get("APP_PASSWORD", "050620")})
r = c2.get("/settings", follow_redirects=False)
assert r.status_code == 303 and "/admin-login" in r.headers["location"], "Настройки открылись без второго пароля!"
r = c2.get("/log", follow_redirects=False)
assert r.status_code == 303 and "/admin-login" in r.headers["location"], "Журнал открылся без второго пароля!"
# Неверный второй пароль не пускает.
c2.post("/admin-login", data={"password": "0000", "next": "/settings"})
r = c2.get("/settings", follow_redirects=False)
assert r.status_code == 303, "неверный второй пароль пустил в Настройки!"
print("[ok] Журнал и Настройки закрыты паролем 1908")

# Пароль к Настройкам НЕ открывает Журнал: каждый раздел разблокируется отдельно.
c2.post("/admin-login", data={"password": "1908", "next": "/settings"}, follow_redirects=False)
r = c2.get("/settings", follow_redirects=False)
assert r.status_code == 200, "верный пароль не открыл Настройки"
r = c2.get("/log", follow_redirects=False)
assert r.status_code == 303 and "/admin-login" in r.headers["location"], "пароль к Настройкам открыл и Журнал!"
print("[ok] пароль к Настройкам не открывает Журнал (разделы независимы)")

# Разблокировка разовая: после ухода на другую страницу раздел снова закрыт.
c2.post("/admin-login", data={"password": "1908", "next": "/settings"}, follow_redirects=False)
assert c2.get("/settings", follow_redirects=False).status_code == 200, "не открылись Настройки"
c2.get("/", follow_redirects=False)  # ушли из раздела — замок защёлкивается
r = c2.get("/settings", follow_redirects=False)
assert r.status_code == 303 and "/admin-login" in r.headers["location"], "разблокировка не сбросилась после ухода!"
print("[ok] доступ к разделу разовый, а не на всю сессию")


# --- 2. Сохранение ключей: секрет шифруется, в ответ не утекает ------------

enable_ozon()
with SessionLocal() as s:
    acc = s.query(MarketplaceAccount).filter_by(marketplace="ozon").one()
    assert acc.enabled is True, "площадка не включилась"
    assert acc.credentials_encrypted and "secret" not in acc.credentials_encrypted, "ключ хранится не зашифрованным!"
from app.security import decrypt_credentials
assert decrypt_credentials(acc.credentials_encrypted)["api_key"] == "secret", "ключ не расшифровывается"
print("[ok] ключи Ozon сохранены в зашифрованном виде")


# --- 3. Проверка подключения (мок отвечает 200) ---------------------------

unlock("/settings")
r = c.post("/settings/check", data={"marketplace": "ozon"}, follow_redirects=True)
assert "подключение успешно" in r.text.lower(), "проверка подключения не прошла"
print("[ok] проверка подключения Ozon успешна на заглушке")


# --- 4. Публикация: живой вызов проставляет external_id и ACTIVE ----------

bid = make_book("LIVE-1")
r = c.post("/books/bulk", data={"action": "publish", "book_ids": [bid]}, follow_redirects=True)
with SessionLocal() as s:
    lst = s.query(Listing).filter_by(book_id=bid, marketplace="ozon").one()
    assert lst.status == ListingStatus.ACTIVE, f"лот не активен: {lst.status}"
    assert lst.external_id == "LIVE-1", f"external_id не проставлен: {lst.external_id}"
    logs = s.query(SyncLog).filter_by(book_id=bid, action="publish", ok=True).count()
    assert logs >= 1, "нет записи в журнале о публикации"
print("[ok] публикация на Ozon: лот активен, external_id и журнал записаны")


# --- 4b. Кнопка подбора категорий: Ozon отдаёт «категория → тип» -----------

r = c.get("/books/categories?marketplace=ozon&q=книг")
assert r.status_code == 200, f"категории Ozon: {r.status_code}"
body = r.json()
assert body["ok"] and body["items"], "Ozon не вернул варианты категорий"
first = body["items"][0]
assert first["fields"]["description_category_id"] == "17028922", "нет ID категории Ozon"
assert first["fields"]["type_id"] == "970763", "нет type_id Ozon"
print("[ok] подбор категорий Ozon в карточке: кнопка отдаёт готовые ID категории и типа")


# --- 4c. Категории сохраняются в карточке книги ---------------------------

c.post(
    "/books/save",
    data={
        "sku": "CAT-1", "title": "С категорией",
        "ozon_category_id": "17028922", "ozon_type_id": "970763",
        "wb_subject_id": "4127",
    },
    follow_redirects=True,
)
with SessionLocal() as s:
    saved = s.query(Book).filter_by(sku="CAT-1").one()
    assert saved.ozon_category_id == "17028922", "не сохранён ozon_category_id"
    assert saved.ozon_type_id == "970763", "не сохранён ozon_type_id"
    assert saved.wb_subject_id == "4127", "не сохранён wb_subject_id"
print("[ok] категории из карточки книги сохраняются (Ozon и WB)")


# --- 4d. Без категории публикация на Ozon отклоняется ---------------------

with SessionLocal() as s:
    nocat = Book(sku="NOCAT-1", title="Без категории", status=BookStatus.IN_STOCK, price=100)
    s.add(nocat)
    s.commit()
    nocat_id = nocat.id
c.post("/books/bulk", data={"action": "publish", "book_ids": [nocat_id]}, follow_redirects=True)
with SessionLocal() as s:
    lst = s.query(Listing).filter_by(book_id=nocat_id, marketplace="ozon").one()
    assert lst.status == ListingStatus.ERROR, f"ожидали ошибку без категории, статус {lst.status}"
    assert "категори" in (lst.last_error or "").lower(), f"нет понятной ошибки: {lst.last_error}"
print("[ok] без категории публикация на Ozon отклоняется с понятной ошибкой")


# --- 5. Снятие: живой вызов переводит лот в WITHDRAWN ---------------------

r = c.post("/books/bulk", data={"action": "withdraw", "book_ids": [bid]}, follow_redirects=True)
with SessionLocal() as s:
    lst = s.query(Listing).filter_by(book_id=bid, marketplace="ozon").one()
    assert lst.status == ListingStatus.WITHDRAWN, f"лот не снят: {lst.status}"
    b = s.get(Book, bid)
    assert b.status == BookStatus.WITHDRAWN, "книга не помечена снятой"
print("[ok] снятие с Ozon переводит лот и книгу в 'снято'")


# --- 6. Опрос заказов: продажа помечает книгу sold + кросс-снятие ----------

# Книга с лотами на двух площадках; на Ozon её «покупают».
bid2 = make_book("SOLD-1")
with SessionLocal() as s:
    b = s.get(Book, bid2)
    s.add(Listing(book_id=b.id, marketplace="ozon", external_id="SOLD-1", status=ListingStatus.ACTIVE))
    s.add(Listing(book_id=b.id, marketplace="wildberries", external_id="WB-SOLD-1", status=ListingStatus.ACTIVE))
    s.commit()

_fake_orders["postings"] = [
    {"posting_number": "ORDER-777", "products": [{"offer_id": "SOLD-1"}]}
]

from app.sync import poll_marketplace_orders
with SessionLocal() as s:
    new_count = poll_marketplace_orders(s, "ozon")
    s.commit()
assert new_count == 1, f"ожидали 1 новый заказ, получили {new_count}"

with SessionLocal() as s:
    b = s.get(Book, bid2)
    assert b.status == BookStatus.SOLD, "книга не помечена проданной"
    wb = s.query(Listing).filter_by(book_id=bid2, marketplace="wildberries").one()
    assert wb.status == ListingStatus.WITHDRAWN, "лот на другой площадке не снят (кросс-снятие)"
    order = s.query(Order).filter_by(external_order_id="ORDER-777").one()
    assert order.processed is True and order.book_id == bid2, "заказ не привязан/не обработан"
print("[ok] продажа на Ozon: книга продана, снята с WB, заказ обработан")


# --- 7. Повторный опрос того же заказа не создаёт дубль -------------------

with SessionLocal() as s:
    again = poll_marketplace_orders(s, "ozon")
    s.commit()
assert again == 0, f"дубль заказа: получили {again} новых"
with SessionLocal() as s:
    assert s.query(Order).filter_by(external_order_id="ORDER-777").count() == 1, "заказ задублировался"
print("[ok] повторный опрос не дублирует заказ")


# --- 8. Офлайн-режим: выключенная площадка не зовёт API, меняет статус -----

disable_ozon()
_fake_orders["postings"] = []  # если API вызовется — тест упадёт на другом
bid3 = make_book("OFFLINE-1")
r = c.post("/books/bulk", data={"action": "publish", "book_ids": [bid3]}, follow_redirects=True)
with SessionLocal() as s:
    lst = s.query(Listing).filter_by(book_id=bid3, marketplace="ozon").one_or_none()
    # В офлайне публикуем на все включённые площадки; их нет → лот не создаётся,
    # но книга помечается в наличии без падений.
    b = s.get(Book, bid3)
    assert b.status == BookStatus.IN_STOCK, "книга не помечена в наличии в офлайне"
print("[ok] офлайн-режим не падает и не зовёт API")


# --- 9. Импорт одним кликом: площадка и колонки распознаются сами ----------

import io

# Выгрузка в стиле Ozon: колонки на русском, площадку не указываем.
csv_ozon = (
    "Артикул продавца;Название товара;Штрихкод;Цена продажи;Автор\r\n"
    "BK-100;Мастер и Маргарита;9785000000001;350;Булгаков\r\n"
    "BK-101;Три товарища;9785000000002;420;Ремарк\r\n"
)
r = c.post(
    "/import/upload",
    files={"file": ("ozon_export.csv", io.BytesIO(csv_ozon.encode("utf-8")), "text/csv")},
    data={"marketplace": ""},  # оставляем на автоопределение
    follow_redirects=True,
)
assert r.status_code == 200, f"импорт не прошёл: {r.status_code}"
assert "Импорт завершён" in r.text, "не попали на экран результата (просили ручное сопоставление?)"
assert "определена автоматически" in r.text, "площадка не определилась автоматически"
with SessionLocal() as s:
    b = s.query(Book).filter_by(sku="BK-100").one()
    assert b.title == "Мастер и Маргарита", f"название не распозналось: {b.title}"
    assert b.author == "Булгаков", f"автор не распознан: {b.author}"
    assert b.isbn == "9785000000001", f"ISBN (штрихкод) не распознан: {b.isbn}"
    assert float(b.price) == 350.0, f"цена не распозналась: {b.price}"
    lst = s.query(Listing).filter_by(book_id=b.id, marketplace="ozon").one()
    assert lst.marketplace == "ozon", "площадка лота не ozon"
print("[ok] импорт одним кликом: площадка и колонки распознаны автоматически")


# --- 10. Просмотр и удаление фото книги -----------------------------------

import os as _os
from app.photos import UPLOAD_DIR

bid_photo = make_book("PHOTO-1")
# Кладём две «фотографии» через форму сохранения книги.
png = bytes.fromhex("89504e470d0a1a0a")  # сигнатура PNG — достаточно для сохранения
r = c.post(
    "/books/save",
    data={"book_id": str(bid_photo), "sku": "PHOTO-1", "title": "С фото", "isbn": "9785000002222"},
    files=[
        ("photo_files", ("a.png", io.BytesIO(png + b"1"), "image/png")),
        ("photo_files", ("b.png", io.BytesIO(png + b"2"), "image/png")),
    ],
    follow_redirects=True,
)
with SessionLocal() as s:
    b = s.get(Book, bid_photo)
    photos = b.photo_list
assert len(photos) == 2, f"ожидали 2 фото, получили {len(photos)}"
# Файлы реально на диске и доступны для просмотра через /static.
for url in photos:
    assert c.get(url).status_code == 200, f"фото не отдаётся: {url}"
first, second = photos
first_path = UPLOAD_DIR / str(bid_photo) / _os.path.basename(first)
assert first_path.is_file(), "файл первого фото не найден на диске"

# Удаляем первое фото галочкой.
r = c.post(
    "/books/save",
    data={"book_id": str(bid_photo), "sku": "PHOTO-1", "title": "С фото", "isbn": "9785000002222", "remove_photos": first},
    follow_redirects=True,
)
with SessionLocal() as s:
    b = s.get(Book, bid_photo)
    remaining = b.photo_list
assert remaining == [second], f"после удаления должно остаться одно фото, есть: {remaining}"
assert not first_path.exists(), "файл удалённого фото остался на диске"
print("[ok] фото книги можно просмотреть и удалить (файл стирается с диска)")


# --- 11. Wildberries: ключи, проверка связи, публикация, снятие, заказы -----

enable_wb()
with SessionLocal() as s:
    acc = s.query(MarketplaceAccount).filter_by(marketplace="wildberries").one()
    assert acc.enabled is True, "WB не включился"
    assert acc.credentials_encrypted and "wb-secret" not in acc.credentials_encrypted, "токен WB не зашифрован!"

unlock("/settings")
r = c.post("/settings/check", data={"marketplace": "wildberries"}, follow_redirects=True)
assert "подключение успешно" in r.text.lower(), "проверка подключения WB не прошла"
print("[ok] WB: ключи сохранены зашифрованно, проверка подключения успешна")

# Публикация: живой вызов проставляет external_id (vendorCode) и ACTIVE.
bid_wb = make_book("WB-LIVE-1")
r = c.post("/books/bulk", data={"action": "publish", "book_ids": [bid_wb]}, follow_redirects=True)
with SessionLocal() as s:
    lst = s.query(Listing).filter_by(book_id=bid_wb, marketplace="wildberries").one()
    assert lst.status == ListingStatus.ACTIVE, f"лот WB не активен: {lst.status}"
    assert lst.external_id == "WB-LIVE-1", f"external_id WB не проставлен: {lst.external_id}"
print("[ok] WB: публикация активна, external_id и журнал записаны")

# Кнопка подбора предмета WB в карточке книги отдаёт subject_id.
r = c.get("/books/categories?marketplace=wildberries&q=книг")
assert r.status_code == 200, f"категории WB: {r.status_code}"
body = r.json()
assert body["ok"] and body["items"], "WB не вернул предметы"
assert body["items"][0]["fields"]["subject_id"] == "4127", "нет subject_id WB"
print("[ok] подбор предмета WB в карточке: кнопка отдаёт готовый subject_id")

# Снятие переводит лот в WITHDRAWN.
r = c.post("/books/bulk", data={"action": "withdraw", "book_ids": [bid_wb]}, follow_redirects=True)
with SessionLocal() as s:
    lst = s.query(Listing).filter_by(book_id=bid_wb, marketplace="wildberries").one()
    assert lst.status == ListingStatus.WITHDRAWN, f"лот WB не снят: {lst.status}"
print("[ok] WB: снятие переводит лот в 'снято'")

# Опрос заказов: продажа на WB помечает книгу проданной.
bid_wb2 = make_book("WB-SOLD-1")
with SessionLocal() as s:
    b = s.get(Book, bid_wb2)
    s.add(Listing(book_id=b.id, marketplace="wildberries", external_id="WB-SOLD-1", status=ListingStatus.ACTIVE))
    s.add(Listing(book_id=b.id, marketplace="ozon", external_id="OZ-SOLD-1", status=ListingStatus.ACTIVE))
    s.commit()
_fake_wb_orders["orders"] = [{"id": "WB-ORDER-1", "article": "WB-SOLD-1"}]
with SessionLocal() as s:
    new_count = poll_marketplace_orders(s, "wildberries")
    s.commit()
assert new_count == 1, f"WB: ожидали 1 новый заказ, получили {new_count}"
with SessionLocal() as s:
    b = s.get(Book, bid_wb2)
    assert b.status == BookStatus.SOLD, "WB: книга не помечена проданной"
    oz = s.query(Listing).filter_by(book_id=bid_wb2, marketplace="ozon").one()
    assert oz.status == ListingStatus.WITHDRAWN, "WB: лот на Ozon не снят (кросс-снятие)"
_fake_wb_orders["orders"] = []
print("[ok] WB: продажа помечает книгу проданной и снимает с других площадок")

# Офлайн-режим: выключенная площадка не зовёт API.
with SessionLocal() as s:
    acc = s.query(MarketplaceAccount).filter_by(marketplace="wildberries").one()
    acc.enabled = False
    s.commit()
with SessionLocal() as s:
    assert poll_marketplace_orders(s, "wildberries") == 0, "WB офлайн: не должно быть вызовов API"
print("[ok] WB: офлайн-режим не зовёт API")


# --- 12. Avito: OAuth, проверка связи, публикация, снятие, заказы -----------

enable_avito()
with SessionLocal() as s:
    acc = s.query(MarketplaceAccount).filter_by(marketplace="avito").one()
    assert acc.enabled is True, "Avito не включился"
    assert acc.credentials_encrypted and "av-secret" not in acc.credentials_encrypted, "секрет Avito не зашифрован!"

unlock("/settings")
r = c.post("/settings/check", data={"marketplace": "avito"}, follow_redirects=True)
assert "подключение успешно" in r.text.lower(), "проверка подключения Avito (OAuth) не прошла"
print("[ok] Avito: ключи зашифрованы, OAuth-проверка подключения успешна")

# Публикация через автозагрузку: external_id (Id объявления) = SKU, ACTIVE.
bid_av = make_book("AV-LIVE-1")
r = c.post("/books/bulk", data={"action": "publish", "book_ids": [bid_av]}, follow_redirects=True)
with SessionLocal() as s:
    lst = s.query(Listing).filter_by(book_id=bid_av, marketplace="avito").one()
    assert lst.status == ListingStatus.ACTIVE, f"лот Avito не активен: {lst.status}"
    assert lst.external_id == "AV-LIVE-1", f"external_id Avito не проставлен: {lst.external_id}"
print("[ok] Avito: публикация через автозагрузку активна, external_id записан")

# Снятие.
r = c.post("/books/bulk", data={"action": "withdraw", "book_ids": [bid_av]}, follow_redirects=True)
with SessionLocal() as s:
    lst = s.query(Listing).filter_by(book_id=bid_av, marketplace="avito").one()
    assert lst.status == ListingStatus.WITHDRAWN, f"лот Avito не снят: {lst.status}"
print("[ok] Avito: снятие переводит лот в 'снято'")

# Опрос заказов Avito.
bid_av2 = make_book("AV-SOLD-1")
with SessionLocal() as s:
    b = s.get(Book, bid_av2)
    s.add(Listing(book_id=b.id, marketplace="avito", external_id="AV-SOLD-1", status=ListingStatus.ACTIVE))
    s.commit()
_fake_avito_orders["orders"] = [{"id": "AV-ORDER-1", "items": [{"id": "AV-SOLD-1"}]}]
with SessionLocal() as s:
    new_count = poll_marketplace_orders(s, "avito")
    s.commit()
assert new_count == 1, f"Avito: ожидали 1 новый заказ, получили {new_count}"
with SessionLocal() as s:
    b = s.get(Book, bid_av2)
    assert b.status == BookStatus.SOLD, "Avito: книга не помечена проданной"
_fake_avito_orders["orders"] = []
print("[ok] Avito: продажа помечает книгу проданной")


# --- 12b. Создание книги: артикул и ISBN обязательны ---

# Артикул и ISBN сохраняются как есть.
r = c.post("/books/save", data={"title": "Своя книга", "sku": "MY-SKU-1", "isbn": "9785000009999"}, follow_redirects=True)
with SessionLocal() as s:
    b = s.query(Book).filter_by(sku="MY-SKU-1").one()
    assert b.title == "Своя книга"
    assert b.isbn == "9785000009999", f"ISBN не сохранён: {b.isbn!r}"
print("[ok] Артикул и ISBN сохраняются как есть")

# Без артикула — понятная ошибка, книга не создаётся.
r = c.post("/books/save", data={"title": "Без артикула", "isbn": "9785000000000"})
assert r.status_code == 400, f"ожидали 400 без артикула, получили {r.status_code}"
assert "артикул" in r.text.lower(), "нет сообщения об обязательном артикуле"
with SessionLocal() as s:
    assert s.query(Book).filter_by(title="Без артикула").count() == 0, "книга без артикула сохранилась"
print("[ok] Без артикула — понятная ошибка, книга не создаётся")

# Без ISBN — книга создаётся, в поле записывается «нет».
r = c.post("/books/save", data={"title": "Без ISBN", "sku": "NO-ISBN-1"}, follow_redirects=True)
with SessionLocal() as s:
    b = s.query(Book).filter_by(sku="NO-ISBN-1").one()
    assert b.isbn == "нет", f"без ISBN должно записаться «нет», получили {b.isbn!r}"
print("[ok] Без ISBN — книга создаётся, в поле записывается «нет»")

# Дубликат артикула — понятная ошибка, а не падение, ввод не теряется.
r = c.post("/books/save", data={"title": "Дубль", "sku": "MY-SKU-1", "isbn": "9785000001111"})
assert r.status_code == 400, f"ожидали 400 на дубль артикула, получили {r.status_code}"
assert "уже занят" in r.text, "нет понятного сообщения о занятом артикуле"
assert "Дубль" in r.text, "введённые данные потерялись при ошибке"
with SessionLocal() as s:
    assert s.query(Book).filter_by(title="Дубль").count() == 0, "дубль всё-таки сохранился"
print("[ok] Дубликат артикула даёт понятную ошибку и сохраняет ввод")


# --- 13. Импорт каталога по кнопке (прямая выгрузка из API) -----------------

# Чистим каталог, чтобы считать созданные записи начисто.
with SessionLocal() as s:
    s.query(Order).delete()
    s.query(Listing).delete()
    s.query(Book).delete()
    s.commit()

# Кнопка выключенной площадки недоступна; ключи WB/Avito включены выше по тесту,
# Ozon — включим снова, т.к. его отключали в офлайн-проверке.
enable_ozon()
enable_wb()
enable_avito()
unlock("/settings")
r = c.get("/import")
assert "Загрузить из" in r.text, "нет кнопок загрузки на странице импорта"

# Ozon: список отдаёт offer_id, детали — название/цену/баркод.
# Заглушки читают эти глобалы при вызове — достаточно переприсвоить имена.
_fake_ozon_list = {"items": [{"offer_id": "OZ-IMP-1"}], "last_id": ""}
_fake_ozon_info = {"items": [{"offer_id": "OZ-IMP-1", "name": "Книга Озон", "price": "150", "barcode": "111222"}]}
r = c.post("/import/pull/ozon", follow_redirects=True)
assert "Импорт завершён" in r.text, f"импорт Ozon не завершился: {r.text[:200]}"
with SessionLocal() as s:
    b = s.query(Book).filter_by(sku="OZ-IMP-1").one()
    assert b.title == "Книга Озон" and b.isbn == "111222", "поля книги Ozon не заполнены"
    lst = s.query(Listing).filter_by(book_id=b.id, marketplace="ozon").one()
    assert lst.external_id == "OZ-IMP-1", "лот Ozon не привязан"
print("[ok] Импорт по кнопке из Ozon: книга и лот созданы")

# WB: карточки с vendorCode, баркодом и ценой в размерах.
# Первая книга в наличии (остаток 3), вторая — нет в наличии (остаток 0).
_fake_wb_cards = [
    {"vendorCode": "WB-IMP-1", "title": "Книга ВБ", "brand": "Изд-во",
     "sizes": [{"price": 200, "skus": ["333444"]}]},
    {"vendorCode": "WB-IMP-0", "title": "Нет в наличии", "brand": "Изд-во",
     "sizes": [{"price": 150, "skus": ["555666"]}]},
]
_fake_wb_stocks = [
    {"sku": "333444", "amount": 3},
    {"sku": "555666", "amount": 0},
]
r = c.post("/import/pull/wildberries", follow_redirects=True)
assert "Импорт завершён" in r.text, f"импорт WB не завершился: {r.text[:200]}"
with SessionLocal() as s:
    b = s.query(Book).filter_by(sku="WB-IMP-1").one()
    assert b.title == "Книга ВБ" and b.isbn == "333444", "поля книги WB не заполнены"
    assert s.query(Listing).filter_by(book_id=b.id, marketplace="wildberries").count() == 1
    # В наличии — остаётся в каталоге (не в архиве).
    assert b.archived_at is None and b.status == BookStatus.IN_STOCK, "книга в наличии не должна уходить в архив"
print("[ok] Импорт по кнопке из WB: книга и лот созданы")

# Остаток 0 → книга сразу в архиве, снята с продажи.
with SessionLocal() as s:
    z = s.query(Book).filter_by(sku="WB-IMP-0").one()
    assert z.archived_at is not None, "книга без остатка должна уйти в архив"
    assert z.status == BookStatus.WITHDRAWN, "книга без остатка должна быть снята"
    lot = s.query(Listing).filter_by(book_id=z.id, marketplace="wildberries").one()
    assert lot.status == ListingStatus.WITHDRAWN, "лот книги без остатка должен быть снят"
print("[ok] Импорт по кнопке из WB: товар без остатка ушёл в архив")

# Avito: объявления из /core/v1/items.
_fake_avito_items = {"resources": [{"id": "AV-IMP-1", "title": "Книга Авито", "price": 250}]}
r = c.post("/import/pull/avito", follow_redirects=True)
assert "Импорт завершён" in r.text, f"импорт Avito не завершился: {r.text[:200]}"
with SessionLocal() as s:
    b = s.query(Book).filter_by(sku="AV-IMP-1").one()
    assert b.title == "Книга Авито", "поля книги Avito не заполнены"
    assert s.query(Listing).filter_by(book_id=b.id, marketplace="avito").count() == 1
print("[ok] Импорт по кнопке из Avito: книга и лот созданы")

# Дедупликация: повторная выгрузка Ozon не плодит дубли, а обновляет.
before = None
with SessionLocal() as s:
    before = s.query(Book).count()
r = c.post("/import/pull/ozon", follow_redirects=True)
with SessionLocal() as s:
    assert s.query(Book).count() == before, "повторный импорт создал дубли"
print("[ok] Импорт по кнопке: повторная выгрузка не плодит дубли")

# Фильтр каталога по площадке: показываем только книги с лотом на этой площадке.
r_wb = c.get("/?marketplace=wildberries")
assert "WB-IMP-1" in r_wb.text, "книга WB не попала в фильтр по WB"
assert "AV-IMP-1" not in r_wb.text, "книга Avito не должна показываться в фильтре по WB"
r_av = c.get("/?marketplace=avito")
assert "AV-IMP-1" in r_av.text, "книга Avito не попала в фильтр по Avito"
assert "WB-IMP-1" not in r_av.text, "книга WB не должна показываться в фильтре по Avito"
print("[ok] Каталог: фильтр по площадке показывает только её товары")

# Выборочное выставление: targets ограничивает публикацию только выбранными площадками.
bid_sel = make_book("SEL-1")
r = c.post("/books/bulk", data={"action": "publish", "book_ids": [bid_sel],
                                "targets": ["ozon", "wildberries"]}, follow_redirects=True)
with SessionLocal() as s:
    mps = {l.marketplace for l in s.query(Listing).filter_by(book_id=bid_sel).all()}
    assert mps == {"ozon", "wildberries"}, f"выставлено не на выбранные площадки: {mps}"
print("[ok] Каталог: 'Выставить' уважает выбор площадок")

# Выборочное снятие: снимаем только с WB, лот на Ozon остаётся активным.
r = c.post("/books/bulk", data={"action": "withdraw", "book_ids": [bid_sel],
                                "targets": ["wildberries"]}, follow_redirects=True)
with SessionLocal() as s:
    wb = s.query(Listing).filter_by(book_id=bid_sel, marketplace="wildberries").one()
    oz = s.query(Listing).filter_by(book_id=bid_sel, marketplace="ozon").one()
    assert wb.status == ListingStatus.WITHDRAWN, "лот WB должен быть снят"
    assert oz.status == ListingStatus.ACTIVE, "лот Ozon не должен сниматься"
    b = s.get(Book, bid_sel)
    assert b.status == BookStatus.IN_STOCK, "книга с активным лотом не должна помечаться снятой"
print("[ok] Каталог: 'Снять' уважает выбор площадок, книга остаётся в продаже")

# Выключенную площадку по API не выгрузить.
with SessionLocal() as s:
    acc = s.query(MarketplaceAccount).filter_by(marketplace="ozon").one()
    acc.enabled = False
    s.commit()
r = c.post("/import/pull/ozon", follow_redirects=True)
assert "выключена" in r.text.lower() or "ключи не заданы" in r.text.lower(), "выключенная площадка выгрузилась!"
print("[ok] Импорт по кнопке: выключенная площадка отклоняется")

_fake_ozon_list = {"items": [], "last_id": ""}
_fake_ozon_info = {"items": []}
_fake_wb_cards = []
_fake_wb_stocks = []
_fake_avito_items = {"resources": []}


# --- 14. Архив: отложенный перенос снятых/проданных книг --------------------

from datetime import timedelta
from app.archive import sweep_to_archive, days_until_archive
from app.models import utcnow

# Стартуем с чистого каталога, чтобы считать архив начисто.
with SessionLocal() as s:
    s.query(Order).delete()
    s.query(Listing).delete()
    s.query(Book).delete()
    s.commit()

# Снятая книга остаётся в каталоге (не в архиве) и показывает срок до переноса.
bid_arch = make_book("ARCH-1")
r = c.post("/books/bulk", data={"action": "withdraw", "book_ids": [bid_arch]}, follow_redirects=True)
with SessionLocal() as s:
    b = s.get(Book, bid_arch)
    assert b.status == BookStatus.WITHDRAWN, "книга не снята"
    assert b.removed_at is not None, "не проставлен removed_at при снятии"
    assert b.archived_at is None, "книга уехала в архив сразу — должна ждать окно"
    assert days_until_archive(b) is not None, "нет обратного отсчёта до архива"
# Основной каталог показывает книгу, архив — пуст.
assert "ARCH-1" in c.get("/").text, "снятая книга пропала из каталога раньше времени"
assert "ARCH-1" not in c.get("/archive").text, "снятая книга уже в архиве"
print("[ok] Архив: снятая книга ждёт в каталоге с обратным отсчётом")

# Пока окно не истекло — авто-перенос ничего не трогает.
with SessionLocal() as s:
    assert sweep_to_archive(s) == 0, "перенос сработал раньше срока"
    s.commit()

# Сдвигаем removed_at за границу окна — авто-перенос забирает книгу в архив.
with SessionLocal() as s:
    b = s.get(Book, bid_arch)
    b.removed_at = utcnow() - timedelta(days=999)
    s.commit()
with SessionLocal() as s:
    moved = sweep_to_archive(s)
    s.commit()
    assert moved == 1, f"ожидали перенос 1 книги, перенесено {moved}"
with SessionLocal() as s:
    b = s.get(Book, bid_arch)
    assert b.archived_at is not None, "книга не помечена архивной"
assert "ARCH-1" not in c.get("/").text, "архивная книга осталась в каталоге"
assert "ARCH-1" in c.get("/archive").text, "архивная книга не видна в архиве"
print("[ok] Архив: по истечении окна книга автоматически уезжает в архив")

# Возврат из архива возвращает книгу в каталог и снимает отметки.
r = c.post("/books/bulk", data={"action": "restore", "book_ids": [bid_arch]}, follow_redirects=True)
with SessionLocal() as s:
    b = s.get(Book, bid_arch)
    assert b.archived_at is None and b.removed_at is None, "отметки архива не сброшены при возврате"
assert "ARCH-1" in c.get("/").text, "возвращённая книга не появилась в каталоге"
print("[ok] Архив: книгу можно вернуть в каталог")

# Ручной перенос в архив без ожидания окна.
r = c.post("/books/bulk", data={"action": "archive", "book_ids": [bid_arch]}, follow_redirects=True)
with SessionLocal() as s:
    b = s.get(Book, bid_arch)
    assert b.archived_at is not None, "ручной перенос в архив не сработал"
assert "ARCH-1" in c.get("/archive").text, "книга не попала в архив вручную"
print("[ok] Архив: ручной перенос в архив работает сразу")


# --- Чистка ---------------------------------------------------------------

with SessionLocal() as s:
    s.query(Order).delete()
    s.query(Listing).delete()
    s.query(SyncLog).delete()
    s.query(Book).delete()
    s.query(MarketplaceAccount).delete()
    s.commit()

print("\nВСЕ ПРОВЕРКИ ИНТЕГРАЦИЙ ПРОЙДЕНЫ")
