"""Страница аналитики: продажи, заказы."""
from __future__ import annotations

from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Book, BookStatus, Order, utcnow
from app.templating import marketplace_label, templates

router = APIRouter(prefix="/analytics")

_MSK = timezone(timedelta(hours=3))


def _day_series(db: Session, days: int, marketplace: str | None = None) -> list[dict]:
    """Возвращает список {date, label, count} за последние `days` дней."""
    now = utcnow()
    cutoff = now - timedelta(days=days - 1)

    q = select(Order.created_at).where(
        Order.created_at >= cutoff,
        Order.cancelled == False,  # noqa: E712
    )
    if marketplace:
        q = q.where(Order.marketplace == marketplace)

    raw = db.scalars(q).all()

    chart: dict[str, int] = {}
    for dt in raw:
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        key = dt.astimezone(_MSK).strftime("%Y-%m-%d")
        chart[key] = chart.get(key, 0) + 1

    series = []
    for i in range(days):
        day = now - timedelta(days=days - 1 - i)
        key = day.astimezone(_MSK).strftime("%Y-%m-%d")
        series.append({
            "date": key,
            "label": day.astimezone(_MSK).strftime("%d.%m"),
            "count": chart.get(key, 0),
        })
    return series


def _build_stats(db: Session) -> dict:
    now = utcnow()

    # ---------- Каталог ----------
    total_books = db.scalar(select(func.count(Book.id))) or 0
    in_stock = db.scalar(select(func.count(Book.id)).where(Book.status == BookStatus.IN_STOCK)) or 0
    sold_total = db.scalar(select(func.count(Book.id)).where(Book.status == BookStatus.SOLD)) or 0
    withdrawn_total = db.scalar(select(func.count(Book.id)).where(Book.status == BookStatus.WITHDRAWN)) or 0

    # ---------- Заказы за периоды ----------
    def _orders(days: int) -> int:
        cutoff = now - timedelta(days=days)
        return db.scalar(
            select(func.count(Order.id)).where(
                Order.created_at >= cutoff, Order.cancelled == False  # noqa: E712
            )
        ) or 0

    def _cancels(days: int) -> int:
        cutoff = now - timedelta(days=days)
        return db.scalar(
            select(func.count(Order.id)).where(
                Order.created_at >= cutoff, Order.cancelled == True  # noqa: E712
            )
        ) or 0

    orders_7d = _orders(7)
    orders_30d = _orders(30)
    orders_90d = _orders(90)
    cancellations_7d = _cancels(7)
    cancellations_30d = _cancels(30)
    cancellations_90d = _cancels(90)

    # ---------- Серии для графиков (90 дней) ----------
    total_series = _day_series(db, 90)
    ozon_series = _day_series(db, 90, "ozon")
    wb_series = _day_series(db, 90, "wildberries")

    # ---------- Последние 15 продаж ----------
    recent_orders = db.execute(
        select(Order, Book)
        .join(Book, Book.id == Order.book_id, isouter=True)
        .where(Order.cancelled == False, Order.book_id.is_not(None))  # noqa: E712
        .order_by(Order.created_at.desc())
        .limit(15)
    ).all()

    recent_sales = []
    for order, book in recent_orders:
        if book:
            recent_sales.append({
                "title": book.title,
                "author": book.author or "",
                "marketplace": marketplace_label(order.marketplace),
                "mp_key": order.marketplace,
                "price": float(book.price) if book.price else None,
                "created_at": order.created_at,
                "book_id": book.id,
            })

    return {
        "total_books": total_books,
        "in_stock": in_stock,
        "sold_total": sold_total,
        "withdrawn_total": withdrawn_total,
        "orders_7d": orders_7d,
        "orders_30d": orders_30d,
        "orders_90d": orders_90d,
        "cancellations_7d": cancellations_7d,
        "cancellations_30d": cancellations_30d,
        "cancellations_90d": cancellations_90d,
        "total_series": total_series,
        "ozon_series": ozon_series,
        "wb_series": wb_series,
        "recent_sales": recent_sales,
    }


@router.get("", response_class=HTMLResponse)
def analytics_page(request: Request):
    return templates.TemplateResponse(request, "analytics_stub.html", {"error": None})


@router.post("", response_class=HTMLResponse)
def analytics_unlock(request: Request, password: str = Form(...), db: Session = Depends(get_db)):
    from app.config import settings
    if password == settings.analytics_password:
        stats = _build_stats(db)
        return templates.TemplateResponse(request, "analytics.html", {"stats": stats})
    return templates.TemplateResponse(
        request, "analytics_stub.html", {"error": "Неверный пароль"}
    )
