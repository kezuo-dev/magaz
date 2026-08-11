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

    def sell(self, listing) -> None:
        """Снять лот после продажи — только обнулить остаток (БЕЗ удаления в корзину).

        Используется при обработке заказов и сверке. Удаление в корзину вынесено в
        отдельную кнопку «Очистить корзину WB», чтобы не схлопнуть лимит API (429).
        """
        self.last_warning = None
        barcode = getattr(listing, "stock_key", None)
        if not barcode:
            raise MarketplaceError(
                "У лота Wildberries нет баркода (stock_key) — снять остаток нельзя. "
                "Нужна сверка каталога, чтобы подтянуть баркод."
            )
        self._set_stock(barcode, 0)

    def withdraw(self, listing) -> None:
        """Снять лот с продажи — обнуляем остаток на складе FBS, затем удаляем карточку в корзину.

        ВАЖНО: остаток WB привязан к БАРКОДУ (skus[0]), а не к vendorCode. Баркод
        мы храним в listing.stock_key. Если по ошибке отправить vendorCode, WB не
        найдёт запись на складе и остаток не обнулится — книга останется висеть.
        """
        self.last_warning = None
        # Только stock_key (баркод). НЕ откатываемся на external_id: там vendorCode,
        # и WB молча не найдёт по нему запись — книга осталась бы висеть в продаже.
        barcode = getattr(listing, "stock_key", None)
        if not barcode:
            raise MarketplaceError(
                "У лота Wildberries нет баркода (stock_key) — снять остаток нельзя. "
                "Нужна сверка каталога, чтобы подтянуть баркод."
            )
        # Шаг 1: обнуляем остаток
        self._set_stock(barcode, 0)

        # Шаг 2: удаляем карточку в корзину (нужен nmID или vendorCode)
        external_id = listing.external_id
        if external_id:
            try:
                self._move_to_trash(external_id)
            except MarketplaceError as exc:
                # Остаток уже обнулён — книга не продаётся. Но карточка видна в кабинете,
                # поэтому пишем в last_warning: sync.py положит это в журнал базы.
                self.last_warning = f"Остаток обнулён, но карточку {external_id} не удалось удалить в корзину WB: {exc}"
                import logging
                logging.getLogger("wildberries").warning(self.last_warning)

    def _set_stock(self, sku: str, stock: int) -> None:
        # Без склада FBS остатки WB не принимает. Это не штатная ситуация при
        # снятии — сообщаем явно, иначе снятие «прошло бы» вникуда.
        if not self.warehouse_id:
            raise MarketplaceError(
                "Не задан склад FBS Wildberries (warehouse_id) — остаток не изменить"
            )
        self._request(
            "PUT",
            f"{MARKETPLACE_URL}/api/v3/stocks/{self.warehouse_id}",
            {"stocks": [{"sku": sku, "amount": stock}]},
        )

    def _move_to_trash(self, external_id: str) -> None:
        """Переместить карточку товара в корзину WB.

        WB API: POST /content/v2/cards/delete/trash с телом {"nmIDs": [123456789]}.
        Принимает только nmID (числовой ID карточки), vendorCode не поддерживается.
        В listing.external_id хранится nmID (с момента обновления сверки каталога).
        """
        try:
            nm_id = int(external_id)
        except (ValueError, TypeError) as exc:
            raise MarketplaceError(
                f"У лота Wildberries external_id={external_id} не число — удалить в корзину нельзя. "
                f"Нужна сверка каталога, чтобы подтянуть nmID."
            ) from exc

        self._post(
            f"{CONTENT_URL}/content/v2/cards/delete/trash",
            {"nmIDs": [nm_id]},
        )

    def _restore_from_trash(self, external_id: str) -> None:
        """Восстановить карточку из корзины WB.

        WB API: POST /content/v2/cards/recover с телом {"nmIDs": [123456789]}.
        Принимает только nmID (числовой ID карточки).
        """
        try:
            nm_id = int(external_id)
        except (ValueError, TypeError) as exc:
            raise MarketplaceError(
                f"У лота Wildberries external_id={external_id} не число — восстановить из корзины нельзя"
            ) from exc

        self._post(
            f"{CONTENT_URL}/content/v2/cards/recover",
            {"nmIDs": [nm_id]},
        )

    def restore(self, listing) -> None:
        """Вернуть карточку WB в продажу после отмены заказа.

        Шаг 1 — восстановить из корзины через /content/v2/cards/recover.
        WB отдаёт 400 если карточка не в корзине — это нормально (заказ мог
        быть отменён до того, как мы успели убрать в корзину). Такой случай
        молча пропускаем. Реальные ошибки (сеть, 401, 5xx) — предупреждение.
        Шаг 2 — выставить остаток 1 по баркоду (stock_key).
        """
        self.last_warning = None
        external_id = listing.external_id
        barcode = getattr(listing, "stock_key", None)

        # Шаг 1: восстанавливаем из корзины
        if external_id:
            try:
                self._restore_from_trash(external_id)
            except MarketplaceError as exc:
                err = str(exc)
                if "400" in err or "bad request" in err.lower():
                    # Карточки не было в корзине — ничего восстанавливать не нужно.
                    pass
                else:
                    self.last_warning = (
                        f"Не удалось восстановить карточку {external_id} из корзины: {exc}. "
                        "Остаток выставлен, карточка может остаться в корзине."
                    )
                    import logging
                    logging.getLogger("wildberries").warning(self.last_warning)

        # Шаг 2: выставляем остаток 1
        if not barcode:
            raise MarketplaceError(
                "У лота Wildberries нет баркода (stock_key) — остаток не выставить."
            )
        self._set_stock(barcode, 1)

    def fetch_stocks(self, keys: list[str]) -> dict[str, int]:
        """Остатки FBS по баркодам (ключам остатка). Возвращает {баркод: остаток}.

        Тот же метод складов WB, но POST-запросом. WB принимает до 1000 sku за раз,
        поэтому шлём батчами. Дешёвая проверка «книга ещё в наличии?» без выгрузки
        всех карточек.

        Если склад не задан — бросаем ошибку, а НЕ возвращаем пустой словарь:
        по контракту отсутствие ключа в ответе означает «карточка пропала», и
        пустой ответ на ненастроенном складе привёл бы к массовому ложному снятию
        всего каталога. Лучше честно прервать слежение с понятной причиной.
        """
        result: dict[str, int] = {}
        if not keys:
            return result
        if not self.warehouse_id:
            raise MarketplaceError(
                "Не задан склад FBS Wildberries (warehouse_id) — остатки не прочитать. "
                "Укажите ID склада в настройках площадки."
            )
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            data = self._request(
                "POST",
                f"{MARKETPLACE_URL}/api/v3/stocks/{self.warehouse_id}",
                {"skus": batch},
            )
            for st in data.get("stocks") or []:
                sku = st.get("sku")
                if sku is None:
                    continue
                # amount читаем строго: `or 0` превращал бы null («данных по этому
                # баркоду нет») в честный ноль, а ноль вызывающий код трактует как
                # снятие с продажи и обнуляет остаток на другой площадке. Запись без
                # amount пропускаем — отсутствие ключа в ответе означает «не знаю»,
                # и слежение такие ключи защищает порогом, а не снимает.
                amount = st.get("amount")
                if amount is None:
                    continue
                try:
                    result[str(sku)] = int(amount)
                except (TypeError, ValueError):
                    continue
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

                # nmID — внутренний ID карточки WB, нужен для удаления в корзину.
                # Сохраняем его в external_id вместо vendorCode: при удалении API
                # требует именно nmID. vendorCode остаётся в sku (он и есть наш SKU).
                nm_id = card.get("nmID")
                rows.append(
                    {
                        "sku": card.get("vendorCode"),
                        "external_id": str(nm_id) if nm_id else card.get("vendorCode"),
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
            next_updated = resp_cursor.get("updatedAt")
            next_nm = resp_cursor.get("nmID")
            # Защита от зацикливания: если WB на полной странице не отдал курсор
            # (updatedAt/nmID пусты), сдвинуться некуда — тот же запрос вернул бы ту
            # же страницу. Прекращаем, а не крутим бесконечно один и тот же батч.
            if next_updated is None or next_nm is None:
                break
            cursor = {
                "limit": cursor["limit"],
                "updatedAt": next_updated,
                "nmID": next_nm,
            }

        # Одним махом узнаём остатки FBS по всем баркодам и проставляем stock.
        # in_sale = «карточка реально продаётся». Если склад FBS задан — это есть
        # баркод И положительный остаток (мёртвые карточки с остатком 0/без баркода
        # отсеются при заведении новых книг). Если склада нет, остатки узнать неоткуда
        # — тогда не блокируем: считаем продающейся любую карточку с баркодом.
        barcodes = [r["_barcode"] for r in rows if r.get("_barcode")]
        have_stock_data = bool(self.warehouse_id)
        # Без склада fetch_stocks намеренно бросает ошибку (защита слежения от
        # ложных снятий). Но полной сверке каталога остатки не обязательны —
        # она и без них заведёт книги, поэтому здесь деградируем мягко.
        stocks = self.fetch_stocks(barcodes) if have_stock_data else {}
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

    def fetch_cancelled_orders(self) -> list["CancelledOrderInfo"]:
        """Получить отменённые сборочные задания.

        WB держит отменённые заказы в /api/v3/orders/cancel. Если у заказа
        заполнен supplyId — он был добавлен в поставку, то есть книга физически
        уехала; при такой отмене остаток не восстанавливаем (already_shipped=True).
        """
        from app.marketplaces.base import CancelledOrderInfo
        try:
            data = self._request(
                "GET", f"{MARKETPLACE_URL}/api/v3/orders/cancel"
            )
            result: list[CancelledOrderInfo] = []
            for order in data.get("orders", []):
                order_id = order.get("id") or order.get("rid")
                if order_id:
                    already_shipped = bool(order.get("supplyId"))
                    result.append(CancelledOrderInfo(
                        external_order_id=str(order_id),
                        already_shipped=already_shipped,
                    ))
            return result
        except MarketplaceError:
            # Если эндпоинт не поддерживается — не роняем процесс, отмены
            # просто не обработаются в этот раз.
            return []
