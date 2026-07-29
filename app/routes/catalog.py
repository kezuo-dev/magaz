"""Каталог книг — чистый мониторинг того, что реально на площадках.

Программа ничего не выставляет и не редактирует: каталог наполняется сверкой
(см. app/catalog_sync.py и /import), продажи ловятся опросом заказов и слежением
за остатками. Здесь только просмотр: список с поиском/фильтрами, карточка книги
(read-only) и разрушительная очистка локальной базы.
"""
import shutil

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from sqlalchemy import case, delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import settings
from app.db import get_db
from app.forbidden_check import check_catalog_for_forbidden
from app.models import (
    Book,
    BookStatus,
    Listing,
    ListingStatus,
    Marketplace,
    Order,
    SyncLog,
)
from app.pdf_export import generate_forbidden_pdf
from app.photos import UPLOAD_DIR
from app.reconciliation import reconcile_all_marketplaces
from app.templating import (
    book_status_css,
    book_status_hint,
    book_status_label,
    listing_status_label,
    marketplace_short,
    sort_listings,
)
from app.templating import templates

router = APIRouter()

PAGE_SIZE = 50


def _filtered_books_query(q: str, status: str, marketplace: str):
    """Собрать запрос списка книг по поиску/фильтрам (общий для страницы и API)."""
    stmt = select(Book).options(selectinload(Book.listings))
    if q:
        # Экранируем подстановочные символы LIKE: без этого поиск «50%» или «A_B»
        # трактовал бы % и _ как шаблон и находил бы лишнее.
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{needle}%"
        stmt = stmt.where(
            or_(
                Book.title.ilike(like, escape="\\"),
                Book.sku.ilike(like, escape="\\"),
                Book.isbn.ilike(like, escape="\\"),
            )
        )
    if status:
        stmt = stmt.where(Book.status == status)
    # Фильтр по площадке: оставляем книги, у которых есть лот на этой площадке.
    # Специальные фильтры "только на X": книга должна быть ТОЛЬКО на одной площадке.
    if marketplace == "ozon_only":
        # Есть активный лот на Ozon, но нет активных лотов на WB
        stmt = stmt.where(
            Book.listings.any(
                (Listing.marketplace == "ozon") & (Listing.status == ListingStatus.ACTIVE)
            ),
            ~Book.listings.any(
                (Listing.marketplace == "wildberries") & (Listing.status == ListingStatus.ACTIVE)
            ),
        )
    elif marketplace == "wb_only":
        # Есть активный лот на WB, но нет активных лотов на Ozon
        stmt = stmt.where(
            Book.listings.any(
                (Listing.marketplace == "wildberries") & (Listing.status == ListingStatus.ACTIVE)
            ),
            ~Book.listings.any(
                (Listing.marketplace == "ozon") & (Listing.status == ListingStatus.ACTIVE)
            ),
        )
    elif marketplace:
        # Обычный фильтр: есть активный лот на указанной площадке (может быть и на других)
        stmt = stmt.where(
            Book.listings.any(
                (Listing.marketplace == marketplace) & (Listing.status == ListingStatus.ACTIVE)
            )
        )
    return stmt


def _sorted_books_query(stmt):
    """Порядок списка: сначала книги в продаже, затем проданные и снятые.

    Внутри группы — свежие изменения сверху. Так актуальный товар всегда наверху,
    а история (продано/снято) не мешается в начале каталога.
    """
    in_sale_first = case((Book.status == BookStatus.IN_STOCK, 0), else_=1)
    return stmt.order_by(in_sale_first, Book.updated_at.desc())


