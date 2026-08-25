"""Клиент Ozon Seller API.

Документация: https://docs.ozon.ru/api/seller/
Аутентификация — два заголовка: Client-Id и Api-Key (берутся в личном кабинете
продавца: Настройки → Seller API). Ключи храним шифрованно в MarketplaceAccount.

Книги б/у в единственном экземпляре, поэтому остаток всегда 1 (или 0 при снятии).
SKU книги используем как offer_id — это наш артикул на стороне Ozon.
"""
from __future__ import annotations

import time

import httpx

from app.marketplaces.base import (
    MarketplaceClient,
    MarketplaceError,
    OrderInfo,
)

BASE_URL = "https://api-seller.ozon.ru"
TIMEOUT = 30.0
# Устойчивость к лимитам: на 429 и 5xx повторяем с нарастающей паузой. При
# слежении за остатками 50k книг запросов много — без ретраев лимит рвал бы сверку.
RETRY_STATUSES = (429, 500, 502, 503, 504)
MAX_RETRIES = 4
RETRY_BACKOFF = 1.5  # секунды: 1.5, 3, 4.5, 6


def _first_positive_price(*values) -> float | None:
    """Первая корректная положительная цена из переданных (Ozon шлёт их строками).

    marketing_price без акции приходит как "0"/0 — такое значение пропускаем и
    берём следующее (обычную price). Если ничего валидного нет — None.
    """
    for v in values:
        if v is None or v == "":
            continue
        try:
            num = float(v)
        except (TypeError, ValueError):
            continue
        if num > 0:
            return num
    return None


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
        """POST к Ozon с обработкой ошибок и ретраями. Возвращает распарсенный JSON."""
        url = f"{BASE_URL}{path}"
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = httpx.post(url, json=payload, headers=self._headers(), timeout=TIMEOUT)
            except httpx.HTTPError as exc:
                # Сетевой сбой — тоже повод повторить (кратковременная потеря связи).
                last_exc = MarketplaceError(f"Сеть Ozon недоступна: {exc}")
                if attempt < MAX_RETRIES:
                    time.sleep(RETRY_BACKOFF * (attempt + 1))
                    continue
                raise last_exc from exc

            if resp.status_code in RETRY_STATUSES and attempt < MAX_RETRIES:
                # Лимит/временный сбой Ozon — ждём и повторяем.
                time.sleep(RETRY_BACKOFF * (attempt + 1))
                continue

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

        # Исчерпали попытки на ретраящемся статусе.
        raise last_exc or MarketplaceError("Ozon: превышено число повторов запроса (лимит/сбой)")

    # --- операции ---------------------------------------------------------

    def check_connection(self) -> None:
        """Лёгкий запрос списка товаров — проверяем, что ключи рабочие."""
        self._post("/v3/product/list", {"filter": {}, "limit": 1})

    def sell(self, listing) -> None:
        """Снять лот после продажи — ТОЛЬКО обнулить остаток, БЕЗ архивации.

        Используется при обработке заказов. Карточка остаётся в статусе «Готов к продаже»
        с нулевым остатком, чтобы при отмене заказа её можно было быстро вернуть в продажу
        (просто выставить остаток 1). Архивация не нужна: книга и так не продаётся (остаток 0),
        а при отмене из архива не достать.
        """
        offer_id = listing.external_id
        if not offer_id:
            raise MarketplaceError("У лота Ozon нет offer_id — нечего снимать")
        self.last_warning = None
        self._set_stock(offer_id, 0)

    def withdraw(self, listing) -> None:
        """Снять лот с продажи — обнулить остаток. Архивацию НЕ делаем.

        Раньше здесь была архивация через /v1/product/archive — от неё отказались:
        книги б/у после снятия нужно возвращать в продажу при отмене заказа, а из
        архива карточку не достать. Обнулённый остаток и так снимает книгу с продажи.
        """
        offer_id = listing.external_id
        if not offer_id:
            raise MarketplaceError("У лота Ozon нет offer_id — нечего снимать")
        self.last_warning = None
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
        try:
            warehouse = int(self.warehouse_id)
        except (TypeError, ValueError):
            raise MarketplaceError(
                f"ID склада Ozon должен быть числом, а задано «{self.warehouse_id}»"
            ) from None

        data = self._post(
            "/v2/products/stocks",
            {
                "stocks": [
                    {
                        "offer_id": offer_id,
                        "stock": stock,
                        "warehouse_id": warehouse,
                    }
                ]
            },
        )

        # Ozon отвечает 200 даже когда остаток НЕ обновлён: в result лежит
        # updated=false и причина в errors. Без этой проверки снятие считалось бы
        # успешным, книга осталась бы в продаже, а в журнал ушло бы «Снято».
        for item in data.get("result") or []:
            if item.get("updated"):
                continue
            errors = item.get("errors") or []
            reason = "; ".join(
                str(e.get("message") or e.get("code") or e) for e in errors
            ) or "Ozon не обновил остаток (причина не указана)"
            raise MarketplaceError(f"Ozon не обновил остаток по {offer_id}: {reason}")

    def restore(self, listing) -> None:
        """Вернуть карточку Ozon в продажу после отмены заказа.

        Карточки в архив мы не убираем (снятие — только обнуление остатка),
        поэтому восстановление — просто выставить остаток 1.
        """
        offer_id = listing.external_id
        if not offer_id:
            raise MarketplaceError("У лота Ozon нет offer_id — нечего восстанавливать")
        self.last_warning = None
        self._set_stock(offer_id, 1)

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
                returned_ids: set[str] = set()
                for prod in (info.get("result") or {}).get("items") or info.get("items") or []:
                    offer_id = prod.get("offer_id")
                    if not offer_id:
                        continue
                    returned_ids.add(str(offer_id))
                    price = _first_positive_price(
                        prod.get("marketing_price"), prod.get("price")
                    )
                    barcode = prod.get("barcode")
                    if not barcode:
                        barcodes = prod.get("barcodes") or []
                        barcode = barcodes[0] if barcodes else None
                    rows.append(
                        {
                            "sku": offer_id,
                            "external_id": offer_id,
                            "stock_key": offer_id,
                            "in_sale": True,
                            "title": prod.get("name"),
                            "isbn": barcode,
                            "price": str(price) if price not in (None, "") else None,
                        }
                    )

                # Ozon иногда не возвращает часть offer_id из /info/list (неполный батч).
                # Раньше эти книги тихо терялись — отсюда расхождение счётчика с Ozon.
                # Добавляем их с offer_id как именем: они в продаже (пришли из IN_SALE
                # фильтра), имя подтянется при следующей сверке когда ответ будет полным.
                for oid in offer_ids:
                    if str(oid) not in returned_ids:
                        rows.append(
                            {
                                "sku": oid,
                                "external_id": oid,
                                "stock_key": oid,
                                "in_sale": True,
                                "title": None,   # заполнится при следующей сверке
                                "isbn": None,
                                "price": None,
                            }
                        )

            last_id = result.get("last_id") or ""
            if not last_id:
                break
        return rows

    def fetch_stocks(self, keys: list[str]) -> dict[str, int]:
        """Доступный остаток FBS по offer_id (ключам). Возвращает {offer_id: доступно}.

        Дешёвый способ узнать «книга ещё продаётся?» без выгрузки всего каталога:
        спрашиваем остатки ровно по нашим SKU пачками (Ozon берёт до 1000 за раз).
        Используем /v4/product/info/stocks. Ключей, которых Ozon не знает, в ответе
        просто не будет — вызывающий код трактует отсутствие как «пропала».

        ВАЖНО: считаем ДОСТУПНОЕ = present − reserved. У книги б/у один экземпляр:
        как только её покупают, Ozon резервирует его под заказ (present=1, reserved=1),
        то есть к продаже доступно 0 ещё ДО фактической отгрузки. Если бы мы смотрели
        только present, продажа замечалась бы с задержкой (после отгрузки) — и всё это
        время книга висела бы на другой площадке. Вычитание reserved закрывает это окно.
        """
        result: dict[str, int] = {}
        if not keys:
            return result
        for i in range(0, len(keys), 1000):
            batch = keys[i:i + 1000]
            data = self._post(
                "/v4/product/info/stocks",
                {"filter": {"offer_id": batch, "visibility": "ALL"}, "limit": 1000},
            )
            items = (data.get("result") or {}).get("items") or data.get("items") or []
            for it in items:
                offer_id = it.get("offer_id")
                if not offer_id:
                    continue
                # Остатки приходят по типам схемы (fbs — наш склад, fbo — склад Ozon).
                # Считаем ТОЛЬКО fbs, если он есть: книгами мы торгуем со своего
                # склада, и именно его остаток обнуляем при снятии. Если сложить с
                # fbo, проданная книга не показала бы 0 и не снялась бы с другой
                # площадки. Когда типов нет вовсе — суммируем что дали.
                entries = it.get("stocks") or []
                fbs_entries = [st for st in entries if str(st.get("type") or "").strip().lower() == "fbs"]
                counted = fbs_entries if fbs_entries else entries
                present = sum(int(st.get("present") or 0) for st in counted)
                reserved = sum(int(st.get("reserved") or 0) for st in counted)
                result[str(offer_id)] = max(0, present - reserved)
        return result

    def fetch_in_sale_ids(self, keys: list[str]) -> set[str]:
        """Вернуть подмножество offer_id, которые Ozon всё ещё показывает «В продаже».

        Используем /v4/product/info/stocks с visibility=IN_SALE: в ответ попадают
        только карточки, реально видимые покупателям. Если offer_id в ответе нет —
        карточка уже снята. Именно этой проверкой нужно пользоваться
        для сверки снятых книг: у проданной книги остаток available=0 (reserved=present),
        поэтому fetch_stocks всегда возвращает 0 даже для снятой карточки.
        """
        result: set[str] = set()
        if not keys:
            return result
        for i in range(0, len(keys), 1000):
            batch = keys[i : i + 1000]
            data = self._post(
                "/v4/product/info/stocks",
                {"filter": {"offer_id": batch, "visibility": "IN_SALE"}, "limit": 1000},
            )
            items = (data.get("result") or {}).get("items") or data.get("items") or []
            for it in items:
                offer_id = it.get("offer_id")
                if offer_id:
                    result.add(str(offer_id))
        return result

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
        result: list[OrderInfo] = []
        limit = 100
        offset = 0
        # Пагинация по offset: за 3-дневное окно отправлений может быть больше 100.
        # Без цикла часть продаж не попала бы в кросс-снятие. Стоп — когда страница
        # неполная (постингов меньше лимита) или предохранитель по offset.
        while offset < 10000:
            # Пустой filter.status Ozon может отклонить — поле опускаем целиком,
            # чтобы вернулись отправления во всех статусах.
            data = self._post(
                "/v3/posting/fbs/list",
                {
                    "dir": "DESC",
                    "filter": {
                        "since": since.strftime(fmt),
                        "to": now.strftime(fmt),
                    },
                    "limit": limit,
                    "offset": offset,
                    "with": {},
                },
            )
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
            if len(postings) < limit:
                break
            offset += limit
        return result

    def fetch_cancelled_orders(self) -> list["CancelledOrderInfo"]:
        """Получить отменённые отправления FBS за последние дни.

        Статус 'cancelled' означает, что заказ не будет отгружен. Книга уже
        в пути (already_shipped=True, восстанавливать остаток не нужно), если
        сработал ЛЮБОЙ из признаков отгрузки:

        - в истории статусов был 'awaiting_deliver'/'delivering'/…
          (status_history=true);
        - поле delivering_date непустое — отправление реально уехало в доставку;
        - cancellation.cancelled_after_ship == True — Ozon сам помечает отмену
          «после отгрузки».

        Только истории НЕ достаточно: Ozon часто отдаёт пустую status_history
        для отменённых отправлений, хотя книга уехала (delivering_date при этом
        стоит). Опора только на историю возвращала в продажу книги, которых
        физически нет (баг с «книга в пути, а остатки восстановлены»).
        """
        from datetime import datetime, timedelta, timezone
        from app.marketplaces.base import CancelledOrderInfo

        # Статусы, при которых книга уже физически передана в доставку
        SHIPPED_STATUSES = {"awaiting_deliver", "delivering", "delivered", "driver_pickup"}

        now = datetime.now(timezone.utc)
        since = now - timedelta(days=7)
        fmt = "%Y-%m-%dT%H:%M:%S.000Z"
        result: list[CancelledOrderInfo] = []
        limit = 100
        offset = 0
        while offset < 10000:
            data = self._post(
                "/v3/posting/fbs/list",
                {
                    "dir": "DESC",
                    "filter": {
                        "since": since.strftime(fmt),
                        "to": now.strftime(fmt),
                        "status": "cancelled",
                    },
                    "limit": limit,
                    "offset": offset,
                    # Запрашиваем историю статусов, чтобы понять,
                    # была ли книга передана в доставку до отмены.
                    "with": {"status_history": True},
                },
            )
            postings = (data.get("result") or {}).get("postings") or []
            for posting in postings:
                order_number = posting.get("posting_number") or posting.get("order_number")
                if not order_number:
                    continue
                # Определяем, была ли книга РЕАЛЬНО передана в доставку. Одной
                # истории мало: Ozon отдаёт пустую status_history для многих
                # отменённых, хотя delivering_date стоит.
                #
                # ВАЖНО: cancellation.cancelled_after_ship НЕ считаем признаком
                # отгрузки. Ozon ставит его уже при назначенной ПЛАНОВОЙ дате
                # отгрузки (shipment_date), хотя книга ещё на полке: statшные
                # заказы с cancelled_after_ship=True, пустым delivering_date,
                # пустым трек-номером и пустой историей статусов (проверено
                # на проде 25.08) — книга не уехала, и отмену надо отработать
                # как обычную (вернуть в продажу).
                history = posting.get("status_history") or []
                past_statuses = {h.get("status") for h in history if h.get("status")}
                already_shipped = (
                    bool(past_statuses & SHIPPED_STATUSES)
                    or bool(posting.get("delivering_date"))
                )
                # Отмена продавцом «товар закончился на складе» (cancel_reason_id
                # 352, «Товар закончился на складе»). Книгу не нашли физически —
                # возвращать её в продажу нельзя.
                cancellation = posting.get("cancellation") or {}
                seller_out_of_stock = bool(
                    cancellation.get("cancellation_type") == "seller"
                    and str(cancellation.get("cancel_reason_id")) == "352"
                )
                result.append(CancelledOrderInfo(
                    external_order_id=str(order_number),
                    already_shipped=already_shipped,
                    seller_out_of_stock=seller_out_of_stock,
                ))
            if len(postings) < limit:
                break
            offset += limit
        return result
