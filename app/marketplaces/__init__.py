"""Клиенты площадок. Каждая площадка реализует один интерфейс MarketplaceClient.

Фабрика get_client() отдаёт нужный клиент по названию площадки и расшифрованным
ключам. Наружу (в sync.py и планировщик) уходит только базовый интерфейс —
поэтому подключение новых площадок не требует правок в вызывающем коде.
"""
from app.marketplaces.base import (
    MarketplaceClient,
    MarketplaceError,
    OrderInfo,
)
from app.marketplaces.ozon import OzonClient
from app.marketplaces.wildberries import WBClient

# Реестр реализованных клиентов площадок.
_CLIENTS: dict[str, type[MarketplaceClient]] = {
    "ozon": OzonClient,
    "wildberries": WBClient,
}


def get_client(marketplace: str, credentials: dict) -> MarketplaceClient:
    """Собрать клиент площадки. Бросает MarketplaceError, если площадка ещё не поддержана."""
    cls = _CLIENTS.get(marketplace)
    if cls is None:
        raise MarketplaceError(f"Площадка «{marketplace}» пока не поддерживается")
    return cls(credentials)


def is_supported(marketplace: str) -> bool:
    return marketplace in _CLIENTS


__all__ = [
    "MarketplaceClient",
    "MarketplaceError",
    "OrderInfo",
    "OzonClient",
    "WBClient",
    "get_client",
    "is_supported",
]
