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
    PublishResult,
)
from app.photos import public_photo_list

from app.config import settings

BASE_URL = "https://api-seller.ozon.ru"
TIMEOUT = 30.0

# ID обязательных атрибутов книжной категории Ozon (получены из
# /v1/description-category/attribute для типа «Печатная книга»). Часть значений
# у книги б/у одинакова всегда — их подставляем константами, чтобы продавцу не
# приходилось заполнять их вручную для каждого экземпляра.
ATTR_NAME = 4180          # Название (строка)
ATTR_AUTHOR = 4182        # Автор на обложке (строка)
ATTR_ISBN = 4184          # ISBN (строка)
ATTR_TYPE = 8229          # Тип (справочник) — «Печатная книга»
ATTR_BRAND = 85           # Бренд (справочник) — «Нет бренда»
ATTR_MARKING = 23536      # Нужен код маркировки (Boolean) — для книг «Нет»
ATTR_TNVED = 22232        # ТН ВЭД коды ЕАЭС (справочник)
ATTR_DIRECTION = 23273    # Направление/жанр (справочник) — задаётся в карточке

# Значения-константы из справочников Ozon для книг б/у.
VALUE_TYPE_PRINTED = 971445087   # «Печатная книга» в справочнике «Тип»
VALUE_BRAND_NONE = 126745801     # «Нет бренда»
VALUE_TNVED_BOOKS = 971398243    # 4901990000 — прочие книги, брошюры, сброшюрованные


