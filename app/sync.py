"""Сервис синхронизации каталога с площадками.

Здесь собрано всё, что «ходит наружу» из бизнес-логики:
- withdraw_book — снять одну книгу с одной площадки;
- withdraw_book_everywhere — снять книгу со всех площадок (для авто-снятия);
- poll_marketplace_orders — опрос заказов и обработка продаж (кросс-снятие);
- process_cancelled_orders — обработка отменённых заказов (восстановление книги).

Выставление книг убрано: программа только отслеживает каталог площадок и снимает
проданное. Наполнение каталога идёт сверкой (см. app/catalog_sync.py).

Правило деградации: если аккаунт площадки выключен или ключи не заданы, живого
вызова API не делаем — только меняем локальный статус лота и пишем это в журнал.
"""
from __future__ import annotations

from datetime import timedelta, timezone

from sqlalchemy import select, or_
from sqlalchemy.orm import Session, selectinload

from app.marketplaces import MarketplaceError, get_client, is_supported
from app.marketplaces.base import CancelledOrderInfo
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
from app.security import decrypt_credentials


def refresh_book_status(db: Session, book: Book) -> str:
    """Пересчитать статус книги из состояния её лотов. Единая точка истины.

    Правила (программа только зеркалит площадки, поэтому статусов три):
    - есть хотя бы один активный лот  → IN_STOCK («В продаже»);
    - активных лотов нет и по книге ЕСТЬ заказ → SOLD («Продана»);
    - активных лотов нет и заказа не было → WITHDRAWN («Снята»).

    Раньше статус зависел от того, КАКОЙ механизм заметил уход с продажи: опрос
    заказов ставил SOLD, а слежение за остатками — WITHDRAWN, хотя это была одна
    и та же продажа. Отсюда бралась путаница «снято/продано».

    Присваивает book.status только если статус действительно изменился — тогда
    onupdate=utcnow обновит updated_at. Иначе updated_at остаётся нетронутым.
    """
    still_active = any(l.status == ListingStatus.ACTIVE for l in book.listings)
    if still_active:
        new_status = BookStatus.IN_STOCK
    else:
        # Заказ — доказательство продажи, независимо от того, кто её обнаружил.
        # Но отменённые заказы не считаются: книга с отменённым заказом не продана.
        has_order = db.scalar(
            select(Order.id).where(Order.book_id == book.id, Order.cancelled == False).limit(1)  # noqa: E712
        ) is not None
        new_status = BookStatus.SOLD if has_order else BookStatus.WITHDRAWN

    # Присваиваем только если статус изменился — иначе onupdate не сработает.
    if book.status != new_status:
        book.status = new_status
    return book.status


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


def withdraw_book(db: Session, book: Book, marketplace: str, *, use_sell: bool = False) -> bool:
    """Снять книгу с одной площадки. True — если живой вызов прошёл успешно.

    use_sell=True — использовать sell() вместо withdraw(): обнуляет остаток без
    архивации Ozon. Нужно для сверки снятых книг, чтобы не архивировать карточки.
    """
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
        if use_sell:
            client.sell(listing)
        else:
            client.withdraw(listing)
        listing.status = ListingStatus.WITHDRAWN
        listing.last_error = None
        listing.last_synced_at = utcnow()
        msg = f"Снято с {marketplace}"
        if client.last_warning:
            msg += f". {client.last_warning}"
        _log(db, marketplace=marketplace, action="withdraw", ok=True, book_id=book.id,
             message=msg)
        return True
    except MarketplaceError as exc:
        listing.status = ListingStatus.ERROR
        listing.last_error = str(exc)
        _log(db, marketplace=marketplace, action="withdraw", ok=False, book_id=book.id,
             message=str(exc))
        return False


