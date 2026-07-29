"""Сверка фактического состояния книг на площадках с локальной базой.

Периодически проверяет книги, которые помечены как снятые/проданные, но могут
всё ещё висеть на площадке из-за сбоя API или неполного снятия (например,
обнулили остаток, но не заархивировали карточку Ozon). Запрашивает реальные
остатки через fetch_stocks и повторно снимает, если книга всё ещё продаётся.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.marketplaces import MarketplaceError, get_client, is_supported
from app.models import Book, BookStatus, Listing, ListingStatus, MarketplaceAccount, SyncLog
from app.security import decrypt_credentials
from app.sync import refresh_book_status, withdraw_book


def _log(db: Session, *, marketplace, action, ok, message, book_id=None) -> None:
    db.add(
        SyncLog(
            marketplace=marketplace,
            book_id=book_id,
            action=action,
            ok=ok,
            message=message,
        )
    )


def _get_active_client(db: Session, marketplace: str):
    """Вернуть клиент площадки или None, если выключена."""
    if not is_supported(marketplace):
        return None
    account = db.scalar(
        select(MarketplaceAccount).where(MarketplaceAccount.marketplace == marketplace)
    )
    if not account or not account.enabled or not account.credentials_encrypted:
        return None
    try:
        creds = decrypt_credentials(account.credentials_encrypted)
        return get_client(marketplace, creds)
    except (MarketplaceError, Exception):
        return None


def reconcile_withdrawn_books(db: Session, marketplace: str) -> dict:
    """Сверить снятые книги с реальным состоянием на площадке.

    Для книг со статусом SOLD или WITHDRAWN проверяем реальные остатки через API.
    Если книга всё ещё продаётся (остаток > 0) — снимаем повторно и пишем в журнал.

    Возвращает статистику: {"checked": N, "fixed": M}.
    """
    client = _get_active_client(db, marketplace)
    if client is None:
        return {"checked": 0, "fixed": 0}

    # Находим книги, которые должны быть сняты (статус SOLD или WITHDRAWN), но у
    # которых есть лот на этой площадке со статусом withdrawn. Такие книги могли
    # быть сняты локально, но фактически остаться на площадке из-за сбоя API.
    books = db.scalars(
        select(Book)
        .options(selectinload(Book.listings))
        .where(
            Book.status.in_([BookStatus.SOLD, BookStatus.WITHDRAWN]),
            Book.listings.any(
                (Listing.marketplace == marketplace)
                & (Listing.status == ListingStatus.WITHDRAWN)
            ),
        )
    ).all()

    if not books:
        return {"checked": 0, "fixed": 0}

    # Собираем ключи остатков (stock_key) для всех этих книг
    stock_keys = []
    book_by_key = {}
    for book in books:
        listing = next((l for l in book.listings if l.marketplace == marketplace), None)
        if listing and listing.stock_key:
            stock_keys.append(listing.stock_key)
            book_by_key[listing.stock_key] = (book, listing)

    if not stock_keys:
        return {"checked": 0, "fixed": 0}

    # Запрашиваем реальные остатки с площадки
    try:
        stocks = client.fetch_stocks(stock_keys)
    except MarketplaceError as exc:
        _log(
            db,
            marketplace=marketplace,
            action="reconcile_withdrawn",
            ok=False,
            message=f"Не удалось запросить остатки: {exc}",
        )
        return {"checked": 0, "fixed": 0}

    checked = len(stock_keys)
    fixed = 0

    # Проверяем каждую книгу: если остаток > 0, значит книга всё ещё продаётся
    for key, stock in stocks.items():
        if stock <= 0:
            continue  # всё в порядке, книга снята

        # Книга всё ещё продаётся, хотя должна быть снята
        book, listing = book_by_key.get(key, (None, None))
        if not book:
            continue

        _log(
            db,
            marketplace=marketplace,
            action="reconcile_withdrawn",
            ok=False,
            book_id=book.id,
            message=f"Книга {book.sku}: помечена как снятая, но остаток на {marketplace} = {stock}. Снимаем повторно.",
        )

        # Меняем статус лота обратно на ACTIVE, чтобы withdraw_book сработал
        listing.status = ListingStatus.ACTIVE
        book.status = BookStatus.IN_STOCK

        # Повторное снятие с новой логикой (с архивацией для Ozon)
        success = withdraw_book(db, book, marketplace)
        if success:
            fixed += 1
            _log(
                db,
                marketplace=marketplace,
                action="reconcile_withdrawn",
                ok=True,
                book_id=book.id,
                message=f"Книга {book.sku}: повторное снятие выполнено",
            )
        else:
            # withdraw_book уже записал ошибку в журнал
            pass

        # Обновляем статус книги после повторного снятия
        refresh_book_status(db, book)

    if fixed > 0:
        _log(
            db,
            marketplace=marketplace,
            action="reconcile_withdrawn",
            ok=True,
            message=f"Сверка снятых книг: проверено {checked}, исправлено {fixed}",
        )

    return {"checked": checked, "fixed": fixed}


def reconcile_all_marketplaces(db: Session) -> dict:
    """Сверить снятые книги на всех включённых площадках. Вызывается по расписанию."""
    results = {}
    for marketplace in ["ozon", "wildberries"]:
        result = reconcile_withdrawn_books(db, marketplace)
        if result["checked"] > 0 or result["fixed"] > 0:
            results[marketplace] = result
    return results
