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

import threading
from datetime import timedelta

from sqlalchemy import desc, select
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
from app.sync import _as_utc, refresh_book_status, withdraw_book

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


# Полная сверка тяжёлая и пишет в те же таблицы, что и фоновые задачи. Замок не
# даёт запустить второй проход одновременно (кнопка в UI + задача планировщика).
# Прод работает в один процесс (uvicorn --workers 1), поэтому его достаточно.
_SYNC_LOCK = threading.Lock()

# Сколько книг за один проход слежения можно снять по обнулившемуся остатку.
# Продажи приходят по одной; десяток нулей за пять минут — это сбой площадки
# (например, WB ещё не подхватил остатки свежей поставки). Порог абсолютный, а не
# в долях каталога: на 50k книг «доля» всегда мала, а сто ложно снятых книг —
# это сто карточек, которые перестали продаваться на обеих площадках.
MAX_WATCH_REMOVALS_PER_RUN = 10

# То же для ПОЛНОЙ сверки. Она авторитетнее слежения (тянет весь каталог, а не
# остатки по ключам), поэтому порог выше: за час набегают продажи и ручные снятия.
# Но и здесь пачка снятий — почти всегда неполный ответ площадки, а не реальность:
# обрыв пагинации каталога или склад, не отдавший остатки. Такой ответ выглядит как
# «каталог опустел», и без порога сверка снимала книги с ОБЕИХ площадок пачкой.
MAX_SYNC_REMOVALS_PER_RUN = 50


