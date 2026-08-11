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

ПРАВИЛО ДОКАЗАТЕЛЬСТВА. Эта сверка не наблюдает, а ПИШЕТ на площадку: обнуляет
остаток живой карточки. Поэтому она действует только там, где продажа
подтверждена заказом (Book.status == SOLD и в таблице заказов есть неотменённый
Order). Книги со статусом WITHDRAWN она больше НЕ трогает: этот статус ставится
и по наблюдению (остаток 0, карточка пропала из выгрузки), а наблюдение бывает
ошибочным — площадка отдаёт нулевой остаток по свежей карточке или неполный
ответ по остаткам.

Пока сверка верила WITHDRAWN, ошибка наблюдения превращалась в реальную потерю
остатка: владелец восстанавливал остаток руками, через 10 минут сверка
«исправляла» площадку по неверной базе, и так по кругу. Полная сверка каталога
вылечила бы статус обратно в ACTIVE, но она ходит раз в час, а эта — раз в 10
минут, и всегда успевала первой.

Цена отказа от WITHDRAWN: если владелец снял книгу вручную и архивация карточки
Ozon не прошла, карточка может остаться видимой с нулевым остатком. Купить такую
книгу нельзя (остаток 0), так что это косметика — а вот обнулить остаток живой
книги значит снять с продажи товар, который продаётся.
"""
from __future__ import annotations

from datetime import timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.marketplaces import MarketplaceError, get_client, is_supported
from app.models import (
    Book,
    BookStatus,
    Listing,
    ListingStatus,
    MarketplaceAccount,
    Order,
    SyncLog,
    utcnow,
)
from app.security import decrypt_credentials
from app.sync import refresh_book_status, withdraw_book

# Сколько карточек за один проход сверка вправе доснять. Больше — это уже не
# череда сбоев API, а рассинхрон базы с площадкой: обнулять остатки пачкой в
# такой ситуации нельзя, пусть сначала посмотрит человек. Порог абсолютный, а не
# в долях каталога: на 50k книг любая доля выглядит незаметной, а сотня
# обнулённых живых карточек — уже потеря товара.
MAX_REWITHDRAW_PER_RUN = 20


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


def reconcile_withdrawn_books(db: Session, marketplace: str, verbose: bool = True) -> dict:
    """Сверить ПРОДАННЫЕ книги с реальным состоянием на площадке.

    Берём только книги со статусом SOLD, у которых есть неотменённый заказ, и
    спрашиваем площадку, какие карточки она всё ещё показывает «В продаже».
    Каждую найденную снимаем повторно (обнуляем остаток) и пишем в журнал.
    Книги со статусом WITHDRAWN не трогаем — см. «правило доказательства» в
    docstring модуля.

    verbose — писать в журнал итог, даже когда исправлять нечего. Ручной запуск
    из UI ставит True (пользователь нажал кнопку и ждёт отчёта), автозапуск по
    расписанию — False: две площадки каждые 10 минут дают ~288 записей
    «проверять нечего» в сутки, в которых тонут настоящие ошибки.

    Возвращает статистику: {"checked": N, "fixed": M}.
    """
    client = _get_active_client(db, marketplace)
    if client is None:
        if verbose:
            _log(
                db,
                marketplace=marketplace,
                action="reconcile_withdrawn",
                ok=True,
                message="Сверка снятых книг пропущена: площадка выключена или нет ключей",
            )
        return {"checked": 0, "fixed": 0}

    # Находим ПРОДАННЫЕ книги, у которых есть лот на этой площадке — неважно,
    # ACTIVE или WITHDRAWN локально.
    #
    # Только SOLD и только с неотменённым заказом: обнуление остатка — запись на
    # площадку, и права на неё даёт лишь подтверждённая продажа. WITHDRAWN сюда
    # не входит (ставится по наблюдению, которое бывает ошибочным), проверка
    # заказа стоит отдельно от статуса, потому что статус — производная величина:
    # refresh_book_status выводит SOLD из «нет активных лотов + есть заказ», и
    # если заказ потом отменят, статус может не пересчитаться сразу.
    #
    # Ограничиваем 30 днями: очень старые проданные книги заведомо сняты с
    # площадок, и тянуть их тысячами в API нет смысла.
    cutoff = utcnow() - timedelta(days=30)
    books = db.scalars(
        select(Book)
        .options(selectinload(Book.listings))
        .where(
            Book.status == BookStatus.SOLD,
            Book.updated_at >= cutoff,
            Book.orders.any(Order.cancelled == False),  # noqa: E712
            Book.listings.any(
                (Listing.marketplace == marketplace)
                & (Listing.status.in_([ListingStatus.ACTIVE, ListingStatus.WITHDRAWN, ListingStatus.ERROR]))
            ),
        )
    ).all()

    if not books:
        if verbose:
            _log(
                db,
                marketplace=marketplace,
                action="reconcile_withdrawn",
                ok=True,
                message="Сверка проданных книг: проданных книг для проверки нет",
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
        # Молчим при автозапуске: отсутствие stock_key само не исчезнет, и
        # повторять этот вывод каждые 10 минут — только засорять журнал.
        if verbose:
            _log(
                db,
                marketplace=marketplace,
                action="reconcile_withdrawn",
                ok=True,
                message=f"Сверка проданных книг: у {len(books)} книг нет ключа остатка — проверить нечего",
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

    # Предохранитель на объём. Одна-две карточки «продана, но ещё висит» — это
    # обычный недоснятый остаток после сбоя API. Десятки за один проход означают,
    # что база разошлась с площадкой, и доснимать их пачкой нельзя: если разошлась
    # база, мы обнулим остатки живых книг. Останавливаемся и пишем в журнал ошибку,
    # чтобы человек увидел это в UI.
    targets = [key for key in still_selling if book_by_key.get(key)]
    if len(targets) > MAX_REWITHDRAW_PER_RUN:
        skus = ", ".join(book_by_key[k][0].sku for k in targets[:10])
        _log(
            db,
            marketplace=marketplace,
            action="reconcile_withdrawn",
            ok=False,
            message=(
                f"Сверка проданных книг ОСТАНОВЛЕНА: {len(targets)} карточек на "
                f"{marketplace} помечены проданными, но всё ещё в продаже (порог "
                f"{MAX_REWITHDRAW_PER_RUN}). Похоже на рассинхрон базы с площадкой, а не на "
                f"недоснятые остатки — остатки не тронуты. Проверьте вручную: {skus}…"
            ),
        )
        return {"checked": checked, "fixed": 0, "failed": 0, "halted": len(targets)}

    for key in targets:
        book, listing = book_by_key[key]

        _log(
            db,
            marketplace=marketplace,
            action="reconcile_withdrawn",
            ok=True,
            book_id=book.id,
            message=(
                f"Книга {book.sku}: продана, но на {marketplace} всё ещё "
                f"в продаже. Обнуляем остаток через API."
            ),
        )

        # Снимаем через sell() (через withdraw_book с use_sell=True) — обнуляет
        # остаток БЕЗ архивации Ozon. withdraw_book сам проставляет статус лота:
        # WITHDRAWN при успехе/офлайн, ERROR при сбое API. Взводить ACTIVE не нужно
        # и опасно: если что-то упадёт до вызова, лот навсегда останется активным.
        try:
            success = withdraw_book(db, book, marketplace, use_sell=True)
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
                listing.status = ListingStatus.WITHDRAWN
                failed += 1
        except Exception as exc:
            listing.status = ListingStatus.WITHDRAWN
            failed += 1
            _log(
                db,
                marketplace=marketplace,
                action="reconcile_withdrawn",
                ok=False,
                book_id=book.id,
                message=f"Книга {book.sku}: сбой при снятии — {exc}",
            )

        # Обновляем статус книги
        refresh_book_status(db, book)

    # При ручном запуске итог пишем всегда: иначе после нажатия «Проверить снятые»
    # в журнале не остаётся никакого следа и непонятно, выполнилась ли проверка.
    # При автозапуске — только если реально что-то исправили или не смогли снять.
    if verbose or fixed or failed:
        summary = f"Сверка проданных книг ({method}): проверено {checked}, исправлено {fixed}"
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


def reconcile_all_marketplaces(db: Session, verbose: bool = False) -> dict:
    """Сверить проданные книги на всех включённых площадках. Вызывается по расписанию.

    verbose по умолчанию False: единственный вызывающий — планировщик, а ему
    нужна тишина, пока нечего исправлять (см. reconcile_withdrawn_books).
    """
    results = {}
    for marketplace in ["ozon", "wildberries"]:
        results[marketplace] = reconcile_withdrawn_books(db, marketplace, verbose=verbose)
    return results
