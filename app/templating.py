"""Общий объект шаблонов Jinja2 — чтобы не создавать его в каждом роуте."""
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR

templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

# Русские подписи статусов для интерфейса. Ключи совпадают со значениями enum.
# «draft» больше не используется, но подпись оставлена: в старых базах могли
# остаться такие записи до миграции (ensure_schema переводит их в in_stock).
BOOK_STATUS_LABELS = {
    "in_stock": "В продаже",
    "sold": "Продана",
    "withdrawn": "Снята",
    "draft": "В продаже",
}

# Пояснения к статусам — показываем подсказкой, чтобы логика была очевидна.
BOOK_STATUS_HINTS = {
    "in_stock": "Продаётся хотя бы на одной площадке",
    "sold": "Ушла с продажи, был заказ",
    "withdrawn": "Ушла с продажи без заказа (снята вручную или карточки нет)",
    "draft": "Продаётся хотя бы на одной площадке",
}

LISTING_STATUS_LABELS = {
    "pending": "Ожидает",
    "active": "Активно",
    "withdrawing": "Снимается",
    "withdrawn": "Снято",
    "error": "Ошибка",
}


MARKETPLACE_LABELS = {
    "ozon": "Ozon",
    "wildberries": "Wildberries",
}

# Короткие метки для компактных индикаторов площадок (WI из wildberries[:2] — баг).
MARKETPLACE_SHORT = {
    "ozon": "OZ",
    "wildberries": "WB",
}


def book_status_label(value: str) -> str:
    return BOOK_STATUS_LABELS.get(value, value)


def book_status_hint(value: str) -> str:
    return BOOK_STATUS_HINTS.get(value, "")


def book_status_css(value: str) -> str:
    """Класс бейджа. Старый draft показываем как «в продаже»."""
    return "in_stock" if value == "draft" else value


def listing_status_label(value: str) -> str:
    return LISTING_STATUS_LABELS.get(value, value)


def marketplace_label(value: str) -> str:
    return MARKETPLACE_LABELS.get(value, value)


def marketplace_short(value: str) -> str:
    return MARKETPLACE_SHORT.get(value, (value or "")[:2].upper())


# Порядок площадок в индикаторах: всегда OZ, потом WB — чтобы столбец читался
# ровно, а не вразброс (порядок лотов в базе зависит от того, какая сверка прошла
# первой).
MARKETPLACE_ORDER = {"ozon": 0, "wildberries": 1}


def sort_listings(listings) -> list:
    """Лоты в стабильном порядке площадок (Ozon → Wildberries → прочие)."""
    return sorted(listings, key=lambda l: (MARKETPLACE_ORDER.get(l.marketplace, 99), l.marketplace))


# Делаем доступными во всех шаблонах.
templates.env.globals["book_status_hint"] = book_status_hint
templates.env.globals["book_status_css"] = book_status_css
templates.env.globals["book_status_label"] = book_status_label
templates.env.globals["listing_status_label"] = listing_status_label
templates.env.globals["marketplace_label"] = marketplace_label
templates.env.globals["marketplace_short"] = marketplace_short
templates.env.globals["sort_listings"] = sort_listings
