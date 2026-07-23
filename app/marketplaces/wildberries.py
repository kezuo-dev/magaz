"""Клиент Wildberries Seller API.

Документация: https://dev.wildberries.ru/en/openapi/api-information
Аутентификация — один заголовок Authorization с API-токеном (создаётся в ЛК
продавца: Профиль → Настройки → Доступ к API). Токен храним шифрованно.

WB разнёс методы по нескольким доменам:
- content-api  — карточки товаров (выгрузка каталога);
- marketplace-api — остатки на складах FBS и заказы (сборочные задания).

Книги б/у в единственном экземпляре. SKU книги = vendorCode на стороне WB.
Программа только отслеживает каталог и снимает проданное (обнуляет остаток).
"""
from __future__ import annotations

import time

import httpx

from app.marketplaces.base import (
    MarketplaceClient,
    MarketplaceError,
    OrderInfo,
)

CONTENT_URL = "https://content-api.wildberries.ru"
MARKETPLACE_URL = "https://marketplace-api.wildberries.ru"
TIMEOUT = 30.0
# Устойчивость к лимитам WB: на 429 и 5xx повторяем с нарастающей паузой.
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_RETRIES = 4
RETRY_BACKOFF = 1.5  # секунды: 1.5, 3, 4.5, 6


class WBClient(MarketplaceClient):
    marketplace = "wildberries"

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self.token = str(credentials.get("api_token", "")).strip()
        if not self.token:
            raise MarketplaceError("Не задан API-токен для Wildberries")
        # Склад FBS, откуда читаем остатки и куда пишем 0 при снятии. Необязателен:
        # если не задан, остатки не трогаем.
        self.warehouse_id = str(credentials.get("warehouse_id", "")).strip()

    # --- инфраструктура ---------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Authorization": self.token,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, url: str, payload: dict | None = None, params: dict | None = None) -> dict:
        """Запрос к WB с единой обработкой ошибок и ретраями. Возвращает JSON."""
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = httpx.request(
                    method, url, json=payload, params=params, headers=self._headers(), timeout=TIMEOUT
                )
            except httpx.HTTPError as exc:
                last_exc = MarketplaceError(f"Сеть Wildberries недоступна: {exc}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF * (attempt + 1))
                    continue
                raise last_exc from exc

            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                # Лимит (429) или временный сбой WB — ждём и повторяем.
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue

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

        raise last_exc or MarketplaceError("Wildberries: превышено число повторов запроса (лимит/сбой)")

    def _post(self, url: str, payload: dict) -> dict:
        return self._request("POST", url, payload)

    # --- операции ---------------------------------------------------------

    def check_connection(self) -> None:
        """Лёгкий запрос списка карточек — проверяем, что токен рабочий."""
        self._post(
            f"{CONTENT_URL}/content/v2/get/cards/list",
            {"settings": {"cursor": {"limit": 1}, "filter": {"withPhoto": -1}}},
        )

    def withdraw(self, listing) -> None:
        """Снять лот с продажи — обнуляем остаток на складе FBS.

        ВАЖНО: остаток WB привязан к БАРКОДУ (skus[0]), а не к vendorCode. Баркод
        мы храним в listing.stock_key. Если по ошибке отправить vendorCode, WB не
        найдёт запись на складе и остаток не обнулится — книга останется висеть.
        """
        barcode = getattr(listing, "stock_key", None) or listing.external_id
        if not barcode:
            raise MarketplaceError("У лота Wildberries нет баркода — нечего снимать")
        self._set_stock(barcode, 0)

    def _set_stock(self, sku: str, stock: int) -> None:
        # Без склада FBS остатки WB не принимает — тихо пропускаем.
        if not self.warehouse_id:
            return
        self._request(
            "PUT",
            f"{MARKETPLACE_URL}/api/v3/stocks/{self.warehouse_id}",
            {"stocks": [{"sku": sku, "amount": stock}]},
        )

    def fetch_stocks(self, keys: list[str]) -> dict[str, int]:
        """Остатки FBS по баркодам (ключам остатка). Возвращает {баркод: остаток}.

        Тот же метод складов WB, но POST-запросом. WB принимает до 1000 sku за раз,
        поэтому шлём батчами. Если склад не задан — остатки узнать неоткуда, {}.
        Дешёвая проверка «книга ещё в наличии?» без выгрузки всех карточек.
        """
        result: dict[str, int] = {}
        if not self.warehouse_id or not keys:
            return result
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
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
                        # Остаток WB читается по баркоду (skus[0]), а не по vendorCode —
                        # храним его как ключ остатка для слежения.
                        "stock_key": barcode,
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
        # in_sale = «карточка реально продаётся». Если склад FBS задан — это есть
        # баркод И положительный остаток (мёртвые карточки с остатком 0/без баркода
        # отсеются при заведении новых книг). Если склада нет, остатки узнать неоткуда
        # — тогда не блокируем: считаем продающейся любую карточку с баркодом.
        barcodes = [r["_barcode"] for r in rows if r.get("_barcode")]
        stocks = self.fetch_stocks(barcodes)
        have_stock_data = bool(self.warehouse_id)
        for r in rows:
            bc = r.pop("_barcode", None)
            amount = stocks.get(bc) if bc else None
            r["stock"] = amount
            if have_stock_data:
                r["in_sale"] = bool(bc) and amount is not None and amount > 0
            else:
                r["in_sale"] = bool(bc)
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
