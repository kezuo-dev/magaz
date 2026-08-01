"""Страница аналитики: продажи, заказы, здоровье синхронизации."""
from __future__ import annotations

from datetime import timedelta, timezone

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Book, BookStatus, Order, SyncLog, utcnow
from app.templating import marketplace_label, templates

router = APIRouter(prefix="/analytics")


def _build_stats(db: Session) -> dict:
    now = utcnow()
    tz = timezone.utc

    # ---------- Каталог ----------
    total_books = db.scalar(select(func.count(Book.id))) or 0
    in_stock = db.scalar(select(func.count(Book.id)).where(Book.status == BookStatus.IN_STOCK)) or 0
    sold_total = db.scalar(select(func.count(Book.id)).where(Book.status == BookStatus.SOLD)) or 0
    withdrawn_total = db.scalar(select(func.count(Book.id)).where(Book.status == BookStatus.WITHDRAWN)) or 0

    # ---------- Заказы ----------
    total_orders = db.scalar(select(func.count(Order.id))) or 0
    cancelled_orders = db.scalar(select(func.count(Order.id)).where(Order.cancelled == True)) or 0  # noqa: E712
    active_orders = db.scalar(
        select(func.count(Order.id)).where(Order.cancelled == False, Order.processed == True)  # noqa: E712
    ) or 0

    # ---------- 30 дней ----------
    cutoff_30 = now - timedelta(days=30)
    cutoff_7 = now - timedelta(days=7)

    orders_30d = db.scalar(
        select(func.count(Order.id)).where(Order.created_at >= cutoff_30, Order.cancelled == False)  # noqa: E712
    ) or 0
    orders_7d = db.scalar(
        select(func.count(Order.id)).where(Order.created_at >= cutoff_7, Order.cancelled == False)  # noqa: E712
    ) or 0
    cancellations_30d = db.scalar(
        select(func.count(Order.id)).where(Order.created_at >= cutoff_30, Order.cancelled == True)  # noqa: E712
    ) or 0

    # ---------- Разбивка по площадкам (все не-отменённые заказы) ----------
    mp_rows = db.execute(
        select(Order.marketplace, func.count(Order.id))
        .where(Order.cancelled == False)  # noqa: E712
        .group_by(Order.marketplace)
    ).all()
    mp_sales: dict[str, int] = {row[0]: row[1] for row in mp_rows}
    mp_sales_total = sum(mp_sales.values()) or 1  # защита от деления на 0

    # ---------- График: продажи по дням за 30 дней ----------
    # Собираем кол-во заказов на каждый день. База хранит UTC, делаем +3.
    day_rows = db.execute(
        select(
            func.date(func.datetime(Order.created_at, "+3 hours")),
            func.count(Order.id),
        )
        .where(Order.created_at >= cutoff_30, Order.cancelled == False)  # noqa: E712
        .group_by(func.date(func.datetime(Order.created_at, "+3 hours")))
        .order_by(func.date(func.datetime(Order.created_at, "+3 hours")))
    ).all()
    chart_data: dict[str, int] = {row[0]: row[1] for row in day_rows}

    # Заполняем нули для дней без заказов
    days_series: list[dict] = []
    for i in range(30):
        day = now - timedelta(days=29 - i)
        key = day.strftime("%Y-%m-%d")
        days_series.append({"date": key, "label": day.strftime("%d.%m"), "count": chart_data.get(key, 0)})

    # ---------- Последние 10 продаж ----------
    recent_orders = db.execute(
        select(Order, Book)
        .join(Book, Book.id == Order.book_id, isouter=True)
        .where(Order.cancelled == False, Order.book_id.is_not(None))  # noqa: E712
        .order_by(Order.created_at.desc())
        .limit(10)
    ).all()

    recent_sales = []
    for order, book in recent_orders:
        if book:
            recent_sales.append({
                "sku": book.sku,
                "title": book.title,
                "author": book.author or "",
                "marketplace": marketplace_label(order.marketplace),
                "mp_key": order.marketplace,
                "price": float(book.price) if book.price else None,
                "created_at": order.created_at,
                "book_id": book.id,
            })

    # ---------- Топ авторов по продажам ----------
    author_rows = db.execute(
        select(Book.author, func.count(Order.id).label("sales"))
        .join(Order, Order.book_id == Book.id)
        .where(Order.cancelled == False, Book.author.is_not(None))  # noqa: E712
        .group_by(Book.author)
        .order_by(func.count(Order.id).desc())
        .limit(8)
    ).all()
    top_authors = [{"author": row[0], "sales": row[1]} for row in author_rows]
    max_author_sales = top_authors[0]["sales"] if top_authors else 1

    # ---------- Здоровье синхронизации (ошибки за 24ч) ----------
    cutoff_24h = now - timedelta(hours=24)
    errors_24h = db.scalar(
        select(func.count(SyncLog.id)).where(SyncLog.created_at >= cutoff_24h, SyncLog.ok == False)  # noqa: E712
    ) or 0
    total_24h = db.scalar(
        select(func.count(SyncLog.id)).where(SyncLog.created_at >= cutoff_24h)
    ) or 0
    health_pct = round(100 * (total_24h - errors_24h) / total_24h) if total_24h else 100

    # Последняя ошибка синхронизации
    last_error = db.scalar(
        select(SyncLog).where(SyncLog.ok == False).order_by(SyncLog.created_at.desc()).limit(1)  # noqa: E712
    )

    return {
        "total_books": total_books,
        "in_stock": in_stock,
        "sold_total": sold_total,
        "withdrawn_total": withdrawn_total,
        "total_orders": total_orders,
        "active_orders": active_orders,
        "cancelled_orders": cancelled_orders,
        "orders_30d": orders_30d,
        "orders_7d": orders_7d,
        "cancellations_30d": cancellations_30d,
        "mp_sales": mp_sales,
        "mp_sales_total": mp_sales_total,
        "days_series": days_series,
        "recent_sales": recent_sales,
        "top_authors": top_authors,
        "max_author_sales": max_author_sales,
        "errors_24h": errors_24h,
        "total_24h": total_24h,
        "health_pct": health_pct,
        "last_error": last_error,
    }


@router.get("", response_class=HTMLResponse)
def analytics_page(request: Request, db: Session = Depends(get_db)):
    stats = _build_stats(db)
    return templates.TemplateResponse(request, "analytics.html", {"stats": stats})


@router.get("/api")
def analytics_api(db: Session = Depends(get_db)):
    """JSON-снимок для будущего авто-обновления."""
    stats = _build_stats(db)
    return JSONResponse({
        "orders_7d": stats["orders_7d"],
        "orders_30d": stats["orders_30d"],
        "in_stock": stats["in_stock"],
        "health_pct": stats["health_pct"],
    })