class OzonClient(MarketplaceClient):
    marketplace = "ozon"

    def __init__(self, credentials: dict):
        super().__init__(credentials)
        self.client_id = str(credentials.get("client_id", "")).strip()
        self.api_key = str(credentials.get("api_key", "")).strip()
        if not self.client_id or not self.api_key:
            raise MarketplaceError("Не заданы Client-Id и Api-Key для Ozon")
        # Категория и тип товара обязательны для создания карточки. Для магазина
        # б/у книг это одни и те же значения на все товары — задаются в настройках.
        self.description_category_id = str(credentials.get("description_category_id", "")).strip()
        self.type_id = str(credentials.get("type_id", "")).strip()
        # Склад FBS («Мои склады»). Остатки на Ozon выставляются через
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

    def fetch_categories(self, query: str = "") -> list[dict]:
        """Дерево категорий Ozon → плоский список вариантов «категория → тип».

        Ozon отдаёт дерево (/v1/description-category/tree): узлы категорий имеют
        description_category_id и вложенные children; на листьях лежат типы
        товара с type_id/type_name. Карточку можно создать только с конкретным
        типом, поэтому разворачиваем дерево в пары (категория, тип).
        """
        data = self._post("/v1/description-category/tree", {})
        tree = data.get("result") or []
        query = (query or "").strip().lower()
        out: list[dict] = []

        def walk(nodes: list, trail: list[str], category_id):
            for node in nodes or []:
                cat_id = node.get("description_category_id")
                cat_name = node.get("category_name") or ""
                type_id = node.get("type_id")
                type_name = node.get("type_name") or ""
                children = node.get("children") or []

                if type_id:
                    # Лист-тип: категория берётся из ближайшего родителя.
                    label = " → ".join([*trail, type_name]) if type_name else " → ".join(trail)
                    if not query or query in label.lower():
                        out.append(
                            {
                                "label": label,
                                "fields": {
                                    "description_category_id": str(category_id or cat_id or ""),
                                    "type_id": str(type_id),
                                },
                            }
                        )
                if children:
                    # Узел-категория: углубляемся, запоминая её id и имя в цепочке.
                    walk(children, [*trail, cat_name] if cat_name else trail, cat_id or category_id)

        walk(tree, [], None)
        # Сначала книжные категории — их проще найти в длинном списке.
        out.sort(key=lambda o: (0 if "книг" in o["label"].lower() else 1, o["label"]))
        return out

    def publish(self, book) -> PublishResult:
        """Создать/обновить карточку товара и выставить остаток.

        Ozon импортирует товары асинхронно: /v3/product/import ставит задачу и
        возвращает task_id. Пока задача не завершится, товара ещё нет — попытка
        сразу выставить остаток даёт «товар не создан». Поэтому ждём завершения
        импорта, а затем выставляем остаток на склад FBS. offer_id (наш SKU)
        служит внешним идентификатором лота.
        """
        offer_id = book.sku
        price = str(book.price) if book.price is not None else "0"
        quantity = int(getattr(book, "quantity", 1) or 1)

        # Категория и тип теперь задаются у самой книги (подбираются в карточке).
        # На старых настройках ещё могут лежать значения в ключах площадки —
        # используем их как запасной вариант, чтобы ничего не сломать.
        category_id = str(getattr(book, "ozon_category_id", "") or "").strip() or self.description_category_id
        type_id = str(getattr(book, "ozon_type_id", "") or "").strip() or self.type_id

        # Без категории и типа Ozon отклоняет импорт (Request.Items.TypeId > 0).
        if not category_id or not type_id:
            raise MarketplaceError(
                "Для публикации на Ozon укажите в карточке книги категорию и тип товара"
            )

        # Габариты и вес: у книги свои или дефолт из настроек. Без них Ozon
        # отклоняет карточку («Размеры и вес не заполнены»).
        weight = book.weight_grams or settings.default_weight_grams
        length = book.length_mm or settings.default_length_mm
        width = book.width_mm or settings.default_width_mm
        height = book.height_mm or settings.default_height_mm

        item = {
            "offer_id": offer_id,
            "name": book.title,
            "price": price,
            "currency_code": "RUB",
            "description_category_id": int(category_id),
            "type_id": int(type_id),
            # Габариты (мм) и вес (г) с единицами измерения.
            "depth": int(length),
            "width": int(width),
            "height": int(height),
            "dimension_unit": "mm",
            "weight": int(weight),
            "weight_unit": "g",
            "attributes": self._build_attributes(book),
        }
        # Штрих-код автоматически не проставляем: Ozon сам присвоит товару свой
        # штрих-код при создании карточки. ISBN остаётся в атрибутах книги.
        # Ozon скачивает фото по ссылке, поэтому нужен абсолютный URL с хостом.
        images = public_photo_list(book)
        if images:
            item["images"] = images
        if book.description:
            item["description"] = book.description

        data = self._post("/v3/product/import", {"items": [item]})

        # Дожидаемся, пока Ozon создаст товар из задачи импорта, иначе остаток
        # выставить нельзя («товар не создан»).
        task_id = (data.get("result") or {}).get("task_id")
        self._wait_import(task_id)

        # Выставляем остаток на склад FBS.
        self._set_stock(offer_id, quantity)

        return PublishResult(external_id=offer_id, raw={"offer_id": offer_id})

    def _wait_import(self, task_id, attempts: int = 40, delay: float = 2.0) -> None:
        """Опрашивать статус задачи импорта, пока товар не будет создан.

        /v1/product/import/info по task_id возвращает статус каждой позиции.
        Ждём статус imported. Если Ozon вернул ошибки по товару — поднимаем их,
        чтобы причина попала в журнал (иначе остаток выставить всё равно нельзя).

        Импорт Ozon бывает небыстрым (до минуты и дольше), поэтому ждём с запасом
        (attempts × delay ≈ 80 c). Если так и не дождались — кладём в ошибку
        последний статус и все замечания Ozon, чтобы была видна реальная причина,
        а не общая фраза.
        """
        if not task_id:
            return
        import time

        last_statuses: list = []
        last_errors: list[str] = []
        for _ in range(attempts):
            info = self._post("/v1/product/import/info", {"task_id": task_id})
            items = (info.get("result") or {}).get("items") or []
            last_statuses = [it.get("status") for it in items]

            # Собираем замечания Ozon по всем позициям (не только failed): он
            # иногда прикладывает причины ещё до перевода позиции в failed.
            last_errors = []
            for it in items:
                for err in it.get("errors") or []:
                    msg = err.get("message") or err.get("code") or ""
                    if msg and msg not in last_errors:
                        last_errors.append(msg)

            if items and all(s == "imported" for s in last_statuses):
                return
            # Ozon помечает неудачные позиции статусом failed — это отказ, не ждём.
            if any(s == "failed" for s in last_statuses):
                detail = "; ".join(last_errors) or "Ozon отклонил карточку при импорте"
                raise MarketplaceError(f"Импорт Ozon не прошёл: {detail}")
            time.sleep(delay)

        # Не дождались за отведённое время. Показываем, на чём застряло.
        status_text = ", ".join(str(s) for s in last_statuses) or "неизвестно"
        detail = "; ".join(last_errors)
        msg = (
            f"Ozon не подтвердил создание карточки за отведённое время "
            f"(статус: {status_text})."
        )
        if detail:
            msg += f" Замечания Ozon: {detail}."
        else:
            msg += " Проверьте товар в ЛК и выставьте повторно."
        raise MarketplaceError(msg)

    def _build_attributes(self, book) -> list[dict]:
        """Собрать массив обязательных атрибутов книжной карточки Ozon.

        Формат Ozon: [{"id": attr_id, "values": [{"value": "..."} или
        {"dictionary_value_id": N}]}]. Строковые атрибуты передаём value,
        справочные — dictionary_value_id. Направление (жанр) обязательно и
        задаётся в карточке книги; без него Ozon отклонит товар.
        """
        isbn = book.isbn if (book.isbn and book.isbn != "нет") else ""
        attrs: list[dict] = [
            {"id": ATTR_NAME, "values": [{"value": book.title or ""}]},
            {"id": ATTR_AUTHOR, "values": [{"value": book.author or "Не указан"}]},
            {"id": ATTR_TYPE, "values": [{"dictionary_value_id": VALUE_TYPE_PRINTED}]},
            {"id": ATTR_BRAND, "values": [{"dictionary_value_id": VALUE_BRAND_NONE}]},
            {"id": ATTR_TNVED, "values": [{"dictionary_value_id": VALUE_TNVED_BOOKS}]},
            # Код маркировки для книг не нужен — «Нет».
            {"id": ATTR_MARKING, "values": [{"value": "Нет"}]},
        ]
        if isbn:
            attrs.append({"id": ATTR_ISBN, "values": [{"value": isbn}]})
        # Жанр: если выбран в карточке — отдаём его id из справочника Ozon.
        direction_id = str(getattr(book, "ozon_direction_id", "") or "").strip()
        if direction_id:
            attrs.append(
                {"id": ATTR_DIRECTION, "values": [{"dictionary_value_id": int(direction_id)}]}
            )
        return attrs

    def fetch_directions(self, query: str = "") -> list[dict]:
        """Полный справочник значений атрибута «Направление» (жанр) книги.

        Возвращает список {"id": str, "name": str}. Тянем весь справочник
        постранично (по last_value_id) и отдаём целиком — фильтрацию по вводу
        делает браузер. Поисковый эндпоинт Ozon капризен к регистру и совпадению
        с начала строки, поэтому на него не полагаемся: жанров немного.
        Категория/тип — из ключей площадки или книжные по умолчанию.
        """
        category_id = int(self.description_category_id or 200001483)
        type_id = int(self.type_id or VALUE_TYPE_PRINTED)
        out: list[dict] = []
        last_value_id = 0
        # Ограничиваем число страниц на всякий случай, чтобы не зациклиться.
        for _ in range(20):
            data = self._post(
                "/v1/description-category/attribute/values",
                {
                    "description_category_id": category_id,
                    "type_id": type_id,
                    "attribute_id": ATTR_DIRECTION,
                    "limit": 100,
                    "last_value_id": last_value_id,
                },
            )
            values = data.get("result") or []
            if not values:
                break
            for v in values:
                vid = v.get("id")
                name = v.get("value")
                if vid and name:
                    out.append({"id": str(vid), "name": name})
                    last_value_id = vid
            # has_next=false или страница неполная — значит, это конец справочника.
            if not data.get("has_next") or len(values) < 100:
                break
        out.sort(key=lambda d: d["name"].lower())
        return out

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
