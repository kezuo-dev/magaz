"""Проверка интеграций площадок на заглушках (без реальных вызовов к Ozon/WB).

Мокаем httpx внутри клиентов, чтобы прогнать весь путь новой логики:
сохранение ключей → опрос заказов и кросс-снятие → полная сверка каталога с
обнаружением пропавших книг → архив. Выставление книг убрано — программа только
отслеживает каталог и снимает проданное.
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
import app.marketplaces.wildberries as wb


ADMIN_PW = os.environ.get("ADMIN_PASSWORD", "1908")
APP_PW = os.environ.get("APP_PASSWORD", "050620")

c = TestClient(app)
c.post("/login", data={"password": APP_PW})


def unlock(section="/settings"):
    """Разблокировать защищённый раздел прямо перед обращением к нему (разово)."""
    c.post("/admin-login", data={"password": ADMIN_PW, "next": section}, follow_redirects=False)


# --- Заглушки API площадок -------------------------------------------------

class FakeResponse:
    def __init__(self, status_code=200, payload=None, text=""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text or ""
        self.content = b"{}" if payload is not None else b""

    def json(self):
        return self._payload


# Что «отдают» площадки. Настраивается в тестах.
_fake_orders = {"postings": []}                  # заказы Ozon
_fake_wb_orders = {"orders": []}                 # заказы WB
_fake_ozon_list = {"items": [], "last_id": ""}   # /v3/product/list
_fake_ozon_info = {"items": []}                  # /v3/product/info/list
_fake_wb_cards = []                              # карточки WB
_fake_wb_stocks = []                            # остатки FBS WB: [{"sku","amount"}]

# ozon.httpx и wb.httpx — один и тот же модуль. Подменяем post/request общими
# диспетчерами, маршрутизирующими по URL.


def fake_post(url, json=None, data=None, headers=None, timeout=None):
    if url.endswith("/v3/product/list"):
        return FakeResponse(200, {"result": _fake_ozon_list})
    if url.endswith("/v3/product/info/list"):
        return FakeResponse(200, {"result": _fake_ozon_info})
    if url.endswith("/v2/products/stocks"):
        return FakeResponse(200, {"result": [{"updated": True}]})
    if url.endswith("/v3/posting/fbs/list"):
        return FakeResponse(200, {"result": _fake_orders})
    raise AssertionError(f"неожиданный POST: {url}")


def fake_request(method, url, json=None, data=None, params=None, headers=None, timeout=None):
    if url.endswith("/content/v2/get/cards/list"):
        return FakeResponse(200, {"cards": _fake_wb_cards, "cursor": {"total": len(_fake_wb_cards)}})
    if "/api/v3/stocks/" in url:
        if method.upper() == "POST":
            wanted = set((json or {}).get("skus") or [])
            stocks = [s for s in _fake_wb_stocks if s.get("sku") in wanted]
            return FakeResponse(200, {"stocks": stocks})
        return FakeResponse(200, {})  # PUT — запись остатка (снятие)
    if url.endswith("/api/v3/orders/new"):
        return FakeResponse(200, _fake_wb_orders)
    raise AssertionError(f"неожиданный запрос: {method} {url}")


ozon.httpx.post = fake_post
ozon.httpx.request = fake_request


# --- Хелперы ---------------------------------------------------------------

def make_book(sku, title="Тест", marketplaces=("ozon",)):
    """Книга с активными лотами на указанных площадках."""
    with SessionLocal() as s:
        b = Book(sku=sku, title=title, status=BookStatus.IN_STOCK, price=100)
        s.add(b)
        s.flush()
        for mp in marketplaces:
            s.add(Listing(book_id=b.id, marketplace=mp, external_id=sku, status=ListingStatus.ACTIVE))
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


def set_enabled(marketplace, value):
    with SessionLocal() as s:
        acc = s.query(MarketplaceAccount).filter_by(marketplace=marketplace).one_or_none()
        if acc:
            acc.enabled = value
            s.commit()


# --- 1. Доступ к разделам под паролем --------------------------------------

unlock("/settings")
r = c.get("/settings")
assert r.status_code == 200 and "Настройки площадок" in r.text, "нет страницы настроек"
r = c.get("/")
assert 'href="/settings"' in r.text and 'href="/log"' in r.text, "нет ссылок в меню"
# Меню больше не содержит выставления/добавления книг.
assert 'href="/books/new"' not in r.text, "осталась кнопка «Добавить книгу»"
print("[ok] разделы в меню; кнопки добавления книги нет")

c2 = TestClient(app)
c2.post("/login", data={"password": APP_PW})
r = c2.get("/settings", follow_redirects=False)
assert r.status_code == 303 and "/admin-login" in r.headers["location"], "Настройки открылись без пароля!"
r = c2.get("/log", follow_redirects=False)
assert r.status_code == 303 and "/admin-login" in r.headers["location"], "Журнал открылся без пароля!"
print("[ok] Журнал и Настройки закрыты вторым паролем")


# --- 2. Только две площадки: Ozon и WB, Avito нет --------------------------

unlock("/settings")
r = c.get("/settings")
assert "Ozon" in r.text and "Wildberries" in r.text, "нет карточек Ozon/WB"
assert "avito" not in r.text.lower() and "авито" not in r.text.lower(), "Avito всё ещё в настройках"
print("[ok] в настройках только Ozon и Wildberries")


# --- 3. Ключи Ozon/WB: шифрование, проверка связи --------------------------

enable_ozon()
with SessionLocal() as s:
    acc = s.query(MarketplaceAccount).filter_by(marketplace="ozon").one()
    assert acc.enabled and acc.credentials_encrypted and "secret" not in acc.credentials_encrypted
from app.security import decrypt_credentials
assert decrypt_credentials(acc.credentials_encrypted)["api_key"] == "secret"
unlock("/settings")
r = c.post("/settings/check", data={"marketplace": "ozon"}, follow_redirects=True)
assert "подключение успешно" in r.text.lower(), "проверка Ozon не прошла"

enable_wb()
with SessionLocal() as s:
    acc = s.query(MarketplaceAccount).filter_by(marketplace="wildberries").one()
    assert acc.enabled and "wb-secret" not in (acc.credentials_encrypted or "")
unlock("/settings")
r = c.post("/settings/check", data={"marketplace": "wildberries"}, follow_redirects=True)
assert "подключение успешно" in r.text.lower(), "проверка WB не прошла"
print("[ok] ключи Ozon/WB шифруются, проверка связи успешна")


# --- 4. Опрос заказов Ozon: продажа → книга sold + кросс-снятие с WB --------

bid = make_book("SOLD-1", marketplaces=("ozon", "wildberries"))
_fake_orders["postings"] = [{"posting_number": "ORDER-777", "products": [{"offer_id": "SOLD-1"}]}]

from app.sync import poll_marketplace_orders
with SessionLocal() as s:
    assert poll_marketplace_orders(s, "ozon") == 1, "ожидали 1 новый заказ"
    s.commit()
with SessionLocal() as s:
    b = s.get(Book, bid)
    assert b.status == BookStatus.SOLD, "книга не помечена проданной"
    wb_l = s.query(Listing).filter_by(book_id=bid, marketplace="wildberries").one()
    assert wb_l.status == ListingStatus.WITHDRAWN, "лот на WB не снят (кросс-снятие)"
    order = s.query(Order).filter_by(external_order_id="ORDER-777").one()
    assert order.processed and order.book_id == bid
print("[ok] продажа на Ozon снимает книгу с WB, заказ обработан")

# Повторный опрос того же заказа не создаёт дубль.
_fake_orders["postings"] = [{"posting_number": "ORDER-777", "products": [{"offer_id": "SOLD-1"}]}]
with SessionLocal() as s:
    assert poll_marketplace_orders(s, "ozon") == 0, "заказ задублировался"
    s.commit()
_fake_orders["postings"] = []
print("[ok] повторный опрос не дублирует заказ")


# --- 5. Опрос заказов WB: продажа → снятие с Ozon --------------------------

bid_wb = make_book("WB-SOLD-1", marketplaces=("ozon", "wildberries"))
_fake_wb_orders["orders"] = [{"id": "WB-ORDER-1", "article": "WB-SOLD-1"}]
with SessionLocal() as s:
    assert poll_marketplace_orders(s, "wildberries") == 1
    s.commit()
with SessionLocal() as s:
    b = s.get(Book, bid_wb)
    assert b.status == BookStatus.SOLD, "WB: книга не продана"
    oz = s.query(Listing).filter_by(book_id=bid_wb, marketplace="ozon").one()
    assert oz.status == ListingStatus.WITHDRAWN, "WB: лот Ozon не снят"
_fake_wb_orders["orders"] = []
print("[ok] продажа на WB снимает книгу с Ozon")


# --- 6. Офлайн-режим: выключенная площадка не зовёт API --------------------

set_enabled("ozon", False)
with SessionLocal() as s:
    assert poll_marketplace_orders(s, "ozon") == 0, "офлайн Ozon не должен звать API"
set_enabled("ozon", True)
print("[ok] офлайн-режим не зовёт API")


# --- 7. Полная сверка каталога по API + обнаружение пропавших --------------

# Чистим каталог, чтобы считать начисто.
with SessionLocal() as s:
    s.query(Order).delete()
    s.query(Listing).delete()
    s.query(Book).delete()
    s.commit()

# Первая сверка Ozon: две книги в наличии.
_fake_ozon_list = {"items": [{"offer_id": "OZ-1"}, {"offer_id": "OZ-2"}], "last_id": ""}
_fake_ozon_info = {"items": [
    {"offer_id": "OZ-1", "name": "Книга 1", "price": "150", "barcode": "111"},
    {"offer_id": "OZ-2", "name": "Книга 2", "price": "200", "barcode": "222"},
]}
from app.catalog_sync import sync_marketplace
with SessionLocal() as s:
    res = sync_marketplace(s, "ozon")
    s.commit()
    assert res["created"] == 2, f"ожидали 2 новых, {res}"
with SessionLocal() as s:
    assert s.query(Book).filter_by(sku="OZ-1").one().title == "Книга 1"
print("[ok] сверка Ozon: новые книги созданы")

# Вторая сверка: OZ-2 пропала из каталога Ozon (продана/снята) → должна сняться.
_fake_ozon_list = {"items": [{"offer_id": "OZ-1"}], "last_id": ""}
_fake_ozon_info = {"items": [
    {"offer_id": "OZ-1", "name": "Книга 1", "price": "150", "barcode": "111"},
]}
with SessionLocal() as s:
    res = sync_marketplace(s, "ozon")
    s.commit()
    assert res["removed"] == 1, f"ожидали снятие 1 пропавшей, {res}"
with SessionLocal() as s:
    gone = s.query(Book).filter_by(sku="OZ-2").one()
    assert gone.status == BookStatus.WITHDRAWN, "пропавшая книга не снята"
    lot = s.query(Listing).filter_by(book_id=gone.id, marketplace="ozon").one()
    assert lot.status == ListingStatus.WITHDRAWN, "лот пропавшей книги не снят"
    assert gone.removed_at is not None, "не запущено окно до архива"
    alive = s.query(Book).filter_by(sku="OZ-1").one()
    assert alive.status == BookStatus.IN_STOCK, "живая книга ошибочно снята"
print("[ok] сверка Ozon: пропавшая книга снята, живая не тронута")


# --- 8. Защита от ложного снятия: пустой ответ каталога не трогает книги ----

_fake_ozon_list = {"items": [], "last_id": ""}
_fake_ozon_info = {"items": []}
with SessionLocal() as s:
    res = sync_marketplace(s, "ozon")
    s.commit()
    assert res["removed"] == 0, "пустой каталог не должен ничего снимать"
with SessionLocal() as s:
    assert s.query(Book).filter_by(sku="OZ-1").one().status == BookStatus.IN_STOCK
print("[ok] пустой ответ каталога не снимает книги (защита от сбоя API)")


# --- 9. Книга только на WB не страдает от сверки Ozon ----------------------

with SessionLocal() as s:
    b = Book(sku="WB-ONLY-1", title="Только на WB", status=BookStatus.IN_STOCK, price=300)
    s.add(b)
    s.flush()
    s.add(Listing(book_id=b.id, marketplace="wildberries", external_id="WB-ONLY-1", status=ListingStatus.ACTIVE))
    s.commit()
    wb_only_id = b.id

# Сверяем Ozon (WB-ONLY-1 в его каталоге нет) — книга не должна пострадать.
_fake_ozon_list = {"items": [{"offer_id": "OZ-1"}], "last_id": ""}
_fake_ozon_info = {"items": [{"offer_id": "OZ-1", "name": "Книга 1", "price": "150", "barcode": "111"}]}
with SessionLocal() as s:
    sync_marketplace(s, "ozon")
    s.commit()
with SessionLocal() as s:
    b = s.get(Book, wb_only_id)
    assert b.status == BookStatus.IN_STOCK, "книга только на WB снята сверкой Ozon!"
    wb_l = s.query(Listing).filter_by(book_id=wb_only_id, marketplace="wildberries").one()
    assert wb_l.status == ListingStatus.ACTIVE, "лот WB тронут сверкой Ozon!"
print("[ok] книга только на WB не затронута сверкой Ozon")


# --- 10. Кнопка «Обновить каталог» (сверка всех площадок) ------------------

# WB отдаёт одну карточку в наличии; книга WB-ONLY-1 (её в выдаче WB нет) — пропала.
_fake_ozon_list = {"items": [{"offer_id": "OZ-1"}], "last_id": ""}
_fake_ozon_info = {"items": [{"offer_id": "OZ-1", "name": "Книга 1", "price": "150", "barcode": "111"}]}
_fake_wb_cards = [{"vendorCode": "WB-NEW-1", "title": "Новая ВБ", "brand": "Изд",
                   "sizes": [{"price": 200, "skus": ["999"]}]}]
_fake_wb_stocks = [{"sku": "999", "amount": 2}]
unlock("/settings")
r = c.post("/import/sync", follow_redirects=True)
assert r.status_code == 200, f"сверка всех площадок: {r.status_code}"
with SessionLocal() as s:
    assert s.query(Book).filter_by(sku="WB-NEW-1").one().status == BookStatus.IN_STOCK
    # WB-ONLY-1 пропала из выдачи WB → снята.
    gone = s.get(Book, wb_only_id)
    assert gone.status == BookStatus.WITHDRAWN, "пропавшая с WB книга не снята кнопкой сверки"
_fake_wb_cards = []
_fake_wb_stocks = []
print("[ok] кнопка «Обновить каталог» сверяет все площадки и снимает пропавшее")


# --- 11. Импорт файлом (CSV) не снимает отсутствующие ----------------------

import io
csv_ozon = (
    "Артикул продавца;Название товара;Штрихкод;Цена продажи;Автор\r\n"
    "BK-100;Мастер и Маргарита;9785000000001;350;Булгаков\r\n"
)
r = c.post(
    "/import/upload",
    files={"file": ("ozon_export.csv", io.BytesIO(csv_ozon.encode("utf-8")), "text/csv")},
    data={"marketplace": ""},
    follow_redirects=True,
)
assert r.status_code == 200 and "Импорт завершён" in r.text, "импорт файлом не прошёл"
with SessionLocal() as s:
    b = s.query(Book).filter_by(sku="BK-100").one()
    assert b.title == "Мастер и Маргарита" and b.author == "Булгаков"
    # Другие книги (OZ-1 и т.д.) остались на месте — файл не снимает отсутствующих.
    assert s.query(Book).filter_by(sku="OZ-1").one().status == BookStatus.IN_STOCK
print("[ok] импорт файлом добавляет книги и не снимает отсутствующие")


# --- 12. Карточка книги открывается только на просмотр ---------------------

with SessionLocal() as s:
    some_id = s.query(Book).filter_by(sku="OZ-1").one().id
r = c.get(f"/books/{some_id}")
assert r.status_code == 200 and "только просмотр" in r.text.lower(), "карточка не в режиме просмотра"
assert "<form" not in r.text.split("Площадки")[0] or "books/save" not in r.text, "в карточке осталась форма сохранения"
print("[ok] карточка книги — только просмотр, без формы редактирования")


# --- 13. Ручное снятие и архив --------------------------------------------

from datetime import timedelta
from app.archive import sweep_to_archive, days_until_archive
from app.models import utcnow

bid_arch = make_book("ARCH-1", marketplaces=("ozon",))
c.post("/books/bulk", data={"action": "withdraw", "book_ids": [bid_arch]}, follow_redirects=True)
with SessionLocal() as s:
    b = s.get(Book, bid_arch)
    assert b.status == BookStatus.WITHDRAWN and b.removed_at is not None and b.archived_at is None
    assert days_until_archive(b) is not None
assert "ARCH-1" in c.get("/").text and "ARCH-1" not in c.get("/archive").text
print("[ok] снятая книга ждёт в каталоге с обратным отсчётом")

with SessionLocal() as s:
    b = s.get(Book, bid_arch)
    b.removed_at = utcnow() - timedelta(days=999)
    s.commit()
with SessionLocal() as s:
    assert sweep_to_archive(s) == 1
    s.commit()
assert "ARCH-1" not in c.get("/").text and "ARCH-1" in c.get("/archive").text
print("[ok] по истечении окна книга уезжает в архив")

c.post("/books/bulk", data={"action": "restore", "book_ids": [bid_arch]}, follow_redirects=True)
with SessionLocal() as s:
    b = s.get(Book, bid_arch)
    assert b.archived_at is None and b.removed_at is None
assert "ARCH-1" in c.get("/").text
print("[ok] книгу можно вернуть из архива")


# --- Чистка ---------------------------------------------------------------

with SessionLocal() as s:
    s.query(Order).delete()
    s.query(Listing).delete()
    s.query(SyncLog).delete()
    s.query(Book).delete()
    s.query(MarketplaceAccount).delete()
    s.commit()

print("\nВСЕ ПРОВЕРКИ ИНТЕГРАЦИЙ ПРОЙДЕНЫ")
