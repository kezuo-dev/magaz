"""Каталог книг: список с поиском/фильтрами, карточка, добавление/редактирование, массовые операции."""
import shutil
from types import SimpleNamespace

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from sqlalchemy import delete, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.archive import clear_removed, days_until_archive, mark_removed
from app.config import settings
from app.db import get_db
from app.marketplaces import MarketplaceError, get_client
from app.models import (
    Book,
    BookStatus,
    Listing,
    ListingStatus,
    Marketplace,
    MarketplaceAccount,
    Order,
    utcnow,
)
from app.photos import UPLOAD_DIR, delete_photo_file, save_photos
from app.security import decrypt_credentials
from app.sync import publish_book, withdraw_book, withdraw_book_everywhere
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


def _last_category_defaults(db: Session) -> dict:
    """Категории/предмет из последней сохранённой книги — подставляются в новую.

    Обычно продавец заводит книги в одном и том же разделе, поэтому предзаполняем
    поля значениями предыдущей книги, чтобы не выбирать категорию каждый раз.
    """
    last = db.scalar(
        select(Book)
        .where(
            or_(
                Book.ozon_category_id.is_not(None),
                Book.wb_subject_id.is_not(None),
            )
        )
        .order_by(Book.updated_at.desc())
    )
    if not last:
        return {}
    return {
        "ozon_category_id": last.ozon_category_id,
        "ozon_type_id": last.ozon_type_id,
        "wb_subject_id": last.wb_subject_id,
    }


@router.get("/books/new", response_class=HTMLResponse)
def new_book(request: Request, db: Session = Depends(get_db)):
    return templates.TemplateResponse(
        request,
        "book_form.html",
        {
            "book": None,
            "marketplaces": list(Marketplace),
            "category_defaults": _last_category_defaults(db),
        },
    )


@router.get("/books/categories")
def book_categories(marketplace: str, q: str = "", db: Session = Depends(get_db)):
    """Справочник категорий/предметов площадки для подбора ID в карточке книги.

    Тянет справочник на сохранённых ключах площадки и отдаёт JSON: список
    вариантов с готовыми значениями полей, которые подставятся в форму.
    """
    account = db.scalar(
        select(MarketplaceAccount).where(MarketplaceAccount.marketplace == marketplace)
    )
    if not account or not account.credentials_encrypted:
        return JSONResponse(
            {"ok": False, "error": "Сначала сохраните ключи площадки в настройках"},
            status_code=400,
        )
    try:
        creds = decrypt_credentials(account.credentials_encrypted)
        client = get_client(marketplace, creds)
        items = client.fetch_categories(q)
    except MarketplaceError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 — показываем причину как есть
        return JSONResponse({"ok": False, "error": f"Ошибка загрузки: {exc}"}, status_code=400)
    return JSONResponse({"ok": True, "items": items})


@router.get("/books/ozon-directions")
def ozon_directions(q: str = "", db: Session = Depends(get_db)):
    """Справочник жанров («Направление») Ozon для выбора в карточке книги."""
    account = db.scalar(
        select(MarketplaceAccount).where(MarketplaceAccount.marketplace == "ozon")
    )
    if not account or not account.credentials_encrypted:
        return JSONResponse(
            {"ok": False, "error": "Сначала сохраните ключи Ozon в настройках"},
            status_code=400,
        )
    try:
        creds = decrypt_credentials(account.credentials_encrypted)
        client = get_client("ozon", creds)
        items = client.fetch_directions(q)
    except MarketplaceError as exc:
        return JSONResponse({"ok": False, "error": str(exc)}, status_code=400)
    except Exception as exc:  # noqa: BLE001 — показываем причину как есть
        return JSONResponse({"ok": False, "error": f"Ошибка загрузки: {exc}"}, status_code=400)
    return JSONResponse({"ok": True, "items": items})


