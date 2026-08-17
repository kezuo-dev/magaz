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

from app.access import require_action
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
from app.pdf_export import generate_catalog_pdf, generate_forbidden_pdf
from app.photos import UPLOAD_DIR
from app.reconciliation import reconcile_all_marketplaces
from app.wb_trash import move_withdrawn_to_trash
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
    stmt = select(Book).options(selectinload(Book.listings), selectinload(Book.orders))
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
    return stmt.order_by(in_sale_first, Book.created_at.desc())


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

    # Обработка пустого каталога: не показываем "Страница 1 из 1"
    if total == 0:
        pages = 0
        page = 0
        books = []
    else:
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
                "status_hint": book_status_hint(b.status, b),
                "status_css": book_status_css(b.status),
                # Только активные лоты, в стабильном порядке (Ozon → WB): снятый лот
                # в списке лишь путал бы («OZ снято» у книги, которой на Ozon нет).
                "listings": [
                    {"short": marketplace_short(l.marketplace), "status": l.status,
                     "status_label": listing_status_label(l.status),
                     "marketplace": l.marketplace}
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


@router.post("/catalog/wipe", dependencies=[Depends(require_action("catalog_wipe"))])
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


@router.get("/catalog/export/pdf")
def export_catalog_pdf(
    db: Session = Depends(get_db),
    q: str = "",
    status: str = "",
    marketplace: str = "",
):
    """Скачать PDF-список книг по текущим фильтрам каталога (поиск/статус/площадка)."""
    stmt = _filtered_books_query(q, status, marketplace)
    books = db.scalars(_sorted_books_query(stmt)).all()

    # Формируем читаемое описание фильтра для заголовка в PDF
    parts = []
    if q:
        parts.append(f"поиск: «{q}»")
    if status:
        from app.templating import book_status_label
        parts.append(f"статус: {book_status_label(status)}")
    if marketplace == "ozon_only":
        parts.append("только Ozon (нет на WB)")
    elif marketplace == "wb_only":
        parts.append("только Wildberries (нет на Ozon)")
    elif marketplace:
        parts.append(f"площадка: {marketplace}")
    subtitle = "Фильтры: " + "; ".join(parts) if parts else "Все книги каталога"

    pdf_bytes = generate_catalog_pdf(books, subtitle=subtitle)

    from urllib.parse import quote
    safe_name = "catalog"
    if marketplace == "ozon_only":
        safe_name = "catalog_ozon_only"
    elif marketplace == "wb_only":
        safe_name = "catalog_wb_only"
    elif status:
        safe_name = f"catalog_{status}"

    return Response(
        content=pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": f"attachment; filename={safe_name}.pdf"},
    )


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


@router.post("/catalog/reconcile", dependencies=[Depends(require_action("catalog_sync"))])
def reconcile_withdrawn(db: Session = Depends(get_db)):
    """Кнопка «Проверить снятые»: принудительная сверка снятых/проданных книг.

    Спрашивает площадку, какие карточки она всё ещё показывает «В продаже», и
    повторно снимает те, что помечены у нас как withdrawn/sold. Выборка ограничена
    30 днями — старые снятые книги заведомо сняты с площадок.
    """
    # verbose=True: пользователь нажал кнопку и ждёт отчёта, поэтому итог должен
    # попасть в журнал даже когда исправлять было нечего.
    results = reconcile_all_marketplaces(db, verbose=True)
    db.commit()

    parts = []
    for mp, res in results.items():
        checked = res.get("checked", 0)
        fixed = res.get("fixed", 0)
        failed = res.get("failed", 0)
        if not checked:
            continue
        text = f"{mp}: проверено {checked}"
        if fixed:
            text += f", исправлено {fixed}"
        if failed:
            text += f", не удалось снять {failed}"
        if not fixed and not failed:
            text += ", всё в порядке"
        parts.append(text)

    from urllib.parse import quote
    message = "; ".join(parts) if parts else "Проверка снятых книг: проверять нечего"
    return RedirectResponse("/?synced=" + quote(message), status_code=303)


@router.post("/catalog/wb_trash", dependencies=[Depends(require_action("catalog_trash"))])
def wb_trash(db: Session = Depends(get_db), days: int = Form(7)):
    """Кнопка «Очистить корзину WB»: удалить снятые книги в корзину Wildberries.

    Принимает параметр days — период в днях (0 = все книги за всё время).
    По умолчанию 7 дней — безопасный период без риска схлопнуть лимит.
    """
    result = move_withdrawn_to_trash(db, days=days if days > 0 else None)
    db.commit()

    processed = result.get("processed", 0)
    deleted = result.get("deleted", 0)
    failed = result.get("failed", 0)
    blocked = result.get("blocked", 0)

    if not processed:
        message = "Очистка корзины WB: снятых книг для удаления нет"
    else:
        message = f"WB: удалено в корзину {deleted} из {processed}"
        if result.get("skipped"):
            message += f", отложено {result['skipped']} (лимит WB — продолжим автоматически)"
        if failed:
            message += f", не удалось {failed}"
        if blocked:
            message += f", битых заблокировано {blocked}"

    from urllib.parse import quote
    return RedirectResponse("/?synced=" + quote(message), status_code=303)


