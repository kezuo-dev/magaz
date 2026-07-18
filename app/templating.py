"""Общий объект шаблонов Jinja2 — чтобы не создавать его в каждом роуте."""
from fastapi.templating import Jinja2Templates

from app.config import BASE_DIR

templates = Jinja2Templates(directory=BASE_DIR / "app" / "templates")

# Русские подписи статусов для интерфейса. Ключи совпадают со значениями enum.
BOOK_STATUS_LABELS = {
    "draft": "Черновик",
    "in_stock": "В наличии",
    "sold": "Продана",
    "withdrawn": "Снята",
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
    "avito": "Avito",
}


def book_status_label(value: str) -> str:
    return BOOK_STATUS_LABELS.get(value, value)


def listing_status_label(value: str) -> str:
    return LISTING_STATUS_LABELS.get(value, value)


def marketplace_label(value: str) -> str:
    return MARKETPLACE_LABELS.get(value, value)


# Делаем доступными во всех шаблонах.
templates.env.globals["book_status_label"] = book_status_label
templates.env.globals["listing_status_label"] = listing_status_label
templates.env.globals["marketplace_label"] = marketplace_label
