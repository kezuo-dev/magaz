"""Сервис синхронизации каталога с площадками.

Здесь собрано всё, что «ходит наружу» из бизнес-логики:
- publish_book / withdraw_book — выставить/снять одну книгу на одной площадке;
- withdraw_book_everywhere — снять книгу со всех площадок (для авто-снятия);
- poll_marketplace_orders — опрос заказов и обработка продаж (кросс-снятие).

Правило деградации: если аккаунт площадки выключен или ключи не заданы, живого
вызова API не делаем — только меняем локальный статус лота и пишем это в журнал.
Так интерфейс полностью рабочий ещё до подключения реальных ключей.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.marketplaces import MarketplaceError, get_client, is_supported
from app.models import (
    Book,
    BookStatus,
    Listing,
    ListingStatus,
    MarketplaceAccount,
    Order,
    SyncLog,
)
from app.models import utcnow
from app.archive import mark_removed
from app.security import decrypt_credentials


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
    """Вернуть готовый клиент площадки или None, если площадка выключена/не настроена.

    None означает «работаем в офлайн-режиме» — меняем только локальный статус.
    """
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


def _get_or_create_listing(db: Session, book: Book, marketplace: str) -> Listing:
    listing = next((l for l in book.listings if l.marketplace == marketplace), None)
    if listing is None:
        listing = Listing(book_id=book.id, marketplace=marketplace, status=ListingStatus.PENDING)
        db.add(listing)
        book.listings.append(listing)
    return listing


def publish_book(db: Session, book: Book, marketplace: str) -> bool:
    """Выставить книгу на площадку. Возвращает True при успехе живого вызова.

    Если площадка выключена — помечаем лот ACTIVE локально и возвращаем False
    (никакого сетевого вызова не было).
    """
    listing = _get_or_create_listing(db, book, marketplace)
    client = _get_active_client(db, marketplace)

    if client is None:
        listing.status = ListingStatus.ACTIVE
        listing.last_error = None
        listing.last_synced_at = utcnow()
        _log(db, marketplace=marketplace, action="publish", ok=True, book_id=book.id,
             message="Локально (площадка выключена): лот отмечен активным")
        return False

    try:
        result = client.publish(book)
        listing.external_id = result.external_id or listing.external_id
        listing.status = ListingStatus.ACTIVE
        listing.last_error = None
        listing.last_synced_at = utcnow()
        _log(db, marketplace=marketplace, action="publish", ok=True, book_id=book.id,
             message=f"Выставлено на {marketplace}, лот {listing.external_id}")
        return True
    except MarketplaceError as exc:
        listing.status = ListingStatus.ERROR
        listing.last_error = str(exc)
        _log(db, marketplace=marketplace, action="publish", ok=False, book_id=book.id,
             message=str(exc))
        return False


def withdraw_book(db: Session, book: Book, marketplace: str) -> bool:
    """Снять книгу с одной площадки. True — если живой вызов прошёл успешно."""
    listing = next((l for l in book.listings if l.marketplace == marketplace), None)
    if listing is None:
        return False

    client = _get_active_client(db, marketplace)

    if client is None:
        listing.status = ListingStatus.WITHDRAWN
        listing.last_error = None
        listing.last_synced_at = utcnow()
        _log(db, marketplace=marketplace, action="withdraw", ok=True, book_id=book.id,
             message="Локально (площадка выключена): лот снят")
        return False

    try:
        client.withdraw(listing)
        listing.status = ListingStatus.WITHDRAWN
        listing.last_error = None
        listing.last_synced_at = utcnow()
        _log(db, marketplace=marketplace, action="withdraw", ok=True, book_id=book.id,
             message=f"Снято с {marketplace}")
        return True
    except MarketplaceError as exc:
        listing.status = ListingStatus.ERROR
        listing.last_error = str(exc)
        _log(db, marketplace=marketplace, action="withdraw", ok=False, book_id=book.id,
             message=str(exc))
        return False


def withdraw_book_everywhere(db: Session, book: Book, *, except_marketplace: str | None = None) -> None:
    """Снять книгу со всех площадок, кроме указанной (обычно — той, где продалась)."""
    for listing in list(book.listings):
        if except_marketplace and listing.marketplace == except_marketplace:
            continue
        if listing.status in (ListingStatus.WITHDRAWN,):
            continue
        withdraw_book(db, book, listing.marketplace)


def poll_marketplace_orders(db: Session, marketplace: str) -> int:
    """Опросить заказы площадки, обработать новые продажи. Возвращает число новых заказов.

    На каждый новый заказ: находим книгу по SKU (offer_id), помечаем sold,
    снимаем с остальных площадок. Дубли заказов отсекаем по (marketplace, order_id).
    """
    client = _get_active_client(db, marketplace)
    if client is None:
        return 0

    try:
        orders = client.fetch_orders()
    except MarketplaceError as exc:
        _log(db, marketplace=marketplace, action="poll_orders", ok=False, message=str(exc))
        return 0

    new_count = 0
    for info in orders:
        exists = db.scalar(
            select(Order).where(
                Order.marketplace == marketplace,
                Order.external_order_id == info.external_order_id,
            )
        )
        if exists:
            continue

        # Ищем книгу по SKU (мы используем SKU как offer_id на площадке).
        book = None
        if info.external_sku:
            book = db.scalar(select(Book).where(Book.sku == info.external_sku))

        order = Order(
            marketplace=marketplace,
            external_order_id=info.external_order_id,
            external_sku=info.external_sku,
            book_id=book.id if book else None,
            processed=False,
        )
        db.add(order)
        new_count += 1

        if book:
            book.status = BookStatus.SOLD
            mark_removed(book)  # запускаем окно до переноса в архив
            # Помечаем лот на этой площадке проданным и снимаем с остальных.
            sold_listing = next((l for l in book.listings if l.marketplace == marketplace), None)
            if sold_listing:
                sold_listing.status = ListingStatus.WITHDRAWN
                sold_listing.last_synced_at = utcnow()
            withdraw_book_everywhere(db, book, except_marketplace=marketplace)
            order.processed = True
            _log(db, marketplace=marketplace, action="order_sold", ok=True, book_id=book.id,
                 message=f"Заказ {info.external_order_id}: книга {book.sku} продана, снята с остальных площадок")
        else:
            _log(db, marketplace=marketplace, action="order_unmatched", ok=False,
                 message=f"Заказ {info.external_order_id}: книга по SKU «{info.external_sku}» не найдена")

    if new_count:
        _log(db, marketplace=marketplace, action="poll_orders", ok=True,
             message=f"Новых заказов: {new_count}")
    return new_count
