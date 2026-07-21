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


def set_auto_withdraw(on: bool):
    """Переключить глобальный рубильник автоснятия прямо в базе (для тестов)."""
    from app.flags import set_auto_withdraw as _set
    with SessionLocal() as s:
        _set(s, on)
        s.commit()


# Большинство проверок рассчитывают на кросс-снятие — включаем боевой режим.
set_auto_withdraw(True)


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
_fake_ozon_stocks = {"items": []}                # /v4/product/info/stocks
_fake_wb_cards = []                              # карточки WB
_fake_wb_stocks = []                            # остатки FBS WB: [{"sku","amount"}]

# ozon.httpx и wb.httpx — один и тот же модуль. Подменяем post/request общими
# диспетчерами, маршрутизирующими по URL.


def fake_post(url, json=None, data=None, headers=None, timeout=None):
    if url.endswith("/v3/product/list"):
        return FakeResponse(200, {"result": _fake_ozon_list})
    if url.endswith("/v3/product/info/list"):
        return FakeResponse(200, {"result": _fake_ozon_info})
    if url.endswith("/v4/product/info/stocks"):
        return FakeResponse(200, {"result": _fake_ozon_stocks})
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


# --- 9b. Слежение за остатками: заводим лоты с ключами через сверку ---------

from app.catalog_sync import watch_stocks

# Полная сверка проставляет stock_key лотам (Ozon: offer_id, WB: баркод).
with SessionLocal() as s:
    s.query(Order).delete()
    s.query(Listing).delete()
    s.query(Book).delete()
    s.commit()

_fake_ozon_list = {"items": [{"offer_id": "W-1"}, {"offer_id": "W-2"}], "last_id": ""}
_fake_ozon_info = {"items": [
    {"offer_id": "W-1", "name": "Следим 1", "price": "100", "barcode": "b1"},
    {"offer_id": "W-2", "name": "Следим 2", "price": "100", "barcode": "b2"},
]}
_fake_wb_cards = [{"vendorCode": "W-1", "title": "Следим 1", "brand": "И",
                   "sizes": [{"price": 100, "skus": ["wb-b1"]}]}]
_fake_wb_stocks = [{"sku": "wb-b1", "amount": 5}]
with SessionLocal() as s:
    sync_marketplace(s, "ozon")
    sync_marketplace(s, "wildberries")
    s.commit()
    # W-1 стоит на обеих площадках, W-2 — только на Ozon.
    oz1 = s.query(Listing).filter_by(marketplace="ozon", external_id="W-1").one()
    assert oz1.stock_key == "W-1", f"Ozon stock_key не проставлен: {oz1.stock_key}"
    wb1 = s.query(Listing).filter_by(marketplace="wildberries", external_id="W-1").one()
    assert wb1.stock_key == "wb-b1", f"WB stock_key (баркод) не проставлен: {wb1.stock_key}"
print("[ok] сверка проставляет ключи остатка (Ozon offer_id, WB баркод)")

# Слежение Ozon: у W-1 остаток стал 0 → снять её с обеих площадок; W-2 жива.
_fake_ozon_stocks = {"items": [
    {"offer_id": "W-1", "stocks": [{"present": 0}]},
    {"offer_id": "W-2", "stocks": [{"present": 3}]},
]}
with SessionLocal() as s:
    res = watch_stocks(s, "ozon")
    s.commit()
    assert res["removed"] == 1, f"ожидали снятие 1 по остатку, {res}"
with SessionLocal() as s:
    b1 = s.query(Book).filter_by(sku="W-1").one()
    assert b1.status == BookStatus.WITHDRAWN, "W-1 не снята по нулевому остатку"
    # Кросс-снятие: лот на WB тоже снят.
    wb1 = s.query(Listing).filter_by(marketplace="wildberries", external_id="W-1").one()
    assert wb1.status == ListingStatus.WITHDRAWN, "W-1 не снята с WB (кросс-снятие)"
    b2 = s.query(Book).filter_by(sku="W-2").one()
    assert b2.status == BookStatus.IN_STOCK, "W-2 ошибочно снята"
print("[ok] слежение за остатками: остаток 0 -> снятие с обеих площадок, живая цела")

# Слежение Ozon: W-2 пропала из ответа остатков (карточку удалили) → снять.
_fake_ozon_stocks = {"items": []}
with SessionLocal() as s:
    # Пустой ответ трактуется как сбой (защита) — ничего не снимаем.
    res = watch_stocks(s, "ozon")
    s.commit()
    assert res["removed"] == 0, "пустой ответ остатков не должен ничего снимать"
with SessionLocal() as s:
    assert s.query(Book).filter_by(sku="W-2").one().status == BookStatus.IN_STOCK
print("[ok] слежение: пустой ответ остатков не снимает книги (защита от сбоя API)")

