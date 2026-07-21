"""Каталог книг — чистый мониторинг того, что реально на площадках.

Программа ничего не выставляет и не редактирует: каталог наполняется сверкой
(см. app/catalog_sync.py и /import), продажи ловятся опросом заказов и слежением
за остатками. Здесь только просмотр: список с поиском/фильтрами, карточка книги
(read-only) и разрушительная очистка локальной базы.
"""
import shutil

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.db import get_db
from app.models import (
    Book,
    BookStatus,
    Listing,
    Marketplace,
    Order,
    SyncLog,
)
from app.photos import UPLOAD_DIR
from app.templating import book_status_label, listing_status_label, marketplace_short
from app.templating import templates

router = APIRouter()

PAGE_SIZE = 50


def _filtered_books_query(q: str, status: str, marketplace: str):
    """Собрать запрос списка книг по поиску/фильтрам (общий для страницы и API)."""
    stmt = select(Book).options(selectinload(Book.listings))
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                Book.title.ilike(like),
                Book.sku.ilike(like),
                Book.isbn.ilike(like),
            )
        )
    if status:
        stmt = stmt.where(Book.status == status)
    # Фильтр по площадке: оставляем книги, у которых есть лот на этой площадке.
    if marketplace:
        stmt = stmt.where(Book.listings.any(Listing.marketplace == marketplace))
    return stmt


def _catalog_stats(db: Session) -> dict:
    """Сводка для карточек-счётчиков наверху каталога."""
    total = db.scalar(select(func.count()).select_from(Book)) or 0
    in_stock = db.scalar(
        select(func.count()).select_from(Book).where(Book.status == BookStatus.IN_STOCK)
    ) or 0
    # Проданные и снятые считаем вместе как «ушли с продажи».
    gone = db.scalar(
        select(func.count()).select_from(Book).where(
            Book.status.in_([BookStatus.SOLD, BookStatus.WITHDRAWN])
        )
    ) or 0
    on_ozon = db.scalar(
        select(func.count(func.distinct(Listing.book_id))).where(Listing.marketplace == "ozon")
    ) or 0
    on_wb = db.scalar(
        select(func.count(func.distinct(Listing.book_id))).where(
            Listing.marketplace == "wildberries"
        )
    ) or 0
    return {"total": total, "in_stock": in_stock, "gone": gone, "on_ozon": on_ozon, "on_wb": on_wb}


@router.get("/", response_class=HTMLResponse)
def index(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    status: str = "",
    marketplace: str = "",
    page: int = 1,
    wiped: str = "",
    wipe_error: str = "",
    synced: str = "",
):
    stmt = _filtered_books_query(q, status, marketplace)

    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    # Зажимаем номер страницы в допустимый диапазон: ввод вручную может быть любым.
    page = min(max(1, page), pages)
    books = db.scalars(
        stmt.order_by(Book.updated_at.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()

    return templates.TemplateResponse(
        request,
        "catalog.html",
        {
            "books": books,
            "q": q,
            "status": status,
            "marketplace": marketplace,
            "page": page,
            "pages": pages,
            "total": total,
            "statuses": list(BookStatus),
            "marketplaces": list(Marketplace),
            "wiped": wiped,
            "wipe_error": wipe_error,
            "synced": synced,
            "stats": _catalog_stats(db),
        },
    )


@router.get("/api/books")
def api_books(
    db: Session = Depends(get_db),
    q: str = "",
    status: str = "",
    marketplace: str = "",
    page: int = 1,
):
    """JSON-фрагмент списка книг для живого поиска по мере ввода (без перезагрузки)."""
    stmt = _filtered_books_query(q, status, marketplace)
    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    books = db.scalars(
        stmt.order_by(Book.updated_at.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()

    items = []
    for b in books:
        items.append(
            {
                "id": b.id,
                "sku": b.sku,
                "title": b.title,
                "price": f"{b.price:.0f} ₽" if b.price is not None else "—",
                "status": b.status,
                "status_label": book_status_label(b.status),
                "listings": [
                    {"short": marketplace_short(l.marketplace), "status": l.status,
                     "status_label": listing_status_label(l.status)}
                    for l in b.listings
                ],
            }
        )
    return JSONResponse({"items": items, "total": total, "page": page, "pages": pages})


@router.get("/books/{book_id}", response_class=HTMLResponse)
def view_book(book_id: int, request: Request, db: Session = Depends(get_db)):
    """Карточка книги — только просмотр. Данные подтягиваются сверкой с площадок."""
    book = db.get(Book, book_id)
    if not book:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "book_detail.html", {"book": book})


@router.post("/catalog/wipe")
def wipe_catalog(
    request: Request,
    db: Session = Depends(get_db),
    password: str = Form(""),
):
    """Очистка каталога ТОЛЬКО в локальной базе: книги, лоты, заказы и журнал.

    Защищено отдельным паролем. К API площадок не обращается — товары на Ozon
    и WB не затрагиваются. Разрушительно и необратимо: данные удаляются вместе
    с загруженными фото.

    Порядок важен: SyncLog и Order ссылаются на books по FK (book_id). На
    PostgreSQL удаление книг раньше падало из-за этих ссылок — поэтому сначала
    чистим зависимые таблицы, затем книги.
    """
    if password.strip() != settings.wipe_password:
        return RedirectResponse("/?wipe_error=1", status_code=303)

    try:
        db.execute(delete(Order))
        db.execute(delete(SyncLog))
        db.execute(delete(Listing))
        db.execute(delete(Book))
        db.commit()
    except Exception:  # noqa: BLE001 — не роняем страницу 500, показываем ошибку в UI
        db.rollback()
        return RedirectResponse("/?wipe_error=1", status_code=303)

    # Удаляем загруженные фото с диска.
    if UPLOAD_DIR.exists():
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)

    return RedirectResponse("/?wiped=1", status_code=303)
