"""Клиент Ozon Seller API.

Документация: https://docs.ozon.ru/api/seller/
Аутентификация — два заголовка: Client-Id и Api-Key (берутся в личном кабинете
продавца: Настройки → Seller API). Ключи храним шифрованно в MarketplaceAccount.

Книги б/у в единственном экземпляре, поэтому остаток всегда 1 (или 0 при снятии).
SKU книги используем как offer_id — это наш артикул на стороне Ozon.
"""
from __future__ import annotations

import httpx

from app.marketplaces.base import (
    MarketplaceClient,
    MarketplaceError,
    OrderInfo,
)

BASE_URL = "https://api-seller.ozon.ru"
TIMEOUT = 30.0


class OzonClient(MarketplaceClient):
    marketplace = "ozon"

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self.client_id = str(credentials.get("client_id", "")).strip()
        self.api_key = str(credentials.get("api_key", "")).strip()
        if not self.client_id or not self.api_key:
            raise MarketplaceError("Не заданы Client-Id и Api-Key для Ozon")
        # Склад FBS («Мои склады»). Остатки на Ozon обнуляются при снятии через
        # /v2/products/stocks с указанием warehouse_id — без него Ozon остаток
        # не примет. ID берётся в ЛК Ozon: Логистика → Мои склады.
        self.warehouse_id = str(credentials.get("warehouse_id", "")).strip()

    # --- инфраструктура ---------------------------------------------------

    def _headers(self) -> dict:
        return {
            "Client-Id": self.client_id,
            "Api-Key": self.api_key,
            "Content-Type": "application/json",
        }

    def _post(self, path: str, payload: dict) -> dict:
        """POST к Ozon с обработкой ошибок. Возвращает распарсенный JSON."""
        url = f"{BASE_URL}{path}"
        try:
            resp = httpx.post(url, json=payload, headers=self._headers(), timeout=TIMEOUT)
        except httpx.HTTPError as exc:
            raise MarketplaceError(f"Сеть Ozon недоступна: {exc}") from exc

        if resp.status_code == 401 or resp.status_code == 403:
            raise MarketplaceError("Ozon отклонил ключи (401/403). Проверьте Client-Id и Api-Key")
        if resp.status_code >= 400:
            # Ozon кладёт причину в тело ответа — вытаскиваем её для журнала.
            detail = ""
            try:
                detail = resp.json().get("message") or resp.text
            except Exception:
                detail = resp.text
            raise MarketplaceError(f"Ozon вернул {resp.status_code}: {detail}")

        try:
            return resp.json()
        except Exception as exc:
            raise MarketplaceError(f"Ozon вернул не-JSON: {resp.text[:200]}") from exc

    # --- операции ---------------------------------------------------------

    def check_connection(self) -> None:
        """Лёгкий запрос списка товаров — проверяем, что ключи рабочие."""
        self._post("/v3/product/list", {"filter": {}, "limit": 1})

    def withdraw(self, listing) -> None:
        """Снять лот с продажи — обнуляем остаток по offer_id."""
        offer_id = listing.external_id
        if not offer_id:
            raise MarketplaceError("У лота Ozon нет offer_id — нечего снимать")
        self._set_stock(offer_id, 0)

    def _set_stock(self, offer_id: str, stock: int) -> None:
        """Выставить остаток на складе FBS.

        Ozon принимает остаток только с указанием склада (warehouse_id). Без
        него запрос отклоняется, и снятие/выставление молча не срабатывает —
        поэтому явно требуем настроенный склад.
        """
        if not self.warehouse_id:
            raise MarketplaceError(
                "Не задан ID склада FBS для Ozon. Укажите его в настройках "
                "площадки (ЛК Ozon → Логистика → Мои склады)."
            )
        self._post(
            "/v2/products/stocks",
            {
                "stocks": [
                    {
                        "offer_id": offer_id,
                        "stock": stock,
                        "warehouse_id": int(self.warehouse_id),
                    }
                ]
            },
        )

    def fetch_catalog(self) -> list[dict]:
        """Выгрузить все товары Ozon постранично (по last_id).

        /v3/product/list отдаёт offer_id и product_id; детали (название, цена,
        баркод) берём пачками через /v3/product/info/list.
        """
        rows: list[dict] = []
        last_id = ""
        while True:
            # visibility=IN_SALE — только товары со статусом «В продаже».
            # Без фильтра Ozon отдаёт и карточки «Готовы к продаже» (прошли
            # модерацию, но не продаются), которые нам импортировать не нужно.
            page = self._post(
                "/v3/product/list",
                {"filter": {"visibility": "IN_SALE"}, "last_id": last_id, "limit": 1000},
            )
            result = page.get("result") or {}
            items = result.get("items") or []
            if not items:
                break

            offer_ids = [it.get("offer_id") for it in items if it.get("offer_id")]
            if offer_ids:
                info = self._post(
                    "/v3/product/info/list", {"offer_id": offer_ids}
                )
                for prod in (info.get("result") or {}).get("items") or info.get("items") or []:
                    offer_id = prod.get("offer_id")
                    price = prod.get("marketing_price") or prod.get("price")
                    barcode = prod.get("barcode")
                    if not barcode:
                        barcodes = prod.get("barcodes") or []
                        barcode = barcodes[0] if barcodes else None
                    rows.append(
                        {
                            "sku": offer_id,
                            "external_id": offer_id,
                            "title": prod.get("name"),
                            "isbn": barcode,
                            "price": str(price) if price not in (None, "") else None,
                        }
                    )

            last_id = result.get("last_id") or ""
            if not last_id:
                break
        return rows

    def fetch_orders(self) -> list[OrderInfo]:
        """Получить недавние отправления FBS. Каждый товар в заказе — проданная книга.

        Ozon требует диапазон дат: у /v3/posting/fbs/list поля фильтра называются
        since/to (ISO 8601). Без них метод отвечает 400 «processed_at_to must be
        set» — это внутреннее имя Ozon, но в запросе ждёт именно since/to. Берём
        окно последних дней: свежие заказы для кросс-снятия, старые не нужны.
        """
        from datetime import datetime, timedelta, timezone

        now = datetime.now(timezone.utc)
        since = now - timedelta(days=3)
        # Формат Ozon — ISO 8601 с Z на конце.
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        data = self._post(
            "/v3/posting/fbs/list",
            {
                "dir": "DESC",
                "filter": {
                    "status": "",
                    "since": since.strftime(fmt),
                    "to": now.strftime(fmt),
                },
                "limit": 100,
                "offset": 0,
                "with": {},
            },
        )
        result: list[OrderInfo] = []
        postings = (data.get("result") or {}).get("postings") or []
        for posting in postings:
            order_number = posting.get("posting_number") or posting.get("order_number")
            for product in posting.get("products", []):
                result.append(
                    OrderInfo(
                        external_order_id=str(order_number),
                        external_sku=str(product.get("offer_id")) if product.get("offer_id") else None,
                    )
                )
        return result
