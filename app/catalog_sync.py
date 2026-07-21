"""Сверка каталога с площадками — сердце автоматики после отказа от выставления.

Программа больше не выставляет книги, а отслеживает то, что реально есть на
Ozon и WB, и снимает проданное. Три независимых механизма (см. scheduler.py):

1. Опрос заказов (sync.poll_marketplace_orders, ~1 мин) — ловит продажи.
2. Слежение за остатками (watch_stocks / watch_all_stocks, ~5 мин) — дёшево
   спрашивает остатки НАШИХ книг по их ключам (без выгрузки всего каталога).
   Остаток 0 или ключ пропал → книга снята → кросс-снятие. Главный «частый» канал.
3. Полная сверка (sync_marketplace / sync_all, ~60 мин) — тянет весь каталог,
   находит НОВЫЕ книги и снимает пропавшие. Авторитетная, но тяжёлая.

Функции наполнения/актуализации:
- upsert_catalog_rows — создать/обновить книги по строкам выгрузки (общий код);
- reconcile_disappeared — снять книги, пропавшие из ПОЛНОЙ выгрузки площадки;
- watch_stocks — снять книги, у которых остаток по ключу упал до 0 / ключ исчез.

Кросс-снятие всегда трогает ТОЛЬКО лоты своей площадки при выборке, а снимает с
остальных через withdraw_book_everywhere. Книга, которой нет на площадке (только
на Ozon или только на WB), чужим механизмом не затрагивается — выбор строго по
marketplace.

Защита от ложных снятий: пустой ответ каталога/остатков (сбой сети/лимит) НЕ
трогает книги — иначе одна ошибка API сняла бы весь каталог.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.marketplaces import MarketplaceError, get_client, is_supported
from app.models import (
    Book,
    BookStatus,
    Listing,
    ListingStatus,
    MarketplaceAccount,
    SyncLog,
    utcnow,
)
from app.security import decrypt_credentials
from app.sync import withdraw_book_everywhere

# Поля книги, на которые сопоставляются колонки выгрузки (ключи = поля модели).
TARGET_FIELDS = {
    "sku": "Артикул (SKU)",
    "title": "Название",
    "author": "Автор",
    "isbn": "ISBN",
    "publisher": "Издательство",
    "year": "Год",
    "condition": "Состояние",
    "price": "Цена",
    "description": "Описание",
    "external_id": "ID лота на площадке",
}


def _log(db: Session, *, marketplace, action, ok, message) -> None:
    db.add(SyncLog(marketplace=marketplace, action=action, ok=ok, message=message))


def _parse_stock(raw) -> int | None:
    """Разобрать остаток из строки выгрузки. None — остаток неизвестен."""
    if raw in (None, ""):
        return None
    try:
        return int(float(str(raw).strip().replace(",", ".")))
    except (ValueError, TypeError):
        return None


def _cross_withdraw(db: Session, book: Book, marketplace: str, listing: Listing) -> None:
    """Единый путь снятия книги, пропавшей/проданной на площадке `marketplace`.

    Помечаем лот этой площадки снятым (остатка там уже нет — живой вызов не нужен)
    и кросс-снимаем с остальных площадок.
    """
    listing.status = ListingStatus.WITHDRAWN
    listing.last_synced_at = utcnow()
    withdraw_book_everywhere(db, book, except_marketplace=marketplace)
    if book.status == BookStatus.IN_STOCK:
        book.status = BookStatus.WITHDRAWN


def upsert_catalog_rows(db: Session, marketplace: str, rows: list[dict], mapping: dict) -> dict:
    """Создать/обновить книги по строкам выгрузки и сопоставлению колонок.

    Возвращает {created, updated, skipped, live_skus}. live_skus — множество SKU
    книг, которые по этой выгрузке ЕСТЬ в наличии на площадке (остаток > 0 или
    неизвестен). По нему сверка понимает, какие книги пропали (см. reconcile).

    Логику делят импорт файлом (routes/imports.py) и сверка по API (ниже).
    """
    created = updated = skipped = 0
    live_skus: set[str] = set()

    for row in rows:
        def val(field: str):
            col = mapping.get(field)
            v = row.get(col) if col else None
            return str(v).strip() if v not in (None, "") else None

        sku = val("sku")
        isbn = val("isbn")
        title = val("title")
        if not title and not sku:
            skipped += 1
            continue

        # Ищем существующую книгу по SKU, затем по ISBN — чтобы не плодить дубли.
        book = None
        if sku:
            book = db.scalar(select(Book).where(Book.sku == sku))
        if not book and isbn:
            book = db.scalar(select(Book).where(Book.isbn == isbn))

        stock = _parse_stock(row.get("stock"))
        out_of_stock = stock is not None and stock <= 0
        # Ключ остатка на площадке (offer_id у Ozon, баркод у WB). Клиент кладёт
        # его прямо в строку выгрузки (не через сопоставление колонок — при импорте
        # файлом такой колонки нет). Пусто — оставим текущий/по внешнему id.
        raw_key = row.get("stock_key")
        stock_key = str(raw_key).strip() if raw_key not in (None, "") else None

        if book:
            updated += 1
        else:
            book = Book(sku=sku or f"AUTO-{isbn or title[:20]}")
            book.status = BookStatus.IN_STOCK
            db.add(book)
            created += 1

        # Заполняем только пустые поля, чтобы выгрузка со второй площадки не затирала.
        book.title = book.title or title
        book.author = book.author or val("author")
        book.isbn = book.isbn or isbn
        book.publisher = book.publisher or val("publisher")
        book.condition = book.condition or val("condition")
        if not book.description:
            book.description = val("description")
        year = val("year")
        if year and year.isdigit() and not book.year:
            book.year = int(year)
        price = val("price")
        if price and book.price is None:
            try:
                book.price = float(price.replace(",", "."))
            except ValueError:
                pass

        db.flush()  # нужен book.id для лота

        # Привязываем лот площадки, если его ещё нет.
        listing = next((l for l in book.listings if l.marketplace == marketplace), None)
        if not listing:
            listing = Listing(
                book_id=book.id,
                marketplace=marketplace,
                external_id=val("external_id"),
                stock_key=stock_key,
                status=ListingStatus.WITHDRAWN if out_of_stock else ListingStatus.ACTIVE,
            )
            db.add(listing)
            book.listings.append(listing)
        else:
            if val("external_id") and not listing.external_id:
                listing.external_id = val("external_id")
            # Ключ остатка держим в актуальном состоянии — по нему идёт слежение.
            if stock_key:
                listing.stock_key = stock_key

        if out_of_stock:
            # Нет в наличии на площадке → снимаем и с других (кросс-снятие).
            _cross_withdraw(db, book, marketplace, listing)
        else:
            listing.status = ListingStatus.ACTIVE
            listing.last_synced_at = utcnow()
            if sku:
                live_skus.add(sku)

    return {"created": created, "updated": updated, "skipped": skipped, "live_skus": live_skus}


def reconcile_disappeared(db: Session, marketplace: str, live_skus: set[str]) -> int:
    """Снять книги, пропавшие из каталога площадки (карточки больше нет / остаток 0).

    Проходим по всем НЕснятым лотам этой площадки. Если SKU книги нет в live_skus
    (площадка эту книгу больше не отдаёт как «в наличии») — снимаем книгу со всех
    площадок и запускаем окно до архива. Так продажа/снятие на одной площадке
    зеркалится на другую, даже если опрос заказов её не поймал.

    Книги, у которых нет лота на этой площадке (например, только на WB), не
    затрагиваются — выбираем строго по marketplace. Возвращает число снятых книг.
    """
    listings = db.scalars(
        select(Listing)
        .options(selectinload(Listing.book).selectinload(Book.listings))
        .where(
            Listing.marketplace == marketplace,
            Listing.status != ListingStatus.WITHDRAWN,
        )
    ).all()

    removed = 0
    for listing in listings:
        book = listing.book
        if book is None:
            continue
        # Книга всё ещё в живой выгрузке — ничего не делаем.
        if book.sku in live_skus:
            continue

        _cross_withdraw(db, book, marketplace, listing)
        removed += 1
        _log(db, marketplace=marketplace, action="reconcile_removed", ok=True,
             message=f"Книга {book.sku} пропала с {marketplace}")

    return removed


def sync_marketplace(db: Session, marketplace: str) -> dict:
    """Полная сверка одной площадки: тянем каталог, апсертим, снимаем пропавшее.

    Возвращает {created, updated, skipped, removed}. Если площадка выключена/не
    настроена или вернула пустой каталог — сверку не делаем (защита от снятия
    всего каталога из-за сбоя API).
    """
    if not is_supported(marketplace):
        raise MarketplaceError(f"Площадка «{marketplace}» не поддерживается")

    account = db.scalar(
        select(MarketplaceAccount).where(MarketplaceAccount.marketplace == marketplace)
    )
    if not account or not account.enabled or not account.credentials_encrypted:
        raise MarketplaceError("Площадка выключена или ключи не заданы")

    creds = decrypt_credentials(account.credentials_encrypted)
    client = get_client(marketplace, creds)
    rows = client.fetch_catalog()

    if not rows:
        # Пустой ответ — не факт, что каталог пуст. Могла быть ошибка/лимит.
        # Не трогаем книги, только пишем в журнал.
        _log(db, marketplace=marketplace, action="catalog_sync", ok=False,
             message="Площадка вернула пустой каталог — сверка пропущена (защита от ложного снятия)")
        return {"created": 0, "updated": 0, "skipped": 0, "removed": 0}

    mapping = {field: field for field in TARGET_FIELDS}
    result = upsert_catalog_rows(db, marketplace, rows, mapping)
    removed = reconcile_disappeared(db, marketplace, result["live_skus"])

    _log(db, marketplace=marketplace, action="catalog_sync", ok=True,
         message=(f"Сверка: создано {result['created']}, обновлено {result['updated']}, "
                  f"снято пропавших {removed}"))
    return {
        "created": result["created"],
        "updated": result["updated"],
        "skipped": result["skipped"],
        "removed": removed,
    }


def sync_all(db: Session) -> dict:
    """Сверить все включённые площадки. Сбой одной не останавливает остальные.

    Возвращает {marketplace: результат|ошибка} по каждой включённой площадке.
    """
    enabled = db.scalars(
        select(MarketplaceAccount.marketplace).where(MarketplaceAccount.enabled == True)  # noqa: E712
    ).all()

    out: dict[str, dict] = {}
    for marketplace in enabled:
        try:
            out[marketplace] = sync_marketplace(db, marketplace)
            db.commit()
        except Exception as exc:  # noqa: BLE001 — сбой одной площадки не роняет сверку
            db.rollback()
            _log(db, marketplace=marketplace, action="catalog_sync", ok=False, message=str(exc))
            db.commit()
            out[marketplace] = {"error": str(exc)}
    return out


def _active_listings(db: Session, marketplace: str) -> list[Listing]:
    """Активные лоты площадки с подгруженной книгой и её остальными лотами."""
    return db.scalars(
        select(Listing)
        .options(selectinload(Listing.book).selectinload(Book.listings))
        .where(
            Listing.marketplace == marketplace,
            Listing.status == ListingStatus.ACTIVE,
        )
    ).all()


def watch_stocks(db: Session, marketplace: str) -> dict:
    """Дёшево проверить остатки НАШИХ книг на площадке и снять обнулившиеся.

    В отличие от полной сверки (тянет весь чужой каталог), спрашиваем остатки
    ровно по ключам наших активных лотов — это ~1 запрос на 1000 книг. Механизм
    частый (см. scheduler), поэтому продажа/снятие на площадке зеркалится на
    другую почти сразу, даже между полными сверками.

    Правило снятия: остаток по ключу == 0 ЛИБО площадка ключ не вернула (карточка
    удалена/скрыта) → книга снята → кросс-снятие с остальных площадок.

    Защита от ложного снятия: если площадка не вернула НИ ОДНОГО из запрошенных
    ключей (похоже на сбой/лимит, а не на то, что разом продали весь склад) —
    ничего не трогаем. Возвращает {checked, removed} либо {error}.
    """
    if not is_supported(marketplace):
        return {"error": f"Площадка «{marketplace}» не поддерживается"}

    account = db.scalar(
        select(MarketplaceAccount).where(MarketplaceAccount.marketplace == marketplace)
    )
    if not account or not account.enabled or not account.credentials_encrypted:
        return {"error": "Площадка выключена или ключи не заданы"}

    listings = _active_listings(db, marketplace)
    # Лоты, у которых есть ключ остатка. Без ключа проверить нечем — их обойдёт
    # полная сверка. Один ключ может стоять у нескольких лотов — сгруппируем.
    keyed = [l for l in listings if l.stock_key]
    if not keyed:
        return {"checked": 0, "removed": 0}

    keys = sorted({l.stock_key for l in keyed})

    creds = decrypt_credentials(account.credentials_encrypted)
    client = get_client(marketplace, creds)
    try:
        stocks = client.fetch_stocks(keys)
    except MarketplaceError as exc:
        _log(db, marketplace=marketplace, action="watch_stocks", ok=False, message=str(exc))
        return {"error": str(exc)}

    # Защита: пустой ответ на непустой запрос — считаем сбоем, не снимаем.
    if not stocks:
        _log(db, marketplace=marketplace, action="watch_stocks", ok=False,
             message="Пустой ответ по остаткам — слежение пропущено (защита от ложного снятия)")
        return {"checked": len(keys), "removed": 0}

    removed = 0
    for listing in keyed:
        book = listing.book
        if book is None:
            continue
        # Ключ не вернулся (карточки нет) ИЛИ остаток обнулён — снимаем.
        amount = stocks.get(listing.stock_key)
        if amount is None or amount <= 0:
            _cross_withdraw(db, book, marketplace, listing)
            removed += 1
            reason = "остаток 0" if amount is not None else "карточка пропала"
            _log(db, marketplace=marketplace, action="watch_removed", ok=True,
                 message=f"Книга {book.sku}: {reason} на {marketplace}")

    if removed:
        _log(db, marketplace=marketplace, action="watch_stocks", ok=True,
             message=f"Слежение за остатками: проверено {len(keys)}, снято {removed}")
    return {"checked": len(keys), "removed": removed}


def watch_all_stocks(db: Session) -> dict:
    """Слежение за остатками по всем включённым площадкам. Сбой одной не роняет остальные."""
    enabled = db.scalars(
        select(MarketplaceAccount.marketplace).where(MarketplaceAccount.enabled == True)  # noqa: E712
    ).all()

    out: dict[str, dict] = {}
    for marketplace in enabled:
        try:
            out[marketplace] = watch_stocks(db, marketplace)
            db.commit()
        except Exception as exc:  # noqa: BLE001 — сбой одной площадки не роняет слежение
            db.rollback()
            _log(db, marketplace=marketplace, action="watch_stocks", ok=False, message=str(exc))
            db.commit()
            out[marketplace] = {"error": str(exc)}
    return out