def _parse_price(price: str):
    """Разобрать цену из формы в число; None, если пусто/некорректно."""
    price = (price or "").strip().replace(",", ".")
    if not price:
        return None
    try:
        return float(price)
    except ValueError:
        return None


def _render_form_error(request: Request, book, error: str):
    """Показать форму книги заново с сообщением об ошибке (код 400)."""
    return templates.TemplateResponse(
        request,
        "book_form.html",
        {"book": book, "marketplaces": list(Marketplace), "error": error, "category_defaults": {}},
        status_code=400,
    )


@router.get("/books/{book_id}", response_class=HTMLResponse)
def edit_book(book_id: int, request: Request, db: Session = Depends(get_db)):
    book = db.get(Book, book_id)
    if not book:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "book_form.html",
        {"book": book, "marketplaces": list(Marketplace), "category_defaults": {}},
    )


@router.post("/books/save")
def save_book(
    request: Request,
    db: Session = Depends(get_db),
    book_id: str = Form(""),
    sku: str = Form(""),
    title: str = Form(...),
    author: str = Form(""),
    isbn: str = Form(""),
    publisher: str = Form(""),
    year: str = Form(""),
    condition: str = Form(""),
    price: str = Form(""),
    quantity: str = Form(""),
    description: str = Form(""),
    ozon_category_id: str = Form(""),
    ozon_type_id: str = Form(""),
    wb_subject_id: str = Form(""),
    ozon_direction_id: str = Form(""),
    ozon_direction_name: str = Form(""),
    weight_grams: str = Form(""),
    length_mm: str = Form(""),
    width_mm: str = Form(""),
    height_mm: str = Form(""),
    photo_files: list[UploadFile] = File(default=[]),
    remove_photos: list[str] = Form(default=[]),
):
    book = db.get(Book, int(book_id)) if book_id else Book(sku="")
    sku = sku.strip()
    isbn = isbn.strip()
    ozon_category_id = ozon_category_id.strip()
    ozon_type_id = ozon_type_id.strip()
    wb_subject_id = wb_subject_id.strip()
    ozon_direction_id = ozon_direction_id.strip()
    ozon_direction_name = ozon_direction_name.strip()

    def _int_or_none(v: str):
        v = (v or "").strip()
        return int(v) if v.isdigit() else None

    def _submitted():
        """Собрать введённые данные обратно в форму, чтобы не терять ввод при ошибке."""
        return SimpleNamespace(
            id=book.id if book_id else None,
            sku=sku, title=title, author=author, isbn=isbn,
            publisher=publisher, year=year, condition=condition,
            price=_parse_price(price), description=description,
            quantity=(int(quantity) if quantity.strip().isdigit() else 1),
            ozon_category_id=ozon_category_id or None,
            ozon_type_id=ozon_type_id or None,
            wb_subject_id=wb_subject_id or None,
            ozon_direction_id=ozon_direction_id or None,
            ozon_direction_name=ozon_direction_name or None,
            weight_grams=_int_or_none(weight_grams),
            length_mm=_int_or_none(length_mm),
            width_mm=_int_or_none(width_mm),
            height_mm=_int_or_none(height_mm),
            photo_list=book.photo_list if book_id else [],
            listings=book.listings if book_id else [],
        )

    # Артикул обязателен — это общий идентификатор для всех площадок.
    if not sku:
        return _render_form_error(request, _submitted(), "Укажите артикул (SKU) — он обязателен.")
    # ISBN необязателен: если не указан, записываем «нет».
    if not isbn:
        isbn = "нет"

    # Артикул должен быть уникальным: проверяем заранее, чтобы показать понятную
    # ошибку, а не падение на ограничении БД.
    clash = db.scalar(
        select(Book).where(Book.sku == sku, Book.id != (book.id or 0))
    )
    if clash:
        return _render_form_error(
            request,
            _submitted(),
            f"Артикул «{sku}» уже занят другой книгой. Введите другой.",
        )
    book.sku = sku

    book.title = title.strip()
    book.author = author.strip() or None
    book.isbn = isbn
    book.publisher = publisher.strip() or None
    book.year = int(year) if year.strip().isdigit() else None
    book.condition = condition.strip() or None
    book.price = float(price.replace(",", ".")) if price.strip() else None
    # Количество: пусто или мусор → 1 (типичный б/у экземпляр). Не ниже 0.
    _qty = quantity.strip()
    book.quantity = max(0, int(_qty)) if _qty.isdigit() else 1
    book.description = description.strip() or None
    book.ozon_category_id = ozon_category_id or None
    book.ozon_type_id = ozon_type_id or None
    book.wb_subject_id = wb_subject_id or None
    book.ozon_direction_id = ozon_direction_id or None
    book.ozon_direction_name = ozon_direction_name or None
    book.weight_grams = _int_or_none(weight_grams)
    book.length_mm = _int_or_none(length_mm)
    book.width_mm = _int_or_none(width_mm)
    book.height_mm = _int_or_none(height_mm)

    if not book_id:
        db.add(book)

    db.flush()  # нужен book.id для папки с фото

    # Удаляем отмеченные галочками фото: сначала с диска, потом из списка ссылок.
    photos = book.photo_list
    if remove_photos:
        to_remove = set(remove_photos)
        for url in to_remove:
            delete_photo_file(book.id, url)
        photos = [p for p in photos if p not in to_remove]

    # Загруженные файлы сохраняем на диск и дописываем их ссылки к оставшимся.
    new_urls = save_photos(book.id, photo_files)
    book.photos = "\n".join(photos + new_urls) or None

    db.commit()
    return RedirectResponse(f"/books/{book.id}", status_code=303)


