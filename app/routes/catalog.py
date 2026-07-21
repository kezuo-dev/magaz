"""Каталог книг: список с поиском/фильтрами, карточка (только просмотр), архив, массовые операции.

Выставление книг убрано — каталог это зеркало того, что реально на площадках.
Наполняется сверкой (см. app/catalog_sync.py и /import). Ручные массовые операции
(снять / отметить проданной / в архив / вернуть) остаются как подстраховка.
"""
import shutil

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.archive import clear_removed, days_until_archive, mark_removed
from app.config import settings
from app.db import get_db
from app.models import (
    Book,
    BookStatus,
    Listing,
    ListingStatus,
    Marketplace,
    Order,
    utcnow,
)
from app.photos import UPLOAD_DIR
from app.sync import withdraw_book, withdraw_book_everywhere
from app.templating import templates

router = APIRouter()

PAGE_SIZE = 50


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
    # Основной каталог: только книги, ещё не уехавшие в архив.
    stmt = select(Book).options(selectinload(Book.listings)).where(Book.archived_at.is_(None))

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
        stmt = stmt.where(
            Book.listings.any(Listing.marketplace == marketplace)
        )

    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    # Зажимаем номер страницы в допустимый диапазон: ввод вручную может быть любым.
    page = min(max(1, page), pages)
    books = db.scalars(
        stmt.order_by(Book.updated_at.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()

    # Сколько книг уже уехало в архив — для ссылки-счётчика в шапке списка.
    archived_count = db.scalar(
        select(func.count()).select_from(Book).where(Book.archived_at.is_not(None))
    )

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
            "archived_count": archived_count,
            "days_until_archive": days_until_archive,
        },
    )


@router.get("/archive", response_class=HTMLResponse)
def archive(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    status: str = "",
    page: int = 1,
):
    """Архив: проданные и снятые книги, уже убранные из основного каталога."""
    stmt = select(Book).options(selectinload(Book.listings)).where(Book.archived_at.is_not(None))

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

    total = db.scalar(select(func.count()).select_from(stmt.subquery()))
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = min(max(1, page), pages)
    books = db.scalars(
        stmt.order_by(Book.archived_at.desc())
        .offset((page - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()

    # В архив попадают только эти два статуса — их и предлагаем в фильтре.
    archive_statuses = [BookStatus.SOLD, BookStatus.WITHDRAWN]

    return templates.TemplateResponse(
        request,
        "archive.html",
        {
            "books": books,
            "q": q,
            "status": status,
            "page": page,
            "pages": pages,
            "total": total,
            "statuses": archive_statuses,
            "marketplaces": list(Marketplace),
        },
    )


@router.get("/books/{book_id}", response_class=HTMLResponse)
def view_book(book_id: int, request: Request, db: Session = Depends(get_db)):
    """Карточка книги — только просмотр. Данные подтягиваются сверкой с площадок."""
    book = db.get(Book, book_id)
    if not book:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "book_detail.html",
        {"book": book, "days_until_archive": days_until_archive},
    )


@router.post("/books/bulk")
def bulk_action(
    request: Request,
    db: Session = Depends(get_db),
    action: str = Form(...),
    book_ids: list[int] = Form(default=[]),
):
    """Ручные массовые операции из списка — подстраховка к автоматике.

    Снятие идёт через сервис синхронизации: если площадка подключена — реальный
    вызов API, если выключена — только локальный статус. Выставление убрано.
    """
    books = db.scalars(
        select(Book).options(selectinload(Book.listings)).where(Book.id.in_(book_ids))
    ).all()

    for book in books:
        if action == "withdraw":
            # Снимаем со всех площадок, где у книги есть лот.
            withdraw_book_everywhere(db, book)
            if not any(l.status == ListingStatus.ACTIVE for l in book.listings):
                book.status = BookStatus.WITHDRAWN
                mark_removed(book)  # уходит с продажи — запускаем отсчёт до архива
        elif action == "mark_sold":
            withdraw_book_everywhere(db, book)
            book.status = BookStatus.SOLD
            mark_removed(book)
        elif action == "archive":
            # Ручной перенос в архив, не дожидаясь окончания окна.
            mark_removed(book)
            book.archived_at = utcnow()
        elif action == "restore":
            # Возврат из архива в каталог. Книга остаётся снятой/проданной, но снова видна.
            book.archived_at = None
            book.removed_at = None
    db.commit()
    return RedirectResponse(request.headers.get("referer", "/"), status_code=303)


@router.post("/catalog/wipe")
def wipe_catalog(
    request: Request,
    db: Session = Depends(get_db),
    password: str = Form(""),
):
    """Очистка каталога ТОЛЬКО в локальной базе: все книги, лоты и заказы.

    Защищено отдельным паролем. К API площадок не обращается — товары на Ozon
    и WB не затрагиваются. Разрушительно и необратимо: данные удаляются вместе
    с загруженными фото. Заказы и лоты подчищаются явно, т.к. Order не связан
    каскадом с Book.
    """
    if password.strip() != settings.wipe_password:
        return RedirectResponse("/?wipe_error=1", status_code=303)

    db.execute(delete(Order))
    db.execute(delete(Listing))
    db.execute(delete(Book))
    db.commit()

    # Удаляем загруженные фото с диска.
    if UPLOAD_DIR.exists():
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)

    return RedirectResponse("/?wiped=1", status_code=303)
