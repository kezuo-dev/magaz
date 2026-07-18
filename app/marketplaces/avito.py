"""Клиент Avito API.

Документация: https://developers.avito.ru/api-catalog
Аутентификация — OAuth 2.0, grant type client_credentials: по client_id и
client_secret получаем короткоживущий access_token (заголовок
Authorization: Bearer). Токен кэшируем в памяти клиента и переполучаем при
истечении. client_id/client_secret храним шифрованно.

Публикация у Avito идёт через «Автозагрузку»: мы отдаём набор объявлений одним
вызовом, Avito обрабатывает их асинхронно и присылает отчёт. SKU книги
используем как Id объявления (наш внешний идентификатор на стороне Avito).

Точные пути публикации/заказов у Avito зависят от версии API и уровня доступа
аккаунта — вынесены в константы ниже, чтобы при подключении живых ключей
поправить их в одном месте.
"""
from __future__ import annotations

import time

import httpx

from app.marketplaces.base import (
    MarketplaceClient,
    MarketplaceError,
    OrderInfo,
    PublishResult,
)

BASE_URL = "https://api.avito.ru"
TOKEN_PATH = "/token/"                       # POST, grant_type=client_credentials
ITEMS_UPLOAD_PATH = "/autoload/v2/items"     # POST — выгрузка объявлений (автозагрузка)
LAST_REPORT_PATH = "/autoload/v1/accounts/{user_id}/reports/last_report/"
ORDERS_PATH = "/core/v1/accounts/{user_id}/orders"  # GET — заказы аккаунта
ITEMS_LIST_PATH = "/core/v1/items"           # GET — список объявлений аккаунта
TIMEOUT = 30.0


