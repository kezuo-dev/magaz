"""Базовый интерфейс клиента площадки и общие типы.

Любая площадка (Ozon, WB) реализует операции:
- withdraw — снять лот с продажи (обнулить остаток),
- fetch_orders — получить новые заказы (для авто-снятия проданного),
- fetch_catalog — выгрузить каталог площадки для сверки,
- check_connection — проверить ключи.

Выставление книг убрано: программа только отслеживает и снимает проданное.

Возвращаемые типы намеренно простые (dataclass), чтобы вызывающий код —
sync.py и фоновый опрос — не знал деталей конкретной площадки.
"""
from __future__ import annotations

from dataclasses import dataclass


class MarketplaceError(Exception):
    """Любая ошибка обращения к площадке. Текст пишем в sync_log и показываем в UI."""


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

    def withdraw(self, listing) -> None:
        """Снять лот с продажи. listing — ORM-объект Listing с external_id."""
        raise NotImplementedError

    def fetch_orders(self) -> list[OrderInfo]:
        """Вернуть недавние заказы площадки. Дедупликацию делает вызывающий код."""
        raise NotImplementedError

    def fetch_stocks(self, keys: list[str]) -> dict[str, int]:
        """Остатки FBS по ключам остатка (stock_key лотов). Возвращает {ключ: остаток}.

        Дешёвая проверка «товар ещё в наличии?» без выгрузки всего каталога —
        спрашиваем площадку ровно по нашим ключам пачками. Ключ, которого площадка
        не знает (карточка удалена), в ответ не попадает — вызывающий код трактует
        его отсутствие как «книга пропала».
        """
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