def withdraw_book_everywhere(db: Session, book: Book, *, except_marketplace: str | None = None) -> bool:
    """Снять книгу со всех площадок, кроме указанной (обычно — той, где продалась).

    Единая точка авто-снятия для всех механизмов (заказы, сверка, слежение).
    Если рубильник «Автоснятие» в Настройках выключен — ничего не делаем: лоты на
    других площадках остаются активными (книга там реально продолжает продаваться),
    а в журнал пишется пропуск, чтобы было видно, что автоматика заметила продажу.

    Возвращает True, если снятие фактически завершено (сняли все цели ИЛИ снимать
    было нечего). False — если были активные лоты, но снятие пропущено из-за
    выключенного рубильника: вызывающий не должен считать продажу до конца
    обработанной, чтобы позже (при включённом рубильнике) её можно было отзеркалить.
    """
    from app.flags import is_auto_withdraw_enabled  # локальный импорт против цикла

    targets = [
        l for l in book.listings
        if not (except_marketplace and l.marketplace == except_marketplace)
        and l.status not in (ListingStatus.WITHDRAWN,)
    ]
    if not targets:
        return True

    if not is_auto_withdraw_enabled(db):
        _log(db, marketplace=None, action="withdraw_skipped", ok=True, book_id=book.id,
             message=f"Книга {book.sku}: автоснятие выключено — лоты на других площадках не тронуты")
        return False

    for listing in targets:
        withdraw_book(db, book, listing.marketplace)
    return True


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
        # В одном отправлении может быть НЕСКОЛЬКО книг — площадка отдаёт их
        # отдельными строками с ОДИНАКОВЫМ номером заказа. Если дедуплицировать
        # только по номеру, вторая и последующие книги молча отбрасывались бы и
        # не снимались с других площадок (прямой риск двойной продажи). Поэтому
        # ключ заказа = номер + артикул: одна строка на каждую проданную книгу.
        order_key = info.external_order_id
        if info.external_sku:
            order_key = f"{info.external_order_id}#{info.external_sku}"

        exists = db.scalar(
            select(Order.id).where(
                Order.marketplace == marketplace,
                Order.external_order_id == order_key,
            ).limit(1)
        ) is not None
        if exists:
            continue

        # Ищем книгу по SKU (мы используем SKU как offer_id на площадке).
        # Предзагружаем лоты, чтобы кросс-снятие видело все площадки.
        book = None
        if info.external_sku:
            book = db.scalar(
                select(Book).options(selectinload(Book.listings)).where(Book.sku == info.external_sku)
            )

        order = Order(
            marketplace=marketplace,
            external_order_id=order_key,
            external_sku=info.external_sku,
            book_id=book.id if book else None,
            processed=False,
        )
        db.add(order)
        new_count += 1

        if book:
            # Снимаем книгу со ВСЕХ площадок (включая ту, где продали).
            # Рубильников два, и они разные:
            #   sync_enabled  — трогаем ли площадки вообще. Выключен: продажа
            #                   только пишется в БД (аналитика живая), API молчит.
            #   auto_withdraw — снимать ли книгу с ОСТАЛЬНЫХ площадок. Выключен:
            #                   лот площадки продажи снимаем (там уже продано и
            #                   так), а на других книга продолжает продаваться —
            #                   ровно это обещает подпись тумблера в Настройках.
            from app.flags import is_auto_withdraw_enabled, is_sync_enabled
            do_sell = is_sync_enabled(db)
            cross_allowed = do_sell and is_auto_withdraw_enabled(db)
            # Пропущенное кросс-снятие не даёт считать продажу обработанной:
            # заказ останется processed=False, и после включения рубильника
            # продажу можно будет отзеркалить.
            cross_done = True
            for listing in book.listings:
                if listing.status == ListingStatus.ACTIVE:
                    if not do_sell:
                        # Рубильник выключен — только пишем в журнал, не снимаем
                        _log(db, marketplace=listing.marketplace, action="sell", ok=True, book_id=book.id,
                             message=f"Продажа зафиксирована, но синхронизация выключена — лот {listing.marketplace} не тронут")
                        continue
                    if listing.marketplace != marketplace and not cross_allowed:
                        cross_done = False
                        _log(db, marketplace=listing.marketplace, action="withdraw_skipped", ok=True, book_id=book.id,
                             message=f"Книга {book.sku}: автоснятие выключено — лот {listing.marketplace} не тронут")
                        continue
                    client = _get_active_client(db, listing.marketplace)
                    if client is None:
                        listing.status = ListingStatus.WITHDRAWN
                        listing.last_error = None
                        listing.last_synced_at = utcnow()
                        _log(db, marketplace=listing.marketplace, action="sell", ok=True, book_id=book.id,
                             message="Локально (площадка выключена): лот снят после продажи")
                    else:
                        try:
                            client.sell(listing)
                            listing.status = ListingStatus.WITHDRAWN
                            listing.last_error = None
                            listing.last_synced_at = utcnow()
                            msg = f"Снято с {listing.marketplace} после продажи на {marketplace}"
                            if client.last_warning:
                                msg += f". {client.last_warning}"
                            _log(db, marketplace=listing.marketplace, action="sell", ok=True, book_id=book.id,
                                 message=msg)
                        except MarketplaceError as exc:
                            listing.status = ListingStatus.ERROR
                            listing.last_error = str(exc)
                            _log(db, marketplace=listing.marketplace, action="sell", ok=False, book_id=book.id,
                                 message=f"Не удалось снять с {listing.marketplace}: {exc}")

            # flush нужен, чтобы refresh_book_status увидел только что добавленный
            # заказ (он ищет его в базе) и поставил «Продана», а не «Снята».
            db.flush()
            refresh_book_status(db, book)
            # Обработанным считаем только заказ, по которому сделано всё, что
            # должны были: сама продажа проведена и кросс-снятие не пропущено.
            order.processed = do_sell and cross_done
            _log(db, marketplace=marketplace, action="order_sold", ok=True, book_id=book.id,
                 message=f"Заказ {info.external_order_id}: книга {book.sku} продана на {marketplace}")
        else:
            _log(db, marketplace=marketplace, action="order_unmatched", ok=False,
                 message=f"Заказ {info.external_order_id}: книга по SKU «{info.external_sku}» не найдена")

    if new_count:
        _log(db, marketplace=marketplace, action="poll_orders", ok=True,
             message=f"Новых заказов: {new_count}")
    return new_count


