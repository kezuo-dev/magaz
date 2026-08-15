"""Удаление снятых книг в корзину WB.

Проходит по книгам со статусом SOLD/WITHDRAWN, у которых есть лот WB, и удаляет
карточки в корзину пачками с паузами (чтобы не схлопнуть лимит API 429).

СТРАТЕГИЯ ОБРАБОТКИ:
- Сортировка по updated_at ASC (старые книги первыми) — FIFO, не накапливаем backlog
- Лимит за проход (MAX_BOOKS_PER_RUN) — не блокируем scheduler надолго
- Пачки по BATCH_SIZE карточек — баланс между скоростью и лимитами
- Пауза PAUSE_SECONDS между пачками — даём API WB остыть
- При 429 останавливаемся gracefully — остаток обработает следующий запуск

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

# Максимум книг за один проход. Защита от зависания scheduler'а: если backlog
# огромный (тысячи книг), не обрабатываем их все за раз — возьмём порцию, а
# остальное заберёт следующий запуск. 100 книг × 2 секунды на пачку = ~3 минуты.
MAX_BOOKS_PER_RUN = 100

# Размер пачки для одного DELETE-запроса к WB. API принимает массив nmID,
# лимит неизвестен, но 30 работает стабильно (проверено).
BATCH_SIZE = 30

# Пауза между пачками. WB жёстко лимитирует DELETE — даём API остыть.
# 2 секунды — баланс между скоростью и надёжностью.
PAUSE_SECONDS = 2


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


def move_withdrawn_to_trash(
    db: Session,
    limit: int | None = None,
    verbose: bool = True,
) -> dict:
    """Удалить снятые книги в корзину WB. Возвращает {processed, deleted, failed}.

    limit — максимум книг за проход. None = применяется MAX_BOOKS_PER_RUN.
    Защита от зависания: если backlog огромный, берём порцию, остальное — в
    следующий раз. FIFO (старые книги первыми) гарантирует, что backlog не растёт.

    verbose — писать в журнал даже когда удалять нечего. Ручной запуск из UI
    ставит True (пользователь нажал кнопку и ждёт отчёта), автозапуск по
    расписанию — False: иначе каждые 10 минут в журнал падает один и тот же
    «нечего удалять», и за сутки набегает 144 бесполезные записи, в которых
    тонут настоящие ошибки.
    """
    # Проверяем настройки WB
    account = db.scalar(
        select(MarketplaceAccount).where(MarketplaceAccount.marketplace == "wildberries")
    )
    if not account or not account.enabled or not account.credentials_encrypted:
        if verbose:
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

    # Находим снятые книги с лотом WB. КЛЮЧЕВОЕ ИЗМЕНЕНИЕ: сортируем по
    # updated_at ASC (старые первыми) и берём limit — это FIFO-очередь, которая
    # гарантирует, что backlog постепенно рассасывается, а не накапливается.
    #
    # Старая логика (hours=3) создавала скользящее окно: книги старше 3 часов
    # пропадали из выборки навсегда, даже если их не успели удалить. При большом
    # потоке продаж (> 30 книг/час) backlog рос без границ.
    max_books = limit if limit is not None else MAX_BOOKS_PER_RUN
    query = (
        select(Book)
        .options(selectinload(Book.listings))
        .where(
            Book.status.in_([BookStatus.SOLD, BookStatus.WITHDRAWN]),
            Book.listings.any(Listing.marketplace == "wildberries"),
        )
        .order_by(Book.updated_at.asc())  # FIFO: старые книги первыми
        .limit(max_books)
    )

    books = db.scalars(query).all()

    if not books:
        if verbose:
            _log(db, action="wb_trash", ok=True,
                 message=f"Очистка корзины WB: снятых книг для удаления нет")
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
    no_nm_id: list[str] = []  # SKU книг без nmID — для диагностического лога
    for book in books:
        # Пропускаем книги со свежим заказом: он ещё может быть отменён, и тогда
        # карточку придётся достать из корзины обратно.
        if book.id in active_order_book_ids:
            continue

        listing = next((l for l in book.listings if l.marketplace == "wildberries"), None)
        if not listing or not listing.external_id:
            no_nm_id.append(f"{book.sku} (нет external_id)")
            continue
        try:
            nm_id = int(listing.external_id)
            to_delete.append((book, listing, nm_id))
        except (ValueError, TypeError):
            # external_id не число (старый vendorCode) — пропускаем
            no_nm_id.append(f"{book.sku} (vendorCode={listing.external_id})")
            continue

    if not to_delete:
        # Книги без nmID удалить нельзя (API требует именно nmID). Их карточек уже
        # нет в каталоге WB — сверка каталога не может подтянуть им nmID. Молчим
        # при автозапуске: причина не исчезнет сама, а повторять её каждые 10
        # минут — только засорять журнал.
        if verbose:
            skus = ", ".join(no_nm_id[:10]) if no_nm_id else "—"
            if len(no_nm_id) > 10:
                skus += f"… (всего {len(no_nm_id)})"
            _log(db, action="wb_trash", ok=True,
                 message=f"Очистка корзины WB: у {len(books)} книг нет nmID для удаления: {skus}")
        return {"processed": 0, "deleted": 0, "failed": 0, "no_nm_id": len(no_nm_id)}

    deleted = 0
    failed = 0
    skipped = 0  # не обработали из-за лимита (попробуем в следующий раз)

    # Удаляем пачками с паузами. Увеличены размеры пачек (5 → 30) и уменьшены
    # паузы (5с → 2с) для ускорения обработки при большом потоке продаж.
    #
    # WB лимитирует этот эндпоинт жёстко: при 429 НЕ пробуем повторно и НЕ
    # разбиваем на единичные запросы — это только удваивает нагрузку. Просто
    # останавливаемся: остаток обработает следующий запуск через 10 минут.

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
                # ВАЖНО: всегда логируем удалённые карточки, чтобы можно было проверить
                _log(db, action="wb_trash", ok=True, book_id=book.id,
                     message=f"Карточка {nm} ({book.sku}) удалена в корзину WB")
        except MarketplaceError as exc:
            err = str(exc)
            # 429 — лимит. Останавливаемся, не множим запросы.
            if "429" in err or "лимит" in err.lower():
                skipped = len(to_delete) - i  # всё, что осталось (включая текущую пачку)
                _log(db, action="wb_trash", ok=True,
                     message=f"Лимит WB при удалении в корзину: остановились, "
                             f"отложено {skipped} карточек")
                break
            # Другая ошибка — пишем и идём дальше
            for book, listing, nm in batch:
                failed += 1
                _log(db, action="wb_trash", ok=False, book_id=book.id,
                     message=f"Не удалось удалить карточку {nm} ({book.sku}) в корзину WB: {exc}")

        # Пауза между пачками (кроме последней)
        if i + BATCH_SIZE < len(to_delete):
            time.sleep(PAUSE_SECONDS)

    # Итог пишем при ручном запуске всегда, при автозапуске — только если реально
    # что-то произошло (удалили, не смогли или отложили по лимиту).
    # "Обработано" = реально отправлено в API (удалено + не удалось), без отложенных.
    processed = deleted + failed
    if verbose or deleted or failed or skipped:
        msg_parts = []
        if deleted:
            msg_parts.append(f"удалено {deleted}")
        if failed:
            msg_parts.append(f"не удалось {failed}")
        if skipped:
            msg_parts.append(f"отложено {skipped} (лимит WB)")

        message = f"Очистка корзины WB: {', '.join(msg_parts)}"
        _log(db, action="wb_trash", ok=(failed == 0), message=message)

    return {"processed": processed, "deleted": deleted, "failed": failed, "skipped": skipped}
