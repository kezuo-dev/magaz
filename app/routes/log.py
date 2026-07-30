"""Журнал синхронизации: последние действия с площадками и их результат.

Критично для разбора ошибок на объёме 50k книг — видно, что, куда, когда ушло
и чем закончилось.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Book, Marketplace, SyncLog
from app.templating import marketplace_label, templates, _to_msk

router = APIRouter(prefix="/log")

PAGE_SIZE = 100


@router.get("", response_class=HTMLResponse)
def log_page(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    marketplace: str = "",
    only_errors: str = "",
):
    stmt = select(SyncLog)
    if q:
        # Поиск по сообщению или артикулу (артикулы часто попадают в message)
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{needle}%"
        stmt = stmt.where(SyncLog.message.ilike(like, escape="\\"))
    if marketplace:
        stmt = stmt.where(SyncLog.marketplace == marketplace)
    if only_errors:
        stmt = stmt.where(SyncLog.ok == False)  # noqa: E712

    entries = db.scalars(
        stmt.order_by(SyncLog.created_at.desc()).limit(PAGE_SIZE)
    ).all()

    return templates.TemplateResponse(
        request,
        "log.html",
        {
            "entries": entries,
            "marketplaces": list(Marketplace),
            "q": q,
            "marketplace": marketplace,
            "only_errors": only_errors,
        },
    )


@router.get("/api")
def api_log(
    db: Session = Depends(get_db),
    q: str = "",
    marketplace: str = "",
    only_errors: str = "",
):
    """JSON-фрагмент журнала для живого поиска."""
    stmt = select(SyncLog)
    if q:
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{needle}%"
        stmt = stmt.where(SyncLog.message.ilike(like, escape="\\"))
    if marketplace:
        stmt = stmt.where(SyncLog.marketplace == marketplace)
    if only_errors:
        stmt = stmt.where(SyncLog.ok == False)  # noqa: E712

    entries = db.scalars(
        stmt.order_by(SyncLog.created_at.desc()).limit(PAGE_SIZE)
    ).all()

    items = []
    for e in entries:
        items.append(
            {
                "created_at": _to_msk(e.created_at),
                "marketplace": marketplace_label(e.marketplace) if e.marketplace else "—",
                "action": e.action,
                "ok": e.ok,
                "message": e.message or "",
            }
        )
    return JSONResponse({"items": items})


@router.get("/by-book", response_class=HTMLResponse)
def log_by_book(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    marketplace: str = "",
    only_errors: str = "",
):
    """Журнал, сгруппированный по книгам (book_id). Показывает все записи для каждой книги."""
    stmt = select(SyncLog).where(SyncLog.book_id.is_not(None))

    if q:
        # Поиск по артикулу — джойним Book
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{needle}%"
        stmt = stmt.join(Book, Book.id == SyncLog.book_id).where(
            Book.sku.ilike(like, escape="\\")
        )
    if marketplace:
        stmt = stmt.where(SyncLog.marketplace == marketplace)
    if only_errors:
        stmt = stmt.where(SyncLog.ok == False)  # noqa: E712

    # Берём последние 500 записей с book_id, сортируем по времени
    entries = db.scalars(
        stmt.order_by(SyncLog.created_at.desc()).limit(500)
    ).all()

    # Группируем по book_id
    from collections import defaultdict
    groups_dict = defaultdict(list)
    for e in entries:
        groups_dict[e.book_id].append(e)

    # Получаем информацию о книгах
    book_ids = list(groups_dict.keys())
    books = {b.id: b for b in db.scalars(select(Book).where(Book.id.in_(book_ids))).all()}

    # Формируем список групп для шаблона
    groups = []
    for book_id, log_entries in groups_dict.items():
        book = books.get(book_id)
        if not book:
            continue
        groups.append({
            "sku": book.sku,
            "title": book.title,
            "entries": sorted(log_entries, key=lambda e: e.created_at, reverse=True),
        })

    # Сортируем группы по последней записи (свежие книги сверху)
    groups.sort(key=lambda g: g["entries"][0].created_at, reverse=True)

    return templates.TemplateResponse(
        request,
        "log_by_book.html",
        {
            "groups": groups,
            "marketplaces": list(Marketplace),
            "q": q,
            "marketplace": marketplace,
            "only_errors": only_errors,
        },
    )