def _log(db: Session, *, marketplace, action, ok, message, book_id=None) -> None:
    db.add(SyncLog(marketplace=marketplace, book_id=book_id, action=action, ok=ok, message=message))


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

    Помечаем лот этой площадки снятым (остатка там уже нет — живой вызов не нужен),
    а лоты на остальных площадках снимаем ЧЕРЕЗ ЖИВОЙ API (withdraw_book): без вызова
    карточка площадки осталась бы физически в продаже при «снятой» книге в базе —
    отсюда «в наличии на Ozon» при нуле на WB. Статус книги пересчитывается единой
    функцией refresh_book_status.

    Кросс-снятие подчиняется рубильнику «Автоснятие» из Настроек: пока он выключен,
    лот пропавшей площадки помечаем снятым (это просто факт — там книги уже нет), а
    лоты на ОСТАЛЬНЫХ площадках не трогаем. Иначе тумблер обещал бы одно
    («не снимает книги с площадок»), а программа делала другое.
    """
    from app.flags import is_auto_withdraw_enabled  # локальный импорт против цикла

    if listing.status == ListingStatus.TRASHED:
        # Карточка уже удалена в корзину WB — TRASHED терминальный статус.
        # Не переводим её в WITHDRAWN: иначе сверка вернула бы карточку в очередь
        # корзины и каждый час повторно удаляла одну и ту же (лишние запросы → 429).
        # Только пересчитываем статус книги — лот остаётся как есть.
        refresh_book_status(db, book)
        return

    listing.status = ListingStatus.WITHDRAWN
    listing.last_synced_at = utcnow()

    if is_auto_withdraw_enabled(db):
        # Снимаем лоты на остальных площадках ЧЕРЕЗ ЖИВОЙ API (withdraw_book), а не
        # только локально. Локальная пометка без вызова оставляла карточку площадки
        # физически в продаже: книга «снята» в базе, но продолжает продаваться.
        # Именно из-за этого книга показывалась «в наличии на Ozon» при нуле на WB.
        # WB-карточки снимаем через sell() (обнуление остатка) — убирать их в корзину
        # здесь не нужно и вредно (корзиной занимается wb_trash, а лишние вызовы API
        # сжигают лимит). Снятые ранее/удалённые в корзину лоты не трогаем.
        for other_listing in book.listings:
            if other_listing.marketplace != marketplace and other_listing.status in (
                ListingStatus.ACTIVE,
                ListingStatus.ERROR,
            ):
                withdraw_book(db, book, other_listing.marketplace, use_sell=True)
    else:
        skipped = [
            l.marketplace for l in book.listings
            if l.marketplace != marketplace and l.status == ListingStatus.ACTIVE
        ]
        if skipped:
            _log(db, marketplace=marketplace, action="withdraw_skipped", ok=True,
                 message=(f"Книга {book.sku}: автоснятие выключено — "
                          f"лоты не тронуты: {', '.join(skipped)}"))

    refresh_book_status(db, book)


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

        # Ищем существующую книгу СТРОГО по SKU. SKU — уникальный идентификатор
        # экземпляра. Книги букинистические, б/у: у разных физических экземпляров
        # ISBN совпадает, поэтому искать по ISBN НЕЛЬЗЯ — иначе новый экземпляр
        # «приклеится» к чужому и не заведётся отдельной карточкой (пропадёт).
        # По ISBN ищем только когда SKU в строке вообще нет (площадка не дала) —
        # тогда это единственная зацепка, чтобы не плодить дубли одной карточки.
        book = None
        if sku:
            book = db.scalar(select(Book).where(Book.sku == sku))
        elif isbn:
            book = db.scalar(select(Book).where(Book.isbn == isbn))

        stock = _parse_stock(row.get("stock"))
        out_of_stock = stock is not None and stock <= 0
        # Ключ остатка на площадке (offer_id у Ozon, баркод у WB). Клиент кладёт
        # его прямо в строку выгрузки (не через сопоставление колонок — при импорте
        # файлом такой колонки нет). Пусто — оставим текущий/по внешнему id.
        raw_key = row.get("stock_key")
        stock_key = str(raw_key).strip() if raw_key not in (None, "") else None

        # «Реально продаётся» — авторитетный признак от клиента площадки (Ozon: список
        # IN_SALE; WB: есть баркод и положительный остаток FBS). Если клиент его не дал
        # (импорт файлом), считаем продающейся любую строку не с нулевым остатком.
        in_sale = row.get("in_sale")
        if in_sale is None:
            in_sale = not out_of_stock

        # Новую карточку заводим ТОЛЬКО если она реально продаётся. На площадках висят
        # сотни давно снятых карточек (остаток 0 или вообще без баркода) — им не место
        # в каталоге. Уже известную книгу, ушедшую из продажи, обрабатываем ниже как
        # реальное снятие/продажу (кросс-снятие).
        if book is None and not in_sale:
            skipped += 1
            continue

        # Новой книге нужно название (Book.title NOT NULL). Ozon иногда возвращает
        # карточку без имени из /v3/product/info/list (пакетный ответ неполный) — раньше
        # такие книги тихо пропускались и не попадали в каталог, отсюда расхождение
        # счётчиков с Ozon. Теперь используем SKU как запасное название: книга создастся,
        # а название подтянется при следующей полной сверке, когда ответ будет полным.
        if book is None and not title:
            if sku:
                title = sku  # SKU как запасное название, лучше чем потерять книгу
            else:
                skipped += 1
                continue

        # ВАЖНО: in_sale может быть None, когда площадка не вернула остаток по карточке
        # (неполный ответ склада / сбой API) — это «не знаю», а НЕ «не продаётся».
        # Проверяем строго через `is False`, иначе None уходил в «не продаётся» и
        # живая карточка снималась с продажи на ОБЕИХ площадках по сбою API.
        if in_sale is None:
            in_sale = True

        if book:
            updated += 1
        else:
            auto_sku = sku or f"AUTO-{isbn or title[:20]}"
            book = Book(sku=auto_sku)
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

        # Год: приводим к int, отсекая .0 и строки вроде "2020 г."
        year_raw = val("year")
        if year_raw and not book.year:
            try:
                book.year = int(float(year_raw))
            except (ValueError, TypeError):
                pass  # игнорируем битое значение

        price = val("price")
        if price and book.price is None:
            try:
                book.price = float(price.replace(",", "."))
            except ValueError:
                pass

        try:
            # flush нужен только для новых книг: нужен book.id для создания лота.
            # Для уже существующих книг (updated) book.id уже есть — flush лишний
            # и при 10k карточек даёт 10k лишних roundtrip к БД.
            if book.id is None:
                db.flush()
        except Exception as exc:
            # IntegrityError при дубле SKU (race condition с AUTO-{isbn}): откатываем
            # добавление и ищем книгу заново. Если она появилась — используем её.
            if "unique constraint" in str(exc).lower() or "duplicate" in str(exc).lower():
                db.rollback()
                if sku:
                    book = db.scalar(select(Book).where(Book.sku == sku))
                elif isbn:
                    book = db.scalar(select(Book).where(Book.isbn == isbn))
                if book:
                    # Книга создана параллельным потоком — продолжаем с ней
                    updated += 1
                    created -= 1  # отменяем счётчик created
                else:
                    # Не удалось найти — пропускаем строку
                    skipped += 1
                    continue
            else:
                # Другая ошибка — пробрасываем выше
                raise

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
            # external_id всегда обновляем, если площадка дала новое значение:
            # у WB это nmID, который нужен для удаления в корзину. Старое значение
            # могло быть vendorCode — перезаписываем на nmID.
            new_ext_id = val("external_id")
            if new_ext_id:
                listing.external_id = new_ext_id
            # Ключ остатка держим в актуальном состоянии — по нему идёт слежение.
            if stock_key:
                listing.stock_key = stock_key

        if not in_sale or out_of_stock:
            # Больше не продаётся на площадке (остаток 0 / пропал баркод / in_sale=False) →
            # снимаем лот и кросс-снимаем с других площадок. Проверяем и out_of_stock явно:
            # если площадка вернула остаток 0, книга не должна висеть в продаже, даже если
            # in_sale случайно проставился в True (например, склад не настроен).
            _cross_withdraw(db, book, marketplace, listing)
        elif listing.removed_from_sale:
            # Карточка на площадке снова «в продаже» (площадка сама её подтянула:
            # возврат после отгрузки, новый остаток и т.п.), но заказ по ней был
            # отменён ПОСЛЕ отгрузки — значит книга физически вернулась и НИКОГДА не
            # вернётся на этот артикул (возвраты идут в работу как новая книга с
            # другим артикулом). Поднимать лот в ACTIVE нельзя, иначе:
            #   refresh_book_status → IN_STOCK → watch_stocks/poll_orders продаст её
            #   снова — и это тот самый баг, из-за которого возвращённые книги
            #   повторно уходили покупателям.
            #
            # Вместо этого принудительно сжимаем карточку через живой API:
            #   - Ozon: _set_stock(0);
            #   - WB:   _set_stock(0) + move_to_trash.
            # Если площадка выключена или API недоступен — всё равно не поднимаем в
            # ACTIVE: просто держим WITHDRAWN локально. reconcile_disappeared ниже
            # видит флаг removed_from_sale и не пытается «воскресить» такую книгу.
            listing.status = ListingStatus.WITHDRAWN
            listing.last_synced_at = utcnow()
            _log(db, marketplace=marketplace, action="reconcile_removed", ok=True,
                 book_id=book.id,
                 message=(
                     f"Книга {book.sku}: карточка {marketplace} снова появилась «в продаже», "
                     f"но помечена «удалена из продажи» (отменённый отгруженный заказ) — "
                     f"лот удержан в WITHDRAWN, карточку сожмём при следующем проходе корзины"
                 ))
            _force_remove_from_sale(db, book, marketplace, listing)
            refresh_book_status(db, book)
        else:
            # Карточка на площадке есть и в продаже. Но если книга локально уже
            # НЕ в продаже (sold/withdrawn — продана или снята вручную), поднимать
            # лот в ACTIVE нельзя: это «воскресило» бы снятую книгу, и она снова
            # продавалась бы без товара. Такое бывает, когда карточку вернули на
            # площадку после отмены/возврата, а сверка не знает, что книга снята.
            #
            # Правильно — держать WITHDRAWN и дать wb_trash/reconcile_withdrawn
            # сжать карточку. Иначе за час набираются «активные» лоты при снятых
            # книгах (942 шт.), из-за которых сверка бьёт ложную тревогу
            # «421 пропало».
            if book.status == BookStatus.IN_STOCK:
                listing.status = ListingStatus.ACTIVE
                listing.last_synced_at = utcnow()
                if sku:
                    live_skus.add(sku)
            else:
                if listing.status != ListingStatus.WITHDRAWN:
                    listing.status = ListingStatus.WITHDRAWN
                    listing.last_synced_at = utcnow()

    return {"created": created, "updated": updated, "skipped": skipped, "live_skus": live_skus}


def _force_remove_from_sale(
    db: Session, book: Book, marketplace: str, listing: Listing
) -> None:
    """Живой вызов API: сжать карточку площадки, которая не должна вернуться в продажу.

    Ozon: _set_stock(0) (архив не используем).
    WB: _set_stock(0) + перемещение в корзину (через withdraw).

    Если API недоступно/выключено — молча оставляем WITHDRAWN локально. Лот не
    поднимется в ACTIVE из-за флага removed_from_sale, а физическое удаление
    карточки выполнит wb_trash (для WB) или reconcile_withdrawn (для Ozon) в
    ближайший проход.
    """
    from app.marketplaces import MarketplaceError
    from app.security import decrypt_credentials

    account = db.scalar(
        select(MarketplaceAccount).where(MarketplaceAccount.marketplace == marketplace)
    )
    if not account or not account.enabled or not account.credentials_encrypted:
        return
    try:
        creds = decrypt_credentials(account.credentials_encrypted)
        client = get_client(marketplace, creds)
    except Exception:
        return
    try:
        client.withdraw(listing)
        listing.status = ListingStatus.WITHDRAWN
        listing.last_synced_at = utcnow()
        listing.last_error = None
        _log(db, marketplace=marketplace, action="withdraw", ok=True, book_id=book.id,
             message=(
                 f"Карточка {book.sku} на {marketplace} принудительно снята: "
                 f"заказ был отменён после отгрузки, возврат не возвращается на этот артикул"
             ))
    except MarketplaceError as exc:
        listing.last_error = str(exc)
        _log(db, marketplace=marketplace, action="withdraw", ok=False, book_id=book.id,
             message=(
                 f"Не удалось принудительно снять карточку {book.sku} на {marketplace}: {exc}"
             ))


def reconcile_disappeared(db: Session, marketplace: str, live_skus: set[str]) -> int:
    """Снять книги, пропавшие из каталога площадки (карточки больше нет / остаток 0).

    Проходим по всем НЕснятым лотам этой площадки. Если SKU книги нет в live_skus
    (площадка эту книгу больше не отдаёт как «в наличии») — снимаем книгу со всех
    площадок. Так продажа/снятие на одной площадке зеркалится на другую, даже
    если опрос заказов её не поймал.

    Книги, у которых нет лота на этой площадке (например, только на WB), не
    затрагиваются — выбираем строго по marketplace. Возвращает число снятых книг.
    """
    listings = db.scalars(
        select(Listing)
        .options(selectinload(Listing.book).selectinload(Book.listings))
        .where(
            Listing.marketplace == marketplace,
            Listing.status != ListingStatus.WITHDRAWN,
            # TRASHED — терминальный статус (карточка удалена в корзину WB).
            # Такие лоты не сканируем: карточки нет в выгрузке, и сверка
            # «воскрешала» бы их каждый час в WITHDRAWN → повторная очередь.
            Listing.status != ListingStatus.TRASHED,
        )
    ).all()

    # «Пропавшими» считаем только книги, которые СЕЙЧАС в продаже (IN_STOCK).
    # Книга со статусом SOLD/WITHDRAWN уже снята — её лот не «пропал», а просто
    # застрял в ACTIVE (карточку вернули на площадку при отмене/возврате, а код
    # не перевёл лот в WITHDRAWN). Раньше такие лоты попадали в would_remove
    # тысячами, за каждый час сверка «снимала их заново» и била ложную тревогу
    # «421 книг пропали» — хотя книги просто давно сняты. Их не надо трогать.
    would_remove = [
        l for l in listings
        if l.book and l.book.sku not in live_skus and l.book.status == BookStatus.IN_STOCK
    ]

    # Книги, которые только-только пропали из выгрузки (впервые на этом проходе) —
    # НЕ снимаем сразу: свежая карточка WB часто отсутствует в каталоге первые
    # минуты после заведения (ещё не сгенерирован баркод/не попала в выдачу), а
    # иногда и карточка Ozon пропадает из выгрузки на час. Если снимать такие
    # книги немедленно, сверка хаотично снимала бы свежезаведённые книги с обоих
    # площадок. Даём сутки, и только тогда снимаем: за сутки карточка либо
    # появится в выгрузке, либо её действительно нет (удалена/в корзине).
    # Без этого окна «потерянный час» превращался в ложное снятие тысячи книг.
    grace_until = utcnow() - timedelta(days=1)
    would_remove = [
        l for l in would_remove
        if _as_utc(l.last_synced_at) is None or _as_utc(l.last_synced_at) < grace_until
    ]

    # Предохранитель от массового снятия при неполном ответе площадки.
    #
    # Два условия остановки:
    # 1. Абсолютный порог MAX_SYNC_REMOVALS_PER_RUN: больше N снятий за проход —
    #    слишком много даже для активного каталога. Защищает большие каталоги от
    #    тотального сноса при полном отказе API.
    # 2. Относительный: удаляется БОЛЬШЕ, чем нашлось живых. Если платформа вернула
    #    3 карточки, а снять хочется 27 — это обрыв пагинации или сбой склада,
    #    а не реальные продажи. Настоящие продажи приходят постепенно; разом
    #    «продать» больше книг, чем сейчас в каталоге, невозможно.
    tripped = len(would_remove) > MAX_SYNC_REMOVALS_PER_RUN or (
        live_skus and len(would_remove) > len(live_skus)
    )
    if tripped:
        skus_sample = ", ".join(l.book.sku for l in would_remove[:10])
        if len(would_remove) > MAX_SYNC_REMOVALS_PER_RUN:
            reason = f"это больше порога {MAX_SYNC_REMOVALS_PER_RUN} за один проход"
        else:
            reason = f"а в продаже площадка показала всего {len(live_skus)}"
        _log(
            db,
            marketplace=marketplace,
            action="reconcile_removed",
            ok=False,
            message=(
                f"Сверка каталога ОСТАНОВЛЕНА: {len(would_remove)} книг пропали из "
                f"выгрузки {marketplace}, {reason}. Похоже на неполный ответ API, "
                f"а не на реальные снятия — книги не тронуты. "
                f"Проверьте вручную: {skus_sample}…"
            ),
        )
        # ВАЖНО: порог сработал — но снимать по-прежнему нужно, иначе сотни
        # пропавших книг висят вечно. Снимаем порцию до предела, остальное —
        # в следующий проход (через час). Это безопасно: карточки уже >суток
        # отсутствуют в выгрузке, а не свежие.
        removed = 0
        for listing in would_remove[:MAX_SYNC_REMOVALS_PER_RUN]:
            book = listing.book
            _reconcile_remove_one(db, book, marketplace, listing)
            removed += 1
        return removed

    removed = 0
    for listing in would_remove:
        book = listing.book
        _reconcile_remove_one(db, book, marketplace, listing)
        removed += 1

    return removed


def _reconcile_remove_one(db: Session, book: Book, marketplace: str, listing: Listing) -> None:
    """Снять одну книгу, пропавшую из ПОЛНОЙ выгрузки площадки.

    Полная выгрузка — весь каталог площадки. Если карточки в ней НЕТ больше суток,
    физически на площадке её не существует (удалена/в корзине/снята). Поэтому это
    НЕ «снятие», а фиксация ФАКТА отсутствия:

    - лот этой площадки помечаем терминальным TRASHED: корзина WB больше не будет
      тратить на него лимит (удалять нечего — карточки нет), и сверка не будет
      «воскрешать» его обратно (TRASHED исключён из очереди WB и из подъёма);
    - книги на ДРУГИХ площадках (если есть) снимаем через живой API (withdraw_book)
      только когда лот раньше был ACTIVE — то есть книга реально продавалась где-то
      ещё. Если других активных лотов нет — просто фиксируем статус книги.
    """
    if marketplace == "wildberries" and listing.status != ListingStatus.TRASHED:
        # Карточки физически нет в каталоге WB — удалять в корзину нечего.
        # TRASHED исключает лот из очереди корзины (лимит WB не тратится впустую)
        # и из дальнейших сверок. Пометка терминальная — книга не вернётся,
        # пока её не выставят заново на площадке (тогда сверка заведёт ACTIVE).
        listing.status = ListingStatus.TRASHED
        listing.last_synced_at = utcnow()
        _log(db, marketplace=marketplace, action="reconcile_removed", ok=True,
             book_id=book.id,
             message=f"Книга {book.sku}: карточки нет в выгрузке {marketplace} — лот помечен TRASHED (в корзину удалять нечего)")
        refresh_book_status(db, book)
        return

    _cross_withdraw(db, book, marketplace, listing)
    _log(db, marketplace=marketplace, action="reconcile_removed", ok=True,
         book_id=book.id,
         message=f"Книга {book.sku} пропала с {marketplace}")


def sync_marketplace(db: Session, marketplace: str) -> dict:
    """Полная сверка одной площадки под общим замком сверки.

    Внешняя точка входа (кнопка «Загрузить из Ozon/WB» в Импорте). Замок тот же,
    что у sync_all: два параллельных прохода по одним SKU дают конфликт на
    уникальном лоте (book_id+marketplace) и рассинхрон статусов. Раньше эта
    функция вызывалась в обход замка прямо из роута импорта.

    Если сверка уже идёт — бросаем MarketplaceError, роут покажет это человеку.
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        raise MarketplaceError("Сверка уже выполняется — дождитесь завершения")
    try:
        return _sync_marketplace_locked(db, marketplace)
    finally:
        _SYNC_LOCK.release()


