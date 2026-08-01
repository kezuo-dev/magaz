"""Удаление снятых книг в корзину WB.

Проходит по книгам со статусом SOLD/WITHDRAWN, у которых есть лот WB с
остатком 0, и удаляет карточки в корзину небольшими пачками с паузами (чтобы не
схлопнуть лимит API 429). Вызывается по кнопке из UI или по расписанию.
"""
from __future__ import annotations

import time
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.marketplaces import MarketplaceError, get_client
from app.models import Book, BookStatus, Listing, MarketplaceAccount, SyncLog, utcnow
from app.security import decrypt_credentials


def _log(db: Session, *, action, ok, message, book_id=None) -> None:
    db.add(
        SyncLog(
            marketplace="wildberries",
            book_id=book_id,
            action=action,
            ok=ok,
            message=message,
        )
    )


def move_withdrawn_to_trash(db: Session, days: int | None = 90) -> dict:
    """Удалить снятые книги в корзину WB. Возвращает {processed, deleted, failed}.

    days — ограничение по периоду: обрабатываем книги, обновлённые за последние
    N дней. None = без ограничения (все снятые книги за всё время).
    По умолчанию 90 дней — свежие снятия, не обрабатываем всю историю за раз.
    """
    # Проверяем настройки WB
    account = db.scalar(
        select(MarketplaceAccount).where(MarketplaceAccount.marketplace == "wildberries")
    )
    if not account or not account.enabled or not account.credentials_encrypted:
        _log(db, action="wb_trash", ok=True,
             message="Очистка корзины WB пропущена: площадка выключена или нет ключей")
        return {"processed": 0, "deleted": 0, "failed": 0}

    try:
        creds = decrypt_credentials(account.credentials_encrypted)
        client = get_client("wildberries", creds)
    except (MarketplaceError, Exception) as exc:
        _log(db, action="wb_trash", ok=False,
             message=f"Не удалось подключиться к WB: {exc}")
        return {"processed": 0, "deleted": 0, "failed": 0}

    # Находим снятые книги с лотом WB
    query = (
        select(Book)
        .options(selectinload(Book.listings))
        .where(
            Book.status.in_([BookStatus.SOLD, BookStatus.WITHDRAWN]),
            Book.listings.any(Listing.marketplace == "wildberries"),
        )
    )
    if days is not None:
        cutoff = utcnow() - timedelta(days=days)
        query = query.where(Book.updated_at >= cutoff)

    books = db.scalars(query).all()

    period_label = f"за последние {days} дн." if days else "за всё время"

    if not books:
        _log(db, action="wb_trash", ok=True,
             message=f"Очистка корзины WB ({period_label}): снятых книг для удаления нет")
        return {"processed": 0, "deleted": 0, "failed": 0}

    # Собираем nmID карточек для удаления
    to_delete = []
    for book in books:
        listing = next((l for l in book.listings if l.marketplace == "wildberries"), None)
        if not listing or not listing.external_id:
            continue
        try:
            nm_id = int(listing.external_id)
            to_delete.append((book, listing, nm_id))
        except (ValueError, TypeError):
            # external_id не число (старый vendorCode) — пропускаем
            continue

    if not to_delete:
        _log(db, action="wb_trash", ok=True,
             message=f"Очистка корзины WB ({period_label}): у {len(books)} книг нет nmID для удаления")
        return {"processed": 0, "deleted": 0, "failed": 0}

    deleted = 0
    failed = 0

    # Удаляем небольшими пачками с паузами (чтобы не схлопнуть лимит 429)
    BATCH_SIZE = 20
    PAUSE_SECONDS = 2

    for i in range(0, len(to_delete), BATCH_SIZE):
        batch = to_delete[i:i + BATCH_SIZE]
        nm_ids = [nm for _, _, nm in batch]

        try:
            client._post(
                "https://content-api.wildberries.ru/content/v2/cards/delete/trash",
                {"nmIDs": nm_ids},
            )
            for book, listing, nm in batch:
                deleted += 1
                _log(db, action="wb_trash", ok=True, book_id=book.id,
                     message=f"Карточка {nm} удалена в корзину WB")
        except MarketplaceError as exc:
            # Вся пачка не прошла — пробуем по одной
            for book, listing, nm in batch:
                try:
                    client._post(
                        "https://content-api.wildberries.ru/content/v2/cards/delete/trash",
                        {"nmIDs": [nm]},
                    )
                    deleted += 1
                    _log(db, action="wb_trash", ok=True, book_id=book.id,
                         message=f"Карточка {nm} удалена в корзину WB")
                except MarketplaceError as e:
                    failed += 1
                    _log(db, action="wb_trash", ok=False, book_id=book.id,
                         message=f"Не удалось удалить карточку {nm} в корзину WB: {e}")

        # Пауза между пачками (кроме последней)
        if i + BATCH_SIZE < len(to_delete):
            time.sleep(PAUSE_SECONDS)

    _log(db, action="wb_trash", ok=failed == 0,
         message=f"Очистка корзины WB ({period_label}): обработано {len(to_delete)}, удалено {deleted}, не удалось {failed}")

    return {"processed": len(to_delete), "deleted": deleted, "failed": failed}