# W-2 действительно пропала, но другие ключи вернулись → снимаем W-2.
_fake_ozon_stocks = {"items": [{"offer_id": "W-1", "stocks": [{"present": 0}]}]}
with SessionLocal() as s:
    res = watch_stocks(s, "ozon")
    s.commit()
    # W-1 уже снята ранее (не активна), W-2 активна и пропала из ответа → снять.
    assert res["removed"] == 1, f"ожидали снятие пропавшей W-2, {res}"
with SessionLocal() as s:
    assert s.query(Book).filter_by(sku="W-2").one().status == BookStatus.WITHDRAWN
print("[ok] слежение: пропавший из ответа ключ (карточки нет) -> снятие")

# Книга только на Ozon не страдает от слежения WB (у неё нет WB-лота с ключом).
with SessionLocal() as s:
    # Вернём W-2 в наличие для этой проверки.
    b2 = s.query(Book).filter_by(sku="W-2").one()
    lot = s.query(Listing).filter_by(marketplace="ozon", external_id="W-2").one()
    lot.status = ListingStatus.ACTIVE
    b2.status = BookStatus.IN_STOCK
    s.commit()
_fake_wb_stocks = [{"sku": "wb-b1", "amount": 5}]  # WB знает только про W-1
with SessionLocal() as s:
    watch_stocks(s, "wildberries")
    s.commit()
with SessionLocal() as s:
    b2 = s.query(Book).filter_by(sku="W-2").one()
    assert b2.status == BookStatus.IN_STOCK, "книга только на Ozon снята слежением WB!"
print("[ok] слежение WB не трогает книгу, которой нет на WB (только на Ozon)")

# Чистим под следующий блок.
with SessionLocal() as s:
    s.query(Order).delete()
    s.query(Listing).delete()
    s.query(Book).delete()
    s.commit()
_fake_ozon_stocks = {"items": []}
_fake_wb_cards = []
_fake_wb_stocks = []


# --- 9c. Восстанавливаем данные для проверки кнопки сверки ------------------

with SessionLocal() as s:
    b = Book(sku="WB-ONLY-1", title="Только на WB", status=BookStatus.IN_STOCK, price=300)
    s.add(b)
    s.flush()
    s.add(Listing(book_id=b.id, marketplace="wildberries", external_id="WB-ONLY-1",
                  stock_key="wbonly", status=ListingStatus.ACTIVE))
    s.add(Book(sku="OZ-1", title="Книга 1", status=BookStatus.IN_STOCK, price=150))
    s.flush()
    oz1 = s.query(Book).filter_by(sku="OZ-1").one()
    s.add(Listing(book_id=oz1.id, marketplace="ozon", external_id="OZ-1",
                  stock_key="OZ-1", status=ListingStatus.ACTIVE))
    s.commit()
    wb_only_id = s.query(Book).filter_by(sku="WB-ONLY-1").one().id


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
assert 'action="/books/save"' not in r.text, "в карточке осталась форма сохранения"
assert 'action="/books/bulk"' not in r.text, "в карточке остались массовые действия"
print("[ok] карточка книги — только просмотр, без формы редактирования")


# --- 13. Каталог — чистый мониторинг: массовых действий и архива нет --------

r = c.get("/")
assert 'action="/books/bulk"' not in r.text, "в каталоге осталась форма массовых действий"
assert "rowcheck" not in r.text, "в каталоге остались чекбоксы строк"
assert 'href="/archive"' not in r.text, "в меню осталась ссылка на архив"
# Роут массовых действий удалён.
assert c.post("/books/bulk", data={"action": "withdraw", "book_ids": [some_id]},
              follow_redirects=False).status_code in (404, 405), "роут /books/bulk ещё жив"
# Роут архива удалён.
assert c.get("/archive", follow_redirects=False).status_code in (404, 405, 303), "роут /archive ещё жив"
print("[ok] каталог — чистый мониторинг: нет массовых действий и архива")


# --- 14. Очистка каталога не падает при ссылках SyncLog->books --------------
# Это была причина краша: SyncLog.book_id (FK) не чистился, и DELETE books падал.

WIPE_PW = os.environ.get("WIPE_PASSWORD", "2601")
with SessionLocal() as s:
    b = Book(sku="WIPE-1", title="К удалению", status=BookStatus.IN_STOCK, price=10)
    s.add(b)
    s.flush()
    s.add(Listing(book_id=b.id, marketplace="ozon", external_id="WIPE-1", status=ListingStatus.ACTIVE))
    # Запись журнала со ссылкой на книгу — ровно то, что рушило очистку.
    s.add(SyncLog(marketplace="ozon", book_id=b.id, action="test", ok=True, message="ref"))
    s.add(Order(marketplace="ozon", external_order_id="WIPE-ORD", book_id=b.id, processed=True))
    s.commit()

# Неверный пароль — ничего не удаляется.
r = c.post("/catalog/wipe", data={"password": "0000"}, follow_redirects=True)
with SessionLocal() as s:
    assert s.query(Book).count() > 0, "книги удалены при неверном пароле!"