def _sync_marketplace_locked(db: Session, marketplace: str) -> dict:
    """Тело сверки одной площадки. Вызывать только с уже взятым _SYNC_LOCK.

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
    Если сверка уже идёт (фоновая задача или другая кнопка) — не запускаем вторую:
    два параллельных прохода по одним SKU дают конфликт на уникальном лоте
    (book_id+marketplace) и рассинхрон статусов.
    """
    if not _SYNC_LOCK.acquire(blocking=False):
        return {"__busy__": {"error": "Сверка уже выполняется — дождитесь завершения"}}
    try:
        return _sync_all_locked(db)
    finally:
        _SYNC_LOCK.release()


def _sync_all_locked(db: Session) -> dict:
    enabled = db.scalars(
        select(MarketplaceAccount.marketplace).where(MarketplaceAccount.enabled == True)  # noqa: E712
    ).all()

    out: dict[str, dict] = {}
    for marketplace in enabled:
        try:
            # Именно _locked-вариант: замок уже взят в sync_all, а Lock не
            # реентрантный — повторный acquire дал бы отказ «сверка уже идёт».
            out[marketplace] = _sync_marketplace_locked(db, marketplace)
            db.commit()
        except Exception as exc:  # noqa: BLE001 — сбой одной площадки не роняет сверку
            db.rollback()
            _log(db, marketplace=marketplace, action="catalog_sync", ok=False, message=str(exc))
            db.commit()
            out[marketplace] = {"error": str(exc)}
    return out


