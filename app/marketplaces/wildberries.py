"""Клиент Wildberries Seller API.

Документация: https://dev.wildberries.ru/en/openapi/api-information
Аутентификация — один заголовок Authorization с API-токеном (создаётся в ЛК
продавца: Профиль → Настройки → Доступ к API). Токен храним шифрованно.

WB разнёс методы по нескольким доменам:
- content-api  — карточки товаров (создание/поиск);
- discounts-prices-api — цены;
- marketplace-api — остатки на складах FBS и заказы (сборочные задания).

Книги б/у в единственном экземпляре: остаток всегда 1 (или 0 при снятии).
SKU книги используем как vendorCode — это наш артикул на стороне WB.
"""
from __future__ import annotations

import httpx

from app.marketplaces.base import (
    MarketplaceClient,
    MarketplaceError,
    OrderInfo,
    PublishResult,
)
from app.photos import public_photo_list

CONTENT_URL = "https://content-api.wildberries.ru"
PRICES_URL = "https://discounts-prices-api.wildberries.ru"
MARKETPLACE_URL = "https://marketplace-api.wildberries.ru"
TIMEOUT = 30.0


class WBClient(MarketplaceClient):
    marketplace = "wildberries"

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self.token = str(credentials.get("api_token", "")).strip()
        if not self.token:
            raise MarketplaceError("Не задан API-токен для Wildberries")
        # Склад FBS, куда пишем остатки. Необязателен: если не задан, остатки
        # не трогаем (карточка всё равно создаётся).
        self.warehouse_id = str(credentials.get("warehouse_id", "")).strip()
        # ID предмета «Книги». WB требует его при создании карточки (subjectID > 0).
        # Для магазина книг это одно значение на все товары — задаётся в настройках.
        self.subject_id = str(credentials.get("subject_id", "")).strip()

    # --- инфраструктура ---------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": self.token,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, payload: dict | None = None, params: dict | None = None) -> dict:
        """Запрос к WB с единой обработкой ошибок. Возвращает распарсенный JSON."""
        try:
            resp = httpx.request(
                method, url, json=payload, params=params, headers=self._headers(), timeout=TIMEOUT
            )
        except httpx.HTTPError as exc:
            raise MarketplaceError(f"Сеть Wildberries недоступна: {exc}") from exc

        if resp.status_code in (401, 403):
            raise MarketplaceError(
                "Wildberries отклонил токен (401/403). Проверьте API-токен и его права"
            )
        if resp.status_code == 429:
            raise MarketplaceError("Wildberries: превышен лимит запросов (429), повторите позже")
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                # WB кладёт причину в errorText либо в errors.
                detail = body.get("errorText") or body.get("error") or str(body.get("errors") or "")
            except Exception:
                detail = resp.text
            raise MarketplaceError(f"Wildberries вернул {resp.status_code}: {detail or resp.text[:200]}")

        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception as exc:
            raise MarketplaceError(f"Wildberries вернул не-JSON: {resp.text[:200]}") from exc

    def _post(self, url: str, payload: dict) -> dict:
        return self._request("POST", url, payload)

    # --- операции ---------------------------------------------------------

    def check_connection(self) -> None:
        """Лёгкий запрос списка карточек — проверяем, что токен рабочий."""
        self._post(
            f"{CONTENT_URL}/content/v2/get/cards/list",
            {"settings": {"cursor": {"limit": 1}, "filter": {"withPhoto": -1}}},
        )

    def fetch_categories(self, query: str = "") -> list[dict]:
        """Справочник предметов WB → список вариантов с subjectID.

        WB отдаёт предметы через /content/v2/object/all (можно искать по name).
        По умолчанию подсказываем предметы со словом «книг».
        """
        params = {"limit": 1000, "locale": "ru"}
        # Пустой поиск заменяем на «книг» — для магазина книг это нужный раздел.
        params["name"] = (query or "книг").strip()
        data = self._request(
            "GET",
            f"{CONTENT_URL}/content/v2/object/all",
            params=params,
        )
        subjects = data.get("data") or []
        out: list[dict] = []
        for subj in subjects:
            subject_id = subj.get("subjectID") or subj.get("subjectId")
            name = subj.get("subjectName") or subj.get("name") or ""
            parent = subj.get("parentName") or ""
            if not subject_id:
                continue
            label = f"{parent} → {name}" if parent else name
            out.append(
                {
                    "label": label,
                    "fields": {"subject_id": str(subject_id)},
                }
            )
        out.sort(key=lambda o: (0 if "книг" in o["label"].lower() else 1, o["label"]))
        return out

    def publish(self, book) -> PublishResult:
        """Создать/обновить карточку товара и выставить остаток 1.

        WB, как и Ozon, обрабатывает карточки асинхронно: /content/v2/cards/upload
        ставит задачу. vendorCode (наш SKU) сразу известен и служит внешним
        идентификатором лота.
        """
        vendor_code = book.sku
        price = int(book.price) if book.price is not None else 0
        quantity = int(getattr(book, "quantity", 1) or 1)

        # Предмет теперь задаётся у самой книги (подбирается в карточке).
        # На старых настройках значение могло лежать в ключах — берём как запас.
        subject_id = str(getattr(book, "wb_subject_id", "") or "").strip() or self.subject_id

        # Без предмета WB отклоняет карточку (subjectID is not provided or zero).
        if not subject_id:
            raise MarketplaceError(
                "Для публикации на Wildberries укажите в карточке книги предмет «Книги»"
            )

        # Реальный ISBN используем как баркод (sku); «нет»/пусто — без баркода.
        barcode = book.isbn if book.isbn and book.isbn != "нет" else None

        card = {
            "vendorCode": vendor_code,
            "title": book.title,
            # Книги б/у не имеют размеров; WB требует хотя бы один размер с ценой.
            "sizes": [{"price": price, "skus": [barcode] if barcode else []}],
        }
        if book.description:
            card["description"] = book.description
        if book.publisher:
            card["brand"] = book.publisher
        # WB тоже скачивает фото по ссылке — нужен абсолютный URL с хостом.
        images = public_photo_list(book)
        if images:
            card["photos"] = [{"url": u} for u in images]

        # WB группирует карточки по предметам; для книг отправляем одиночную группу.
        self._post(
            f"{CONTENT_URL}/content/v2/cards/upload",
            [{"subjectID": int(subject_id), "variants": [card]}],
        )

        # Цену выставляем отдельно (на случай, если карточка уже существует).
        self._set_price(vendor_code, price)
        # И остаток на складе FBS, если склад задан.
        self._set_stock(barcode or vendor_code, quantity)

        return PublishResult(external_id=vendor_code, raw={"vendorCode": vendor_code})

    def withdraw(self, listing) -> None:
        """Снять лот с продажи — обнуляем остаток на складе FBS."""
        sku = listing.external_id
        if not sku:
            raise MarketplaceError("У лота Wildberries нет vendorCode — нечего снимать")
        self._set_stock(sku, 0)

    def _set_price(self, vendor_code: str, price: int) -> None:
        if price <= 0:
            return
        self._post(
            f"{PRICES_URL}/api/v2/upload/task",
            {"data": [{"vendorCode": vendor_code, "price": price}]},
        )

    def _set_stock(self, sku: str, stock: int) -> None:
        # Без склада FBS остатки WB не принимает — тихо пропускаем.
        if not self.warehouse_id:
            return
        self._request(
            "PUT",
            f"{MARKETPLACE_URL}/api/v3/stocks/{self.warehouse_id}",
            {"stocks": [{"sku": sku, "amount": stock}]},
        )

    def _fetch_stocks(self, skus: list[str]) -> dict[str, int]:
        """Остатки FBS по баркодам (sku). Тот же метод складов, но POST-запросом.

        WB принимает до 1000 sku за раз, поэтому шлём батчами. Возвращаем
        {sku: остаток}. Если склад не задан — остатки узнать неоткуда, {}.
        """
        result: dict[str, int] = {}
        if not self.warehouse_id or not skus:
            return result
        for i in range(0, len(skus), 1000):
            batch = skus[i:i + 1000]
            data = self._request(
                "POST",
                f"{MARKETPLACE_URL}/api/v3/stocks/{self.warehouse_id}",
                {"skus": batch},
            )
            for st in data.get("stocks") or []:
                sku = st.get("sku")
                if sku is not None:
                    result[str(sku)] = st.get("amount") or 0
        return result

    def fetch_catalog(self) -> list[dict]:
        """Выгрузить все карточки WB постранично (курсор updatedAt + nmID).

        WB листает карточки курсором: в ответ приходит cursor с updatedAt/nmID и
        total; когда total меньше размера страницы — карточки кончились.
        """
        rows: list[dict] = []
        cursor: dict = {"limit": 100}
        while True:
            page = self._post(
                f"{CONTENT_URL}/content/v2/get/cards/list",
                {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}},
            )
            cards = page.get("cards") or []
            for card in cards:
                # Баркод и цена лежат в размерах (у книг один размер).
                sizes = card.get("sizes") or []
                barcode = None
                price = None
                if sizes:
                    skus = sizes[0].get("skus") or []
                    barcode = skus[0] if skus else None
                    price = sizes[0].get("price")
                rows.append(
                    {
                        "sku": card.get("vendorCode"),
                        "external_id": card.get("vendorCode"),
                        "title": card.get("title") or card.get("subjectName"),
                        "publisher": card.get("brand"),
                        "isbn": barcode,
                        "price": str(price) if price not in (None, "") else None,
                        # Баркод FBS — по нему ниже подтянем остаток со склада.
                        "_barcode": barcode,
                    }
                )

            # Продолжаем, пока страница полностью заполнена.
            resp_cursor = page.get("cursor") or {}
            total = resp_cursor.get("total", len(cards))
            if total < cursor["limit"] or not cards:
                break
            cursor = {
                "limit": cursor["limit"],
                "updatedAt": resp_cursor.get("updatedAt"),
                "nmID": resp_cursor.get("nmID"),
            }

        # Одним махом узнаём остатки FBS по всем баркодам и проставляем stock.
        # Нет склада/баркода → остаток неизвестен, оставляем None (импорт решит).
        barcodes = [r["_barcode"] for r in rows if r.get("_barcode")]
        stocks = self._fetch_stocks(barcodes)
        for r in rows:
            bc = r.pop("_barcode", None)
            r["stock"] = stocks.get(bc) if bc else None
        return rows

    def fetch_orders(self) -> list[OrderInfo]:
        """Получить новые сборочные задания (заказы FBS). Каждый — проданная книга."""
        data = self._request(
            "GET", f"{MARKETPLACE_URL}/api/v3/orders/new"
        )
        result: list[OrderInfo] = []
        for order in data.get("orders", []):
            # WB отдаёт article — это наш vendorCode (SKU книги).
            article = order.get("article")
            order_id = order.get("id") or order.get("rid")
            result.append(
                OrderInfo(
                    external_order_id=str(order_id),
                    external_sku=str(article) if article else None,
                )
            )
        return result
