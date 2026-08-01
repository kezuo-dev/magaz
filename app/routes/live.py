"""Живые обновления страниц без перезагрузки.

Отдаёт компактный JSON по опросу (polling) с фронта. Держим отдельным роутером,
чтобы страницы оставались обычными server-rendered, а живость была надстройкой:
если JS отключён или эндпоинт упал — страница работает как раньше.

Каталог и журнал открыты всем вошедшим (как и их HTML-страницы). Аналитика —
только после ввода пароля раздела (флаг в сессии), иначе цифры утекали бы через
JSON в обход формы.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Book, BookStatus, Listing, ListingStatus, Marketplace, SyncLog
from app.templating import _to_msk, marketplace_label

router = APIRouter(prefix="/api/live")

# Сколько свежих записей журнала отдаём максимум за один опрос. Ограничение
# защищает от гигантского ответа, если сверка каталога залила тысячи строк.
LOG_BURST = 40


@router.get("/catalog")
def live_catalog(db: Session = Depends(get_db)):
    """Счётчики каталога + отпечаток состояния для подсветки изменений."""
    in_stock = db.scalar(
        select(func.count(Book.id)).where(Book.status == BookStatus.IN_STOCK)
    ) or 0

    def _on_marketplace(mp: str) -> int:
        return db.scalar(
            select(func.count(func.distinct(Listing.book_id))).where(
                Listing.marketplace == mp,
                Listing.status == ListingStatus.ACTIVE,
            )
        ) or 0

    total_books = db.scalar(select(func.count(Book.id))) or 0
    sold = db.scalar(
        select(func.count(Book.id)).where(Book.status == BookStatus.SOLD)
    ) or 0

    return JSONResponse({
        "in_stock": in_stock,
        "on_ozon": _on_marketplace(Marketplace.OZON.value),
        "on_wb": _on_marketplace(Marketplace.WILDBERRIES.value),
        "total": total_books,
        "sold": sold,
    })


@router.get("/log")
def live_log(
    db: Session = Depends(get_db),
    after_id: int = 0,
    marketplace: str = "",
    only_errors: str = "",
):
    """Записи журнала новее `after_id` — по возрастанию id.

    Фронт передаёт id самой свежей строки, которую уже показал, и получает
    только новое. Фильтры повторяют /log, чтобы живая дописка не тащила
    записи, отфильтрованные пользователем.
    """
    stmt = select(SyncLog).where(SyncLog.id > after_id)
    if marketplace:
        stmt = stmt.where(SyncLog.marketplace == marketplace)
    if only_errors:
        stmt = stmt.where(SyncLog.ok == False)  # noqa: E712

    # Берём свежие (desc), затем разворачиваем: при большом отставании нужны
    # последние LOG_BURST, а не первые после after_id.
    entries = db.scalars(
        stmt.order_by(SyncLog.id.desc()).limit(LOG_BURST)
    ).all()
    entries = list(reversed(entries))

    latest_id = db.scalar(select(func.max(SyncLog.id))) or 0

    items = [
        {
            "id": e.id,
            "created_at": _to_msk(e.created_at),
            "marketplace": marketplace_label(e.marketplace) if e.marketplace else "—",
            "action": e.action,
            "ok": e.ok,
            "message": e.message or "",
        }
        for e in entries
    ]
    return JSONResponse({"items": items, "latest_id": latest_id})


@router.get("/analytics")
def live_analytics(request: Request, db: Session = Depends(get_db)):
    """Полная статистика аналитики. Требует разблокировки раздела паролем."""
    if not request.session.get("analytics_unlocked"):
        return JSONResponse({"error": "locked"}, status_code=403)

    from app.routes.analytics import _build_stats

    stats = _build_stats(db)
    # recent_sales содержит datetime — приводим к строке МСК для JSON.
    stats["recent_sales"] = [
        {**s, "created_at": _to_msk(s["created_at"])} for s in stats["recent_sales"]
    ]
    return JSONResponse(stats)