@router.post("/books/bulk")
def bulk_action(
    request: Request,
    db: Session = Depends(get_db),
    action: str = Form(...),
    book_ids: list[int] = Form(default=[]),
    targets: list[str] = Form(default=[]),
):
    """Массовые операции из списка. Идут через сервис синхронизации: если площадка
    подключена — реальный вызов API, если выключена — только локальный статус.

    `targets` — выбранные в меню площадки. Пусто = «все»: публикуем/снимаем
    там, где это осмысленно (см. ниже). Иначе действуем только по выбранным.
    """
    books = db.scalars(
        select(Book).options(selectinload(Book.listings)).where(Book.id.in_(book_ids))
    ).all()

    # Целевые площадки для публикации: все включённые аккаунты.
    enabled = db.scalars(
        select(MarketplaceAccount.marketplace).where(MarketplaceAccount.enabled == True)  # noqa: E712
    ).all()

    # Отбрасываем мусор: оставляем только реальные значения площадок.
    valid = {m.value for m in Marketplace}
    chosen = [t for t in targets if t in valid]

    for book in books:
        if action == "publish":
            if chosen:
                # Явно выбранные площадки — выставляем ровно на них.
                publish_targets = chosen
            else:
                # «Все»: туда, где уже есть лот, иначе — на все включённые площадки.
                publish_targets = [l.marketplace for l in book.listings] or list(enabled)
            for mp in publish_targets:
                publish_book(db, book, mp)
            book.status = BookStatus.IN_STOCK
            clear_removed(book)  # снова в продаже — сбрасываем ожидание архива
        elif action == "withdraw":
            # «Все» — снимаем со всех лотов книги; иначе только с выбранных площадок.
            withdraw_targets = chosen or [l.marketplace for l in book.listings]
            for mp in withdraw_targets:
                withdraw_book(db, book, mp)
            # Книгу считаем снятой, только если не осталось активных лотов.
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
    и других маркетплейсах не затрагиваются. Разрушительно и необратимо: данные
    удаляются вместе с загруженными фото. Заказы и лоты подчищаются явно, т.к.
    Order не связан каскадом с Book.
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
