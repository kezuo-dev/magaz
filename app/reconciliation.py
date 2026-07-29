"""Сверка фактического состояния книг на площадках с локальной базой.

Периодически проверяет книги, которые помечены как снятые/проданные, но могут
всё ещё висеть на площадке из-за сбоя API или неполного снятия (например,
обнулили остаток, но не заархивировали карточку Ozon).

ВАЖНО про признак «книга ещё продаётся». Остаток для этого не годится: у
проданной книги б/у Ozon резервирует единственный экземпляр под заказ
(present=1, reserved=1), поэтому доступный остаток = 0 ещё до отгрузки — и
незаархивированная карточка, видимая покупателям, выглядела бы «снятой».
Поэтому сначала спрашиваем площадку, какие карточки она реально показывает
«В продаже» (fetch_in_sale_ids), и лишь если площадка это не умеет —
откатываемся на остатки.
"""
from __future__ import annotations

from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.marketplaces import MarketplaceError, get_client, is_supported
from app.models import Book, BookStatus, Listing, ListingStatus, MarketplaceAccount, SyncLog, utcnow
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

    Для книг со статусом SOLD или WITHDRAWN спрашиваем площадку, какие карточки
    она всё ещё показывает «В продаже». Каждую найденную снимаем повторно (для
    Ozon это ещё и архивация карточки) и пишем в журнал.

    Возвращает статистику: {"checked": N, "fixed": M}.
    """
    client = _get_active_client(db, marketplace)
    if client is None:
        _log(
            db,
            marketplace=marketplace,
            action="reconcile_withdrawn",
            ok=True,
            message="Сверка снятых книг пропущена: площадка выключена или нет ключей",
        )
        return {"checked": 0, "fixed": 0}

    # Находим книги, которые должны быть сняты (статус SOLD или WITHDRAWN), но у
    # которых есть лот на этой площадке — неважно, ACTIVE или WITHDRAWN локально.
    # Ограничиваем 30 днями: очень старые снятые книги заведомо сняты с площадок,
    # и тянуть их тысячами в API нет смысла — только тормозим и засоряем журнал.
    cutoff = utcnow() - timedelta(days=30)
    books = db.scalars(
        select(Book)
        .options(selectinload(Book.listings))
        .where(
            Book.status.in_([BookStatus.SOLD, BookStatus.WITHDRAWN]),
            Book.updated_at >= cutoff,
            Book.listings.any(
                (Listing.marketplace == marketplace)
                & (Listing.status.in_([ListingStatus.ACTIVE, ListingStatus.WITHDRAWN]))
            ),
        )
    ).all()

    if not books:
        _log(
            db,
            marketplace=marketplace,
            action="reconcile_withdrawn",
            ok=True,
            message="Сверка снятых книг: снятых книг для проверки нет",
        )
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
        _log(
            db,
            marketplace=marketplace,
            action="reconcile_withdrawn",
            ok=True,
            message=f"Сверка снятых книг: у {len(books)} книг нет ключа остатка — проверить нечего",
        )
        return {"checked": 0, "fixed": 0}

    # Спрашиваем площадку, какие карточки реально видны покупателям. Остаток тут
    # не показатель: у проданной книги он 0 даже когда карточка не заархивирована.
    checked = len(stock_keys)
    try:
        still_selling = client.fetch_in_sale_ids(stock_keys)
        if still_selling is None:
            # Площадка не умеет отдавать видимость — откатываемся на остатки.
            stocks = client.fetch_stocks(stock_keys)
            still_selling = {k for k, stock in stocks.items() if stock > 0}
            method = "по остаткам"
        else:
            method = "по видимости карточек"
    except MarketplaceError as exc:
        _log(
            db,
            marketplace=marketplace,
            action="reconcile_withdrawn",
            ok=False,
            message=f"Не удалось проверить состояние карточек: {exc}",
        )
        return {"checked": 0, "fixed": 0}

    fixed = 0
    failed = 0

    for key in still_selling:
        book, listing = book_by_key.get(key, (None, None))
        if not book:
            continue

        _log(
            db,
            marketplace=marketplace,
            action="reconcile_withdrawn",
            ok=False,
            book_id=book.id,
            message=(
                f"Книга {book.sku}: помечена как снятая, но на {marketplace} всё ещё "
                f"в продаже. Снимаем повторно."
            ),
        )

        # Меняем статус лота обратно на ACTIVE, чтобы withdraw_book сработал.
        # Сохраняем старые статусы для отката при ошибке.
        old_listing_status = listing.status
        old_book_status = book.status
        listing.status = ListingStatus.ACTIVE
        book.status = BookStatus.IN_STOCK

        # Повторное снятие с новой логикой (с архивацией для Ozon)
        try:
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
                # withdraw_book уже записал причину ошибки в журнал отдельной строкой
                failed += 1

            # Обновляем статус книги после повторного снятия
            refresh_book_status(db, book)
        except Exception as exc:
            # Непредвиденная ошибка (не MarketplaceError) — откатываем статусы
            listing.status = old_listing_status
            book.status = old_book_status
            failed += 1
            _log(
                db,
                marketplace=marketplace,
                action="reconcile_withdrawn",
                ok=False,
                book_id=book.id,
                message=f"Книга {book.sku}: сбой при снятии — {exc}",
            )

    # Итог пишем ВСЕГДА, даже когда исправлять нечего: иначе после нажатия
    # «Проверить снятые» в журнале не остаётся никакого следа и непонятно,
    # выполнилась ли проверка вообще.
    summary = f"Сверка снятых книг ({method}): проверено {checked}, исправлено {fixed}"
    if failed:
        summary += f", не удалось снять {failed}"
    _log(
        db,
        marketplace=marketplace,
        action="reconcile_withdrawn",
        ok=failed == 0,
        message=summary,
    )

    return {"checked": checked, "fixed": fixed, "failed": failed}


def reconcile_all_marketplaces(db: Session) -> dict:
    """Сверить снятые книги на всех включённых площадках. Вызывается по расписанию."""
    results = {}
    for marketplace in ["ozon", "wildberries"]:
        results[marketplace] = reconcile_withdrawn_books(db, marketplace)
    return results
