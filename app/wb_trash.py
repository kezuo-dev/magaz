"""Удаление снятых книг в корзину WB.

Проходит по книгам со статусом SOLD/WITHDRAWN, у которых есть лот WB с
остатком 0, и удаляет карточки в корзину небольшими пачками с паузами (чтобы не
схлопнуть лимит API 429). Вызывается по кнопке из UI или по расписанию.

Книги со свежим неотменённым заказом (моложе CANCEL_GRACE_DAYS) не трогаем:
заказ ещё могут отменить, и тогда карточку пришлось бы достать из корзины.
"""
from __future__ import annotations

import time
from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.marketplaces import MarketplaceError, get_client
from app.models import Book, BookStatus, Listing, MarketplaceAccount, Order, SyncLog, utcnow
from app.security import decrypt_credentials


# Сколько дней после заказа не трогаем карточку проданной книги: столько живёт
# риск отмены. Пока окно не вышло, карточка остаётся в кабинете WB — если заказ
# отменят, её не придётся достать из корзины.
CANCEL_GRACE_DAYS = 14


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


def move_withdrawn_to_trash(db: Session, days: int | None = 7) -> dict:
    """Удалить снятые книги в корзину WB. Возвращает {processed, deleted, failed}.

    days — ограничение по периоду: обрабатываем книги, обновлённые за последние
    N дней. None = без ограничения (все снятые книги за всё время).
    По умолчанию 7 дней — безопасный период, не схлопывает лимит API.
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

    # Одним запросом узнаём, у каких книг есть СВЕЖИЙ активный (не отменённый)
    # заказ. Раньше был N+1: отдельный SELECT для каждой книги.
    #
    # Почему именно свежий, а не любой: статус SOLD книге ставится (sync.py,
    # refresh_book_status) ТОЛЬКО когда у неё есть неотменённый заказ. Поэтому
    # «пропускать книги с любым активным заказом» отбрасывало все SOLD-книги
    # без исключения — условия взаимоисключающие, и половина выборки была
    # мёртвой: в корзину уходили только WITHDRAWN, а карточки проданных книг
    # оставались в кабинете WB навсегда.
    #
    # Смысл пропуска — переждать возможную отмену, а она приходит в первые дни.
    # Поэтому блокируем удаление только на время окна отмены, дальше карточку
    # проданной книги можно спокойно убирать.
    book_ids = [b.id for b in books]
    cancel_grace_cutoff = utcnow() - timedelta(days=CANCEL_GRACE_DAYS)
    active_order_book_ids: set[int] = set(
        db.scalars(
            select(Order.book_id).where(
                Order.book_id.in_(book_ids),
                Order.cancelled == False,  # noqa: E712
                Order.created_at >= cancel_grace_cutoff,
            ).distinct()
        ).all()
    )

    # Собираем nmID карточек для удаления
    to_delete = []
    for book in books:
        # Пропускаем книги со свежим заказом: он ещё может быть отменён, и тогда
        # карточку придётся достать из корзины обратно.
        if book.id in active_order_book_ids:
            continue

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
    skipped = 0  # не обработали из-за лимита (попробуем в следующий раз)

    # Удаляем небольшими пачками с паузами.
    # WB лимитирует этот эндпоинт жёстко: при 429 НЕ пробуем повторно и НЕ
    # разбиваем на единичные запросы — это только удваивает нагрузку. Просто
    # останавливаемся: остаток обработает следующий ночной запуск.
    BATCH_SIZE = 5
    PAUSE_SECONDS = 5

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
                _log(db, action="wb_trash", ok=True,
                     message=f"Карточка {nm} удалена в корзину WB")
        except MarketplaceError as exc:
            err = str(exc)
            # 429 — лимит. Останавливаемся, не множим запросы.
            if "429" in err or "лимит" in err.lower():
                skipped = len(to_delete) - i
                _log(db, action="wb_trash", ok=True,
                     message=f"Лимит WB: остановились после {deleted} удалений, "
                             f"отложено {skipped} карточек на следующий запуск")
                break
            # Другая ошибка — пишем и идём дальше
            for book, listing, nm in batch:
                failed += 1
                _log(db, action="wb_trash", ok=False,
                     message=f"Не удалось удалить карточку {nm} в корзину WB: {exc}")

        # Пауза между пачками (кроме последней)
        if i + BATCH_SIZE < len(to_delete):
            time.sleep(PAUSE_SECONDS)

    _log(db, action="wb_trash", ok=(failed == 0),
         message=f"Очистка корзины WB ({period_label}): обработано {len(to_delete)}, удалено {deleted}"
                 + (f", не удалось {failed}" if failed else "")
                 + (f", отложено {skipped}" if skipped else ""))

    return {"processed": len(to_delete), "deleted": deleted, "failed": failed, "skipped": skipped}
