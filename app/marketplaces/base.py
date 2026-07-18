"""Базовый интерфейс клиента площадки и общие типы.

Любая площадка (Ozon, WB, Avito) реализует три операции:
- publish  — выставить/обновить лот книги,
- withdraw — снять лот с продажи,
- fetch_orders — получить новые заказы (для авто-снятия проданного).

Возвращаемые типы намеренно простые (dataclass), чтобы вызывающий код —
sync.py и фоновый опрос — не знал деталей конкретной площадки.
"""
from __future__ import annotations

from dataclasses import dataclass, field


class MarketplaceError(Exception):
    """Любая ошибка обращения к площадке. Текст пишем в sync_log и показываем в UI."""


@dataclass
class PublishResult:
    """Результат публикации лота на площадке."""

    external_id: str | None  # ID лота на площадке (нужен для последующего снятия)
    raw: dict = field(default_factory=dict)  # сырой ответ — для отладки


@dataclass
class OrderInfo:
    """Один заказ, полученный с площадки. Нужен, чтобы найти проданную книгу."""

    external_order_id: str
    external_sku: str | None  # артикул/offer_id, по которому свяжем с книгой
    external_id: str | None = None  # ID лота на площадке, если заказ отдаёт его


class MarketplaceClient:
    """Контракт клиента площадки. Наследники обязаны реализовать все методы.

    В конструктор приходят уже расшифрованные ключи (dict из MarketplaceAccount).
    """

    marketplace: str = ""

    def __init__(self, credentials: dict):
        self.credentials = credentials or {}

    def publish(self, book) -> PublishResult:
        """Выставить или обновить лот книги. book — ORM-объект Book."""
        raise NotImplementedError

    def withdraw(self, listing) -> None:
        """Снять лот с продажи. listing — ORM-объект Listing с external_id."""
        raise NotImplementedError

    def fetch_orders(self) -> list[OrderInfo]:
        """Вернуть недавние заказы площадки. Дедупликацию делает вызывающий код."""
        raise NotImplementedError

    def fetch_catalog(self) -> list[dict]:
        """Выгрузить весь каталог товаров площадки для импорта в программу.

        Возвращает список строк-словарей, ключи которых — поля книги
        (sku, title, author, isbn, publisher, price, external_id). Тогда импорт
        применяет к ним тождественное сопоставление и не знает деталей площадки.
        Постраничную прокрутку (курсор/пагинацию) реализует сам клиент.
        """
        raise NotImplementedError

    def check_connection(self) -> None:
        """Проверить, что ключи рабочие. Бросает MarketplaceError при провале."""
        raise NotImplementedError

    def fetch_categories(self, query: str = "") -> list[dict]:
        """Справочник категорий/типов площадки — чтобы подобрать ID для публикации.

        Возвращает список вариантов вида
            {"label": "Книги → Художественная литература", "fields": {ключ: значение}},
        где fields — какие поля настроек подставить при выборе этого варианта
        (для Ozon это description_category_id + type_id, для WB — subject_id).
        query — необязательная строка поиска по названию.
        """
        raise NotImplementedError