def _active_listings(db: Session, marketplace: str) -> list[Listing]:
    """Активные лоты площадки с подгруженной книгой и её остальными лотами.

    TRASHED явно исключаем: карточка в корзине WB, и по ней никогда не должно
    идти слежение за остатками (её ключа в ответе площадки нет, и watch_stocks
    считал бы её «пропавшей»).
    """
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

    # Защита от ЧАСТИЧНОГО сбоя: если площадка не вернула значительную долю
    # запрошенных ключей, это похоже на лимит/обрыв пагинации, а не на то, что
    # разом продали пол-склада. В этом случае отсутствие ключа НЕ считаем снятием
    # (снимаем только по явному нулю) — пропавшие карточки доснимет полная сверка,
    # которая тянет весь каталог целиком. Порог: не вернулась > трети ключей.
    missing = [k for k in keys if k not in stocks]
    # Подозрительно, когда пропала ЗАМЕТНАЯ ДОЛЯ ключей и это не единичные карточки:
    # разом «удалить» треть каталога площадка не может, а вот отдать неполный ответ —
    # запросто. Единичные пропажи (1-4 книги) считаем настоящими: это обычное дело.
    # Порог — треть (>=), а не половина (>): ровно половина пропавших ключей это
    # такой же признак обрыва пагинации, а строгое сравнение её пропускало.
    suspicious = len(missing) >= 5 and len(missing) * 3 >= len(keys)
    trust_missing = not suspicious
    if suspicious:
        _log(db, marketplace=marketplace, action="watch_stocks", ok=False,
             message=(f"Не вернулось {len(missing)} из {len(keys)} остатков — вероятен сбой/лимит. "
                      f"Снимаем только по явному нулю; пропавшие ключи оставлены полной сверке"))

    # Кандидаты на снятие: собираем список ДО того, как что-то менять. Нужно, чтобы
    # оценить масштаб — пачка снятий за один проход это признак сбоя площадки, а не
    # массовой продажи (см. ниже).
    #
    # ВАЖНО: В кандидаты попадают только книги, которые РЕАЛЬНО нужно снимать:
    # - Статус IN_STOCK (уже снятые/проданные книги не трогаем)
    # - Есть лоты на ДРУГИХ площадках (книги только на этой площадке не трогаем)
    #
    # Это критично для предохранителя: он должен срабатывать только на реальные
    # массовые продажи, а не на книги, которые и так не трогаем.
    candidates: list[tuple[Listing, Book, str]] = []
    seen_books: set[int] = set()  # защита от дублей: одна книга снимается один раз
    for listing in keyed:
        book = listing.book
        if book is None:
            continue
        if book.id in seen_books:
            continue

        # Пропускаем книги, которые уже не IN_STOCK (уже обработаны ранее)
        if book.status != BookStatus.IN_STOCK:
            continue

        # Пропускаем книги ТОЛЬКО на этой площадке (кросс-снимать не с чего)
        has_other_listings = any(
            other.marketplace != marketplace and other.status == ListingStatus.ACTIVE
            for other in book.listings
        )
        if not has_other_listings:
            continue

        amount = stocks.get(listing.stock_key)
        if amount is None:
            # Ключ не вернулся. Снимаем как «карточка пропала» только если ответ
            # выглядит полным (trust_missing) — иначе это подозрение на сбой.
            if not trust_missing:
                continue
            reason = "карточка пропала"
        elif amount <= 0:
            reason = "остаток 0"
        else:
            continue
        candidates.append((listing, book, reason))
        seen_books.add(book.id)

    # Предохранитель на массовое снятие. Нулевой остаток — не доказательство
    # продажи, а наблюдение: WB отдаёт 0 по только что заведённым карточкам, пока
    # склад не подхватил поставку. Продажи же приходят по одной, а не десятками за
    # пять минут, поэтому пачка нулей — это почти всегда сбой на стороне площадки.
    #
    # Раньше такой сбой обходился дорого: лоты помечались снятыми на ОБЕИХ
    # площадках (кросс-снятие), книга получала статус «Продана»/«Снята», и сверка
    # проданных дальше обнуляла живые остатки по неверной базе. Владелец
    # восстанавливал остатки руками — через 10 минут всё повторялось.
    #
    # Порог абсолютный: пропускаем единичные снятия (обычные продажи), а на пачке
    # останавливаемся целиком и пишем в журнал ошибку, чтобы человек увидел её в UI.
    # Настоящие продажи в этот проход не потеряются — их поймает опрос заказов
    # (он идёт по заказам, а не по остаткам) и полная сверка каталога.
    if len(candidates) > MAX_WATCH_REMOVALS_PER_RUN:
        skus_sample = ", ".join(b.sku for _, b, _ in candidates[:10])
        all_skus = ", ".join(b.sku for _, b, _ in candidates)

        # Дедупликация: не спамим одной и той же ошибкой каждые 5 минут.
        # Проверяем последнее срабатывание: если то же количество книг и недавно
        # (< 30 минут), молчим. Сбой WB может длиться часами — не нужно забивать
        # журнал сотнями одинаковых записей.
        from datetime import timedelta
        recent_halt = db.scalar(
            select(SyncLog).where(
                SyncLog.marketplace == marketplace,
                SyncLog.action == "watch_stocks",
                SyncLog.ok == False,
                SyncLog.message.like(f"%ОСТАНОВЛЕНО: у {len(candidates)} из%"),
                SyncLog.created_at >= utcnow() - timedelta(minutes=30)
            ).order_by(desc(SyncLog.created_at)).limit(1)
        )

        if not recent_halt:
            # Первое срабатывание или изменился масштаб — пишем полный отчёт
            _log(db, marketplace=marketplace, action="watch_stocks", ok=False,
                 message=(f"Слежение за остатками ОСТАНОВЛЕНО: у {len(candidates)} из {len(keys)} книг "
                          f"на {marketplace} остаток пропал разом (порог {MAX_WATCH_REMOVALS_PER_RUN}). "
                          f"Похоже на сбой площадки, а не на продажи — книги не сняты. "
                          f"Проверьте остатки вручную: {skus_sample}…"))
            # Полный список в отдельной записи для анализа
            _log(db, marketplace=marketplace, action="watch_stocks_halted_full", ok=False,
                 message=f"Полный список {len(candidates)} книг, остановленных предохранителем: {all_skus}")
        # Если недавно уже было — молчим, не спамим в журнал

        return {"checked": len(keys), "removed": 0, "halted": len(candidates)}

    removed = 0
    for listing, book, reason in candidates:
        _cross_withdraw(db, book, marketplace, listing)
        removed += 1
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