def _as_utc(dt):
    """Привести datetime из БД к aware-UTC. None остаётся None.

    SQLite отдаёт naive-значения, Postgres — aware. Сравнивать их между собой
    напрямую нельзя (TypeError), поэтому наивные считаем UTC: мы всегда пишем
    время через utcnow(), других источников у этих колонок нет.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _withdrawn_by_order(listing: Listing, order: Order) -> bool:
    """Похоже ли, что лот сняли именно из-за этого заказа.

    Возвращать в продажу при отмене нужно только те лоты, которые сняла эта
    продажа. Лот, снятый раньше (вручную владельцем, слежением за остатками или
    сверкой каталога), отменой чужого заказа трогать нельзя — иначе программа
    сама возвращает на площадку товар, который сознательно убрали.

    Признак: отметка синхронизации лота не раньше момента появления заказа (с
    запасом в минуту на разбег часов между записями одного прохода). Если у лота
    отметки нет вообще, считаем, что снят не этой продажей: продажа её всегда
    проставляет.
    """
    synced = _as_utc(listing.last_synced_at)
    created = _as_utc(order.created_at)
    if synced is None or created is None:
        return False
    return synced >= created - timedelta(minutes=1)


def process_cancelled_orders(db: Session, marketplace: str) -> int:
    """Обработать отменённые заказы площадки. Возвращает число обработанных отмен.

    Если заказ отменён ДО отгрузки — книга возвращается в продажу (восстанавливаем
    лот через API и ставим IN_STOCK). Если заказ отменён ПОСЛЕ отгрузки (книга уже
    в сортцентре/в пути) — помечаем заказ отменённым, но книгу НЕ трогаем: физически
    её у нас нет, восстанавливать нечего.
    """
    client = _get_active_client(db, marketplace)
    if client is None:
        return 0

    try:
        cancelled_infos = client.fetch_cancelled_orders()
    except MarketplaceError as exc:
        _log(db, marketplace=marketplace, action="poll_cancellations", ok=False, message=str(exc))
        return 0

    if not cancelled_infos:
        return 0

    processed_count = 0
    for info in cancelled_infos:
        order_id = info.external_order_id
        already_shipped = info.already_shipped

        orders = db.scalars(
            select(Order).options(selectinload(Order.book).selectinload(Book.listings))
            .where(
                Order.marketplace == marketplace,
                Order.cancelled == False,  # noqa: E712
                or_(
                    Order.external_order_id == order_id,
                    Order.external_order_id.startswith(order_id + "#"),
                ),
            )
        ).all()

        for order in orders:
            if not order.book:
                # Заказ без книги — просто помечаем отменённым.
                order.cancelled = True
                continue

            book = order.book

            if already_shipped:
                # Книга уже передана в доставку: физически её у нас нет, и на эту
                # карточку она больше никогда не вернётся. Возвраты идут в работу как
                # новые книги с другим артикулом — поэтому помечаем лот площадки
                # «удалён из продажи»: следующая сверка не поднимет карточку в ACTIVE
                # даже если площадка снова покажет её «В продаже» (остаток вернулся
                # после возврата), а принудительно её сожмёт (Ozon — архив, WB — корзина).
                order.cancelled = True
                processed_count += 1
                # Ставим WITHDRAWN на всякий случай (в старых базах до этого фикса
                # лот мог остаться ACTIVE — площадка сама вернула карточку в продажу).
                # Переводим в WITHDRAWN немедленно, чтобы refresh_book_status не поднял
                # книгу обратно в IN_STOCK.
                for listing in book.listings:
                    if listing.marketplace == marketplace:
                        if listing.status == ListingStatus.ACTIVE:
                            listing.status = ListingStatus.WITHDRAWN
                            listing.last_synced_at = utcnow()
                        if not listing.removed_from_sale:
                            listing.removed_from_sale = True
                _log(db, marketplace=marketplace, action="order_cancelled",
                     ok=True, book_id=book.id,
                     message=(
                         f"Заказ {order_id} отменён ПОСЛЕ отгрузки: карточка {book.sku} "
                         f"на {marketplace} помечена «удалена из продажи» — "
                         f"при возврате она никогда не вернётся на этот артикул."
                     ))
                # Пересчитываем статус книги: лот WITHDRAWN + заказ cancelled → WITHDRAWN.
                refresh_book_status(db, book)
                continue

            # Заказ отменён до отгрузки — восстанавливаем лоты и возвращаем книгу.
            for listing in book.listings:
                if listing.status not in (ListingStatus.WITHDRAWN, ListingStatus.ERROR):
                    continue
                # Восстанавливаем ТОЛЬКО лоты, снятые этой продажей. Раньше цикл
                # брал все снятые лоты книги без разбора: лот, снятый владельцем
                # вручную месяц назад и уже удалённый в корзину WB, при отмене
                # чужого Ozon-заказа доставался обратно и снова выставлялся на
                # продажу. Признак «снят этой продажей» — отметка синхронизации
                # лота не раньше момента появления заказа.
                if not _withdrawn_by_order(listing, order):
                    _log(db, marketplace=listing.marketplace, action="order_cancelled",
                         ok=True, book_id=book.id,
                         message=(
                             f"Заказ {order_id} отменён: лот {listing.marketplace} снят "
                             f"не этой продажей — оставлен как есть"
                         ))
                    continue
                restore_client = _get_active_client(db, listing.marketplace)
                if restore_client is None:
                    # Площадка выключена — только локальный статус, без API.
                    listing.status = ListingStatus.ACTIVE
                    listing.last_synced_at = utcnow()
                    listing.last_error = None
                    _log(db, marketplace=listing.marketplace, action="order_cancelled",
                         ok=True, book_id=book.id,
                         message=f"Заказ {order_id} отменён: лот {listing.marketplace} возвращён локально (площадка выключена)")
                    continue
                # ACTIVE взводим только ПОСЛЕ удачного вызова: иначе при сбое
                # (429/сеть) в базе остался бы активный лот, которого на площадке
                # нет — каталог показывал бы «В продаже» книгу, купить которую нельзя.
                try:
                    restore_client.restore(listing)
                except MarketplaceError as exc:
                    listing.last_error = str(exc)
                    _log(db, marketplace=listing.marketplace, action="order_cancelled",
                         ok=False, book_id=book.id,
                         message=f"Заказ {order_id} отменён, но вернуть карточку {book.sku} на {listing.marketplace} не удалось: {exc}")
                    continue
                listing.status = ListingStatus.ACTIVE
                listing.last_synced_at = utcnow()
                listing.last_error = None
                msg = f"Заказ {order_id} отменён: карточка {book.sku} восстановлена на {listing.marketplace}"
                if restore_client.last_warning:
                    msg += f". {restore_client.last_warning}"
                _log(db, marketplace=listing.marketplace, action="order_cancelled",
                     ok=True, book_id=book.id, message=msg)

            refresh_book_status(db, book)
            order.cancelled = True
            processed_count += 1
            _log(db, marketplace=marketplace, action="order_cancelled", ok=True, book_id=book.id,
                 message=f"Заказ {order_id} отменён: книга {book.sku} восстановлена в продажу")

    if processed_count:
        _log(db, marketplace=marketplace, action="poll_cancellations", ok=True,
             message=f"Обработано отмен: {processed_count}")
    return processed_count