def _catalog_stats(db: Session) -> dict:
    """Сводка для карточек-счётчиков наверху каталога."""
    total = db.scalar(select(func.count()).select_from(Book)) or 0
    in_stock = db.scalar(
        select(func.count()).select_from(Book).where(Book.status == BookStatus.IN_STOCK)
    ) or 0
    # Продано и снято — разные вещи: продажа подтверждена заказом, снятие нет.
    sold = db.scalar(
        select(func.count()).select_from(Book).where(Book.status == BookStatus.SOLD)
    ) or 0
    withdrawn = db.scalar(
        select(func.count()).select_from(Book).where(Book.status == BookStatus.WITHDRAWN)
    ) or 0
    # Считаем только то, что реально продаётся на площадке: лот активен И книга в
    # продаже. Снятый лот не считается, даже если книга ещё активна на другой
    # площадке (например, продали на Ozon — в счётчике Ozon её уже нет).
    def _on_marketplace(marketplace: str) -> int:
        return db.scalar(
            select(func.count(func.distinct(Listing.book_id)))
            .select_from(Listing)
            .join(Book, Book.id == Listing.book_id)
            .where(
                Listing.marketplace == marketplace,
                Listing.status == ListingStatus.ACTIVE,
                Book.status == BookStatus.IN_STOCK,
            )
        ) or 0

    on_ozon = _on_marketplace("ozon")
    on_wb = _on_marketplace("wildberries")
    return {
        "total": total,
        "in_stock": in_stock,
        "sold": sold,
        "withdrawn": withdrawn,
        "on_ozon": on_ozon,
        "on_wb": on_wb,
    }


def _page_numbers(page: int, pages: int) -> list[int]:
    """Номера страниц для навигатора с многоточиями (0 = разрыв «…»).

    Всегда показываем первую и последнюю, окно ±1 вокруг текущей и «…» на разрывах.
    Пример для стр. 7 из 20: [1, 0, 6, 7, 8, 0, 20].
    """
    if pages <= 7:
        return list(range(1, pages + 1))
    nums = {1, pages, page, page - 1, page + 1}
    nums = sorted(n for n in nums if 1 <= n <= pages)
    out: list[int] = []
    prev = 0
    for n in nums:
        if prev and n - prev > 1:
            out.append(0)  # разрыв «…»
        out.append(n)
        prev = n
    return out


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
        _sorted_books_query(stmt)
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
            "page_numbers": _page_numbers(page, pages),
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
        _sorted_books_query(stmt)
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
                "author": b.author,
                "price": f"{b.price:.0f} ₽" if b.price is not None else "—",
                "status": b.status,
                "status_label": book_status_label(b.status),
                "status_hint": book_status_hint(b.status),
                "status_css": book_status_css(b.status),
                # Только активные лоты, в стабильном порядке (Ozon → WB): снятый лот
                # в списке лишь путал бы («OZ снято» у книги, которой на Ozon нет).
                "listings": [
                    {"short": marketplace_short(l.marketplace), "status": l.status,
                     "status_label": listing_status_label(l.status)}
                    for l in sort_listings(b.listings)
                    if l.status == ListingStatus.ACTIVE
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


@router.get("/catalog/check_forbidden", response_class=HTMLResponse)
def check_forbidden(request: Request, db: Session = Depends(get_db)):
    """Проверка каталога на запрещённые темы: экстремизм, терроризм, ЛГБТ и т.д.

    Сканирует все книги в продаже на ключевые слова. Возвращает страницу со списком
    найденных книг и кнопкой экспорта в PDF.
    """
    results = check_catalog_for_forbidden(db)
    return templates.TemplateResponse(
        request, "forbidden_results.html", {"results": results}
    )


@router.get("/catalog/forbidden/pdf")
def download_forbidden_pdf(db: Session = Depends(get_db)):
    """Скачать PDF-отчёт с найденными книгами (запрещённые темы)."""
    results = check_catalog_for_forbidden(db)
    pdf_bytes = generate_forbidden_pdf(results)
    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={
            "Content-Disposition": "attachment; filename=forbidden_check.pdf"
        },
    )


@router.post("/catalog/reconcile")
def reconcile_withdrawn(db: Session = Depends(get_db)):
    """Кнопка «Проверить снятые»: принудительная сверка снятых/проданных книг.

    Запрашивает реальные остатки через API и повторно снимает книги, которые
    всё ещё продаются (остаток > 0), хотя помечены как withdrawn/sold.
    """
    results = reconcile_all_marketplaces(db)
    db.commit()

    if not results:
        return RedirectResponse("/?synced=Проверка снятых книг: всё в порядке", status_code=303)

    parts = []
    for mp, res in results.items():
        checked = res.get("checked", 0)
        fixed = res.get("fixed", 0)
        if fixed > 0:
            parts.append(f"{mp}: проверено {checked}, исправлено {fixed}")
        elif checked > 0:
            parts.append(f"{mp}: проверено {checked}, всё в порядке")

    from urllib.parse import quote
    return RedirectResponse("/?synced=" + quote("; ".join(parts) if parts else "Проверка снятых книг завершена"), status_code=303)