class AvitoClient(MarketplaceClient):
    marketplace = "avito"

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self.client_id = str(credentials.get("client_id", "")).strip()
        self.client_secret = str(credentials.get("client_secret", "")).strip()
        if not self.client_id or not self.client_secret:
            raise MarketplaceError("Не заданы Client ID и Client Secret для Avito")
        # user_id (номер профиля Avito) нужен для отчётов автозагрузки и заказов.
        self.user_id = str(credentials.get("user_id", "")).strip()
        # Кэш access-токена: (значение, момент истечения в epoch-секундах).
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # --- OAuth ------------------------------------------------------------

    def _get_token(self) -> str:
        """Вернуть действующий access_token, переполучив при необходимости.

        Кэшируем в памяти и обновляем за 60 секунд до истечения, чтобы не
        ходить за токеном на каждый вызов.
        """
        if self._token and time.time() < self._token_expires_at - 60:
            return self._token

        url = f"{BASE_URL}{TOKEN_PATH}"
        try:
            resp = httpx.post(
                url,
                data={
                    "grant_type": "client_credentials",
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                },
                headers={"Content-Type": "application/x-www-form-urlencoded"},
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise MarketplaceError(f"Сеть Avito недоступна: {exc}") from exc

        if resp.status_code in (401, 403):
            raise MarketplaceError(
                "Avito отклонил ключи (401/403). Проверьте Client ID и Client Secret"
            )
        if resp.status_code >= 400:
            raise MarketplaceError(f"Avito вернул {resp.status_code} при получении токена: {resp.text[:200]}")

        try:
            body = resp.json()
        except Exception as exc:
            raise MarketplaceError(f"Avito вернул не-JSON токен: {resp.text[:200]}") from exc

        token = body.get("access_token")
        if not token:
            raise MarketplaceError(f"Avito не вернул access_token: {body}")
        self._token = token
        self._token_expires_at = time.time() + float(body.get("expires_in", 3600))
        return token

    # --- инфраструктура ---------------------------------------------------

    def _request(self, method: str, path: str, payload: dict | None = None) -> dict:
        """Запрос к Avito с Bearer-токеном и единой обработкой ошибок."""
        token = self._get_token()
        url = f"{BASE_URL}{path}"
        try:
            resp = httpx.request(
                method,
                url,
                json=payload,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                },
                timeout=TIMEOUT,
            )
        except httpx.HTTPError as exc:
            raise MarketplaceError(f"Сеть Avito недоступна: {exc}") from exc

        if resp.status_code in (401, 403):
            raise MarketplaceError("Avito отклонил токен (401/403). Проверьте права доступа приложения")
        if resp.status_code == 429:
            raise MarketplaceError("Avito: превышен лимит запросов (429), повторите позже")
        if resp.status_code >= 400:
            detail = ""
            try:
                body = resp.json()
                detail = body.get("message") or (body.get("error") or {}).get("message") or resp.text
            except Exception:
                detail = resp.text
            raise MarketplaceError(f"Avito вернул {resp.status_code}: {detail or resp.text[:200]}")

        if not resp.content:
            return {}
        try:
            return resp.json()
        except Exception as exc:
            raise MarketplaceError(f"Avito вернул не-JSON: {resp.text[:200]}") from exc

    def _require_user_id(self) -> str:
        if not self.user_id:
            raise MarketplaceError("Не задан User ID (номер профиля) Avito")
        return self.user_id

    # --- операции ---------------------------------------------------------

    def check_connection(self) -> None:
        """Проверяем ключи, получив OAuth-токен. Успех = ключи рабочие."""
        self._get_token()

    def publish(self, book) -> PublishResult:
        """Выставить/обновить объявление через автозагрузку.

        Id объявления — наш SKU: по нему потом снимаем и сопоставляем заказы.
        Avito обрабатывает выгрузку асинхронно; результат приходит в отчёте.
        """
        ad_id = book.sku
        ad = {
            "Id": ad_id,
            "Title": book.title,
            "Price": int(book.price) if book.price is not None else 0,
        }
        if book.description:
            ad["Description"] = book.description
        if book.author:
            ad["Author"] = book.author
        images = book.photo_list
        if images:
            ad["Images"] = [{"url": u} for u in images]

        self._request("POST", ITEMS_UPLOAD_PATH, {"items": [ad]})
        return PublishResult(external_id=ad_id, raw={"Id": ad_id})

    def withdraw(self, listing) -> None:
        """Снять объявление: выгружаем его с нулевым остатком (снятие с публикации)."""
        ad_id = listing.external_id
        if not ad_id:
            raise MarketplaceError("У лота Avito нет Id — нечего снимать")
        # У Avito снятие — это выгрузка объявления со статусом «снято с публикации».
        self._request(
            "POST",
            ITEMS_UPLOAD_PATH,
            {"items": [{"Id": ad_id, "AllowEmail": "No", "Status": "Removed"}]},
        )

    def fetch_catalog(self) -> list[dict]:
        """Выгрузить все объявления аккаунта постранично.

        Avito листает объявления параметрами page/per_page. Id объявления = наш
        SKU; по нему сопоставляем с уже заведёнными книгами.
        """
        rows: list[dict] = []
        page = 1
        per_page = 100
        while True:
            data = self._request(
                "GET", f"{ITEMS_LIST_PATH}?per_page={per_page}&page={page}"
            )
            resources = data.get("resources") or data.get("items") or []
            for item in resources:
                item_id = item.get("id") or item.get("Id")
                price = item.get("price") or item.get("Price")
                rows.append(
                    {
                        "sku": str(item_id) if item_id else None,
                        "external_id": str(item_id) if item_id else None,
                        "title": item.get("title") or item.get("Title"),
                        "price": str(price) if price not in (None, "") else None,
                    }
                )
            # Продолжаем, пока страница заполнена целиком.
            if len(resources) < per_page or not resources:
                break
            page += 1
        return rows

    def fetch_orders(self) -> list[OrderInfo]:
        """Получить заказы аккаунта. Каждый заказ — проданная книга.

        Если у аккаунта нет доступа к заказам, Avito вернёт ошибку — вызывающий
        код (sync.poll) залогирует её и продолжит работу.
        """
        user_id = self._require_user_id()
        data = self._request("GET", ORDERS_PATH.format(user_id=user_id))
        result: list[OrderInfo] = []
        for order in data.get("orders", []):
            order_id = order.get("id")
            # В заказе Avito товар лежит в items; Id объявления — наш SKU.
            items = order.get("items") or []
            ad_id = items[0].get("id") if items else order.get("item_id")
            result.append(
                OrderInfo(
                    external_order_id=str(order_id),
                    external_sku=str(ad_id) if ad_id else None,
                )
            )
        return result