# Верный пароль — всё чистится без 500 (даже при ссылках из SyncLog/Order).
r = c.post("/catalog/wipe", data={"password": WIPE_PW}, follow_redirects=True)
assert r.status_code == 200, f"очистка вернула {r.status_code} (краш?)"
assert "полностью очищен" in r.text, "нет подтверждения очистки"
with SessionLocal() as s:
    assert s.query(Book).count() == 0, "книги остались после очистки"
    assert s.query(SyncLog).count() == 0, "журнал не очищен"
    assert s.query(Order).count() == 0, "заказы не очищены"
    assert s.query(Listing).count() == 0, "лоты не очищены"
print("[ok] очистка каталога не падает при ссылках SyncLog/Order и чистит всё")


# --- 15. Живой поиск: JSON-API и метка WB (не WI) --------------------------

with SessionLocal() as s:
    b = Book(sku="SRCH-1", title="Уникальное Название Книги", status=BookStatus.IN_STOCK, price=99)
    s.add(b)
    s.flush()
    s.add(Listing(book_id=b.id, marketplace="wildberries", external_id="SRCH-1",
                  stock_key="SRCH-1", status=ListingStatus.ACTIVE))
    s.commit()

r = c.get("/api/books?q=Уникальное")
assert r.status_code == 200, f"API поиска вернул {r.status_code}"
data = r.json()
assert data["total"] == 1, f"поиск нашёл {data['total']} вместо 1"
item = data["items"][0]
assert item["sku"] == "SRCH-1", "не та книга в выдаче поиска"
# Метка площадки WB, а не WI (это была ошибка wildberries[:2]).
assert item["listings"][0]["short"] == "WB", f"метка площадки {item['listings'][0]['short']} вместо WB"
# Пустой запрос по мусору — ничего.
assert c.get("/api/books?q=неттакойкниги000").json()["total"] == 0, "поиск нашёл несуществующее"
print("[ok] живой поиск: JSON-API фильтрует, метка площадки — WB")


# --- 16. Рубильник автоснятия: выкл — не снимаем, вкл — снимаем -------------

# Книга на двух площадках, продажа приходит с Ozon.
def _make_two_mp(sku):
    with SessionLocal() as s:
        b = Book(sku=sku, title="Рубильник", status=BookStatus.IN_STOCK, price=100)
        s.add(b); s.flush()
        s.add(Listing(book_id=b.id, marketplace="ozon", external_id=sku, stock_key=sku, status=ListingStatus.ACTIVE))
        s.add(Listing(book_id=b.id, marketplace="wildberries", external_id=sku, stock_key=sku, status=ListingStatus.ACTIVE))
        s.commit()
        return b.id

from app.sync import poll_marketplace_orders

# ВЫКЛ: продажа на Ozon НЕ снимает книгу с WB (только мониторинг).
set_auto_withdraw(False)
off_id = _make_two_mp("SWITCH-OFF")
_fake_orders["postings"] = [{"posting_number": "OFF-ORDER", "products": [{"offer_id": "SWITCH-OFF"}]}]
with SessionLocal() as s:
    poll_marketplace_orders(s, "ozon"); s.commit()
with SessionLocal() as s:
    wb_l = s.query(Listing).filter_by(book_id=off_id, marketplace="wildberries").one()
    assert wb_l.status == ListingStatus.ACTIVE, "при ВЫКЛ рубильнике WB не должна сниматься"
print("[ok] рубильник ВЫКЛ: продажа на Ozon не снимает книгу с WB")

# ВКЛ: та же ситуация снимает книгу с WB.
set_auto_withdraw(True)
on_id = _make_two_mp("SWITCH-ON")
_fake_orders["postings"] = [{"posting_number": "ON-ORDER", "products": [{"offer_id": "SWITCH-ON"}]}]
with SessionLocal() as s:
    poll_marketplace_orders(s, "ozon"); s.commit()
with SessionLocal() as s:
    wb_l = s.query(Listing).filter_by(book_id=on_id, marketplace="wildberries").one()
    assert wb_l.status == ListingStatus.WITHDRAWN, "при ВКЛ рубильнике WB должна сняться"
_fake_orders["postings"] = []
print("[ok] рубильник ВКЛ: продажа на Ozon снимает книгу с WB")

# UI: тумблер в настройках переключается и виден в состоянии.
unlock("/settings")
r = c.post("/settings/auto-withdraw", data={"enabled": "off"}, follow_redirects=True)
assert "Автоснятие выключено" in r.text, "тумблер не выключился через UI"
unlock("/settings")
r = c.get("/settings")
assert "Включить снятие" in r.text, "нет кнопки включения при выключенном автоснятии"
set_auto_withdraw(True)  # возвращаем для остальных возможных прогонов
print("[ok] тумблер автоснятия работает через UI")


# --- Чистка ---------------------------------------------------------------

with SessionLocal() as s:
    s.query(Order).delete()
    s.query(Listing).delete()
    s.query(SyncLog).delete()
    s.query(Book).delete()
    s.query(MarketplaceAccount).delete()
    s.commit()

print("\nВСЕ ПРОВЕРКИ ИНТЕГРАЦИЙ ПРОЙДЕНЫ")
