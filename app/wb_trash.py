"""Удаление снятых книг в корзину WB.

Проходит по книгам со статусом SOLD/WITHDRAWN, у которых есть лот WB, и удаляет
карточки в корзину пачками с паузами (чтобы не схлопнуть лимит API 429).

СТРАТЕГИЯ ОБРАБОТКИ:
- Сортировка по updated_at DESC (новые книги первыми) — свежие продажи уходят в
  корзину сразу, а «залежи» — после них, когда новых не осталось
- Лимит за проход (MAX_BOOKS_PER_RUN) — не блокируем scheduler надолго
- Пачки по BATCH_SIZE карточек — баланс между скоростью и лимитами
- Пауза PAUSE_SECONDS между пачками — даём API WB остыть
- При 429: немедленная остановка прогона + окно тишины RETRY_AFTER_429_MIN.
  WB отдаёт 429 на КАЖДЫЙ вызов этого endpoint (даже одиночный) и держит лимит
  десятки минут — молотить в это время бесполезно. После 429 прогон выходит
  сразу, не тратя запросов, пока не пройдёт окно; тогда пробуем одну пачку,
  и если снова 429 — окно продлевается.
- При жёсткой ошибке пачки (400 и т.п.) — ИЗОЛЯЦИЯ: WB отклоняет всю пачку из-за
  одной битой карточки (уже в корзине / карточки нет / нет прав). Пробуем каждую
  по одной: хорошие удаляются, а битые после MAX_TRASH_FAILURES неудач помечаются
  trash_blocked и выходят из очереди — иначе они висели в начале FIFO вечно, каждая
  пачка падала 400-й, и очередь не двигалась («то удаляются, то не удаляются»).

Книги со свежим неотменённым заказом (моложе CANCEL_GRACE_DAYS) не трогаем:
заказ ещё могут отменить, и тогда карточку пришлось бы достать из корзины.
"""
from __future__ import annotations

import time
from datetime import datetime, timedelta
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.marketplaces import MarketplaceError, get_client
from app.models import (
    AppSetting,
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


# Сколько дней после заказа не трогаем карточку проданной книги: столько живёт
# риск отмены. Пока окно не вышло, карточка остаётся в кабинете WB — если заказ
# отменят, её не придётся достать из корзины.
#
# 3 дня вместо 14: восстановление из корзины (WB /content/v2/cards/recover)
# работает и проверено — при отмене программа сама достаёт карточку обратно.
# Длинное окно (14 дней) лишь копило сотни карточек в кабинете при активных
# продажах: очереди корзины вставали, а признак «снят не этой продажей»
# (_withdrawn_by_order) защищает от восстановления чужих лотов.
CANCEL_GRACE_DAYS = 3

# Максимум книг за один проход. Защита от зависания scheduler'а: если backlog
# огромный (тысячи книг), не обрабатываем их все за раз — возьмём порцию, а
# остальное заберёт следующий запуск. 100 книг × 2 секунды на пачку = ~3 минуты.
MAX_BOOKS_PER_RUN = 100

# Размер пачки для одного DELETE-запроса к WB. API принимает массив nmID.
# 10 вместо 30: WB жёстко лимитирует этот endpoint, а очередь тормозили пачки
# по 30 — каждая вторая упиралась в лимит, и прогон почти не приносил удалений.
# Десять карточек проходят чаще, а очередь двигается заметно быстрее.
BATCH_SIZE = 10

# Пауза между пачками. WB жёстко лимитирует DELETE — даём API остыть.
# 5 секунд вместо 2: при завале в тысячи карточек пауза меньше приводила к
# тому, что почти каждая пачка упиралась в лимит (429) и прогон почти не
# приносил удалений. Пять секунд — медленнее, но стабильно.
PAUSE_SECONDS = 5

# Окно тишины после лимита. WB отдаёт 429 на delete/trash на КАЖДЫЙ вызов,
# даже одиночный, и держит лимит десятки минут: молотить пачки/одиночки,
# пока идёт троттлинг, бесполезно — жжём квоту и провоцируем ещё более
# жёсткий лимит. Поэтому после 429 запоминаем время и следующие
# RETRY_AFTER_429_MIN минут прогон выходит сразу, не делая ни одного запроса.
# Когда окно вышло — пробуем одну пачку, и если снова 429, окно продлевается.
# Так очередь (тысячи карточек) двигается по 1-2 пачки, когда WB отпускает,
# и ни один запрос не тратится впустую во время троттлинга.
RETRY_AFTER_429_MIN = 20

# Ключ в app_settings, где хранится момент последнего 429 от WB.
LAST_429_KEY = "wb_trash_last_429"

# Сколько неудачных попыток переживает карточка, прежде чем будет признана
# «битой» и выведена из очереди. Если карточка не удаляется 3 раза подряд
# (обычно: уже в корзине / удалена с WB), шансов, что она удалится сама,
# нет — вечное повторение лишь жжёт лимит и блокирует очередь позади неё.
MAX_TRASH_FAILURES = 3


def _get_last_429(db: Session) -> datetime | None:
    """Когда был последний 429 от WB (из app_settings, ISO-строка).

    None — 429 ещё не было (или ключ стёрли) — прогон не ограничиваем.
    """
    row = db.get(AppSetting, LAST_429_KEY)
    if row is None or not row.value:
        return None
    try:
        return datetime.fromisoformat(row.value)
    except ValueError:
        return None


def _note_429(db: Session, now: datetime | None = None) -> None:
    """Записать момент 429. Пишем обязательно: именно по нему решает окно тишины."""
    row = db.get(AppSetting, LAST_429_KEY)
    if row is None:
        row = AppSetting(key=LAST_429_KEY)
        db.add(row)
    row.value = (now or utcnow()).isoformat()


def _last_429_within(db: Session) -> bool:
    """Внутри ли окна тишины после 429 (прогон выходит, не трогая WB)."""
    last = _get_last_429(db)
    return last is not None and utcnow() - last < timedelta(minutes=RETRY_AFTER_429_MIN)


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
    days: int | None = None,
) -> dict:
    """Удалить снятые книги в корзину WB.

    Возвращает {processed, deleted, failed, blocked, skipped}: processed — реально
    отправлено в API (deleted + failed + blocked), skipped — отложено по лимиту.

    limit — максимум книг за проход. None = применяется MAX_BOOKS_PER_RUN.
    Защита от зависания: если backlog огромный, берём порцию, остальное — в
    следующий раз. Новые книги первыми (updated_at DESC): свежие продажи уходят
    в корзину сразу, а backlog подтянется следом по мере удаления.

    days — брать только книги, снятые за последние N дней (по updated_at книги).
    None = без ограничения периода. Ручной запуск из UI передаёт выбор человека
    («сутки», «7 дней», …), фоновая задача — None.

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
        return {"processed": 0, "deleted": 0, "failed": 0, "blocked": 0, "skipped": 0}

    try:
        creds = decrypt_credentials(account.credentials_encrypted)
        client = get_client("wildberries", creds)
    except (MarketplaceError, Exception) as exc:
        _log(db, action="wb_trash", ok=False,
             message=f"Не удалось подключиться к WB: {exc}")
        return {"processed": 0, "deleted": 0, "failed": 0, "blocked": 0, "skipped": 0}

    # Окно тишины после 429: WB лимитирует delete/trash так, что любой вызов
    # в этом окне вернёт 429 (наблюдали часами даже одиночный запрос). Пока окно
    # не вышло — выходим, не сделав ни одного запроса.
    #
    # В ЖУРНАЛ ПРИ ЭТОМ НЕ ПИШЕМ: при частом прогоне это были бы десятки
    # одинаковых «подождана» в день. Факт прогона и его причину видно в журнале
    # контейнера (счётчики процесса), а для пользователя в UI кнопка всё равно
    # покажет честный ответ.
    if _last_429_within(db):
        remaining = int(
            (RETRY_AFTER_429_MIN * 60)
            - (utcnow() - _get_last_429(db)).total_seconds()
        )
        return {"processed": 0, "deleted": 0, "failed": 0, "blocked": 0, "skipped": 0,
                "waiting": True}

    # Находим снятые книги с лотом WB. Сортировка по updated_at DESC (новые
    # первыми): свежие продажи уходят в корзину сразу, а не ждут, когда рассосётся
    # старый backlog. Раньше было ASC (FIFO), и при тысячах «залежей» новые
    # проданные книги стояли в очереди неделями.
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
            # Берём только книги с числовым nmID — без него API корзины не работает.
            # Книги с vendorCode (старые, до миграции nmID) в очередь не попадают:
            # иначе они вечно занимают первые 100 мест и блокируют все остальные.
            # trash_blocked (битые карточки) в очередь не берём совсем.
            Book.listings.any(
                (Listing.marketplace == "wildberries")
                & (Listing.status != ListingStatus.TRASHED)
                & (Listing.trash_blocked == False)  # noqa: E712
                & Listing.external_id.regexp_match(r"^\d+$")
            ),
        )
    )
    if days is not None:
        # Период из UI («снятые за последние N дней»): фильтруем по моменту
        # снятия книги (updated_at). Раньше параметр days молча игнорировался —
        # выбор «сутки» и «всё время» давали одинаковую FIFO-очередь.
        query = query.where(Book.updated_at >= utcnow() - timedelta(days=days))
    query = (
        query.order_by(Book.updated_at.desc())  # новые книги первыми
        .limit(max_books)
    )

    books = db.scalars(query).all()

    if not books:
        if verbose:
            _log(db, action="wb_trash", ok=True,
                 message=f"Очистка корзины WB: снятых книг для удаления нет")
        return {"processed": 0, "deleted": 0, "failed": 0, "blocked": 0, "skipped": 0}

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
        return {"processed": 0, "deleted": 0, "failed": 0,
                "blocked": 0, "skipped": 0, "no_nm_id": len(no_nm_id)}

    deleted = 0
    failed = 0
    blocked = 0   # битые карточки, выведенные из очереди навсегда
    skipped = 0   # не обработали из-за лимита (попробуем в следующий раз)
    deleted_skus: list[str] = []   # SKU успешно удалённых — для итоговой записи
    failed_skus: list[str] = []    # SKU, которые удалить не удалось (будут ретраиться)
    blocked_skus: list[str] = []   # SKU, переставшие пробоваться (битые)

    DELETE_URL = "https://content-api.wildberries.ru/content/v2/cards/delete/trash"

    def _do_delete(nm_ids: list[int]) -> dict:
        """Один запрос к API корзины. Возвращает тело ответа.

        WB может вернуть 200 даже когда часть карточек не удалена (ошибки в
        теле — errors/error/excludedNmIDs). Смотрим именно на них, а не только
        на HTTP-статус.
        """
        body = client._post(DELETE_URL, {"nmIDs": nm_ids})
        errors = body.get("errors") or body.get("error") or body.get("errorText") or ""
        if errors:
            detail = str(errors)
            # WB возвращает ошибки по-разному: dict {nmID: причина}, список
            # сообщений или одно сообщение. Извлекаем список виноватых nmID.
            if isinstance(errors, dict):
                # Ключи могут быть int или строками — приводим оба к int.
                bad_ids = set()
                for k in errors.keys():
                    try:
                        bad_ids.add(int(k))
                    except (TypeError, ValueError):
                        continue
            elif isinstance(errors, list):
                bad_ids = set()
                for e in errors:
                    if isinstance(e, dict):
                        nm = e.get("nmID") or e.get("nmId")
                    else:
                        nm = e
                    try:
                        bad_ids.add(int(nm))
                    except (TypeError, ValueError):
                        continue
            else:
                # Одиночная ошибка — конкретного виновника нет, считаем виноватыми
                # nmID из ПОДОЗРЕВАЕМЫХ (тех, что в теле как данные) — обычно WB
                # кладёт их в поле "data" или "excludedNmIDs".
                bad_ids = set()
                for pick in ("excludedNmIDs", "notDeletedNmIDs", "data"):
                    val = body.get(pick)
                    if isinstance(val, list):
                        for v in val:
                            try:
                                bad_ids.add(int(v))
                            except (TypeError, ValueError):
                                continue
                        break
            # Ошибка есть, но виновников в теле не названо — не знаем, кто из
            # пачки не удалился. Помечаем ВСЮ пачку неудачей: это консервативно.
            # Иначе карточки, на самом деле оставшиеся на WB, получили бы статус
            # «в корзине» и больше никогда не были бы удалены.
            if not bad_ids:
                bad_ids = set(nm_ids)
            return {"ok_nm_ids": [n for n in nm_ids if n not in bad_ids],
                    "bad_nm_ids": sorted(bad_ids), "detail": detail}
        # Без ошибок в теле — все удалены.
        return {"ok_nm_ids": list(nm_ids), "bad_nm_ids": [], "detail": ""}

    def _mark_trash_failure(book, listing, nm, reason) -> None:
        """Учёт неудачной попытки удаления в корзину.

        Временный сбой (1-2 раза) просто копит счётчик trash_failures. После
        MAX_TRASH_FAILURES подряд карточка признаётся битой (trash_blocked=True)
        и навсегда выходит из очереди: повторять бессмысленно, а в начале FIFO
        она вечно блокировала остальные карточки.
        """
        nonlocal failed, blocked
        listing.trash_failures = (listing.trash_failures or 0) + 1
        listing.last_error = reason
        if listing.trash_failures >= MAX_TRASH_FAILURES:
            # Попытки исчерпаны — карточка считается битой и навсегда выходит
            # из очереди. Не считаем её ещё и «failed»: иначе двойной учёт.
            listing.trash_blocked = True
            blocked += 1
            blocked_skus.append(f"{book.sku} ({nm})")
        else:
            # Временный сбой — будет ретраиться в следующих запусках.
            failed += 1
            failed_skus.append(f"{book.sku} ({nm})")

    # Удаляем пачками с паузами.
    #
    # КЛЮЧЕВАЯ ЛОГИКА: если пачка целиком упала (исключение MarketplaceError),
    # WB не подтвердил удаление НИ ОДНОЙ карточки. Это почти всегда «одна битая
    # в пачке»: WB отклоняет всю пачку 400-й из-за одной карточки (уже в корзине,
    # удалена с WB, нет прав). Поэтому при ошибке пачки разбираем её по одной:
    # хорошие удаляются, а битая после MAX_TRASH_FAILURES неудач выводится из
    # очереди. Иначе она вечно висела в начале FIFO и блокировала остальные.
    #
    # ЛИМИТ (429): раньше после двух неудач пачки бросали ВСЮ оставшуюся очередь
    # («остановились, отложено N») — при завале в тысячи карточек очередь стояла
    # неделями: каждая пачка съедала 2 запроса без пользы. Теперь сильнее давим на
    # паузу и падаем до одиночных запросов: очередь движется даже под лимитом.
    i = 0
    while i < len(to_delete):
        batch = to_delete[i:i + BATCH_SIZE]

        try:
            res = _do_delete([nm for _, _, nm in batch])
        except MarketplaceError as exc:
            err = str(exc)
            # Лимит запросов: одна повторная попытка после паузы, затем для пачки
            # разбираем по одной с паузой (не бросаем очередь!). Одиночная карточка
            # при лимите — откладываем, не считая виноватой.
            if "429" in err or "лимит" in err.lower():
                # Лимит WB на endpoint delete/trash. Часы наблюдений показали:
                # даже одиночный вызов упорно отдаёт 429. Ранняя попытка «долбить»
                # до ответа (ретраи пачки → по одной → снова паузы) лишь городила
                # десятки запросов за прогон и провоцировала ЕЩЁ более жёсткий
                # троттлинг. Правильно — уважать лимит: остановить прогон сразу.
                # Момент 429 запоминаем (окно тишины): следующие RETRY_AFTER_429_MIN
                # минут прогон будет выходить без единого запроса к WB, затем
                # попробует одну пачку. Пока лимит не отпустит — так и будет:
                # одна пачка за прогон, остальное заберёт следующий цикл.
                _note_429(db)
                skipped = len(to_delete) - i
                _log(db, action="wb_trash", ok=True,
                     message=f"Лимит WB при удалении в корзину: остановились, "
                             f"отложено {skipped} карточек")
                break
            else:
                # Жёсткая ошибка пачки (400 и т.п.) — почти всегда одна битая
                # карточка среди пары десятков. Разбираем по одной: выясняем,
                # кто удалился, а учёт (failed/blocked/TRASHED) делает единый
                # цикл ниже — чтобы карточку не считали дважды.
                single_ok = []
                for book, listing, nm in batch:
                    try:
                        res_single = _do_delete([nm])
                        # Ошибку WB может вернуть и в теле ответа (200 без
                        # исключения) — удалённой считаем только при пустом
                        # списке виновников.
                        if nm not in res_single["bad_nm_ids"]:
                            single_ok.append((book, listing, nm))
                    except MarketplaceError:
                        continue
                ok_ids = {n for _, _, n in single_ok}
                res = {"ok_nm_ids": [n for _, _, n in single_ok],
                       "bad_nm_ids": [n for _, _, n in batch if n not in ok_ids],
                       "detail": err}

        for book, listing, nm in batch:
            if nm in res["ok_nm_ids"]:
                deleted += 1
                deleted_skus.append(f"{book.sku} ({nm})")
                listing.status = ListingStatus.TRASHED  # не трогать повторно
                listing.trash_failures = 0
                listing.last_error = None
                listing.last_synced_at = utcnow()
            else:
                # Не подтверждено удаление — неудача. Какая именно, скажет
                # _mark_trash_failure: временный сбой (ретрай позже) или битая
                # карточка (после MAX_TRASH_FAILURES выходит из очереди).
                _mark_trash_failure(book, listing, nm, res.get("detail") or "не удалена")

        # Пауза между пачками (кроме последней)
        if i + BATCH_SIZE < len(to_delete):
            time.sleep(PAUSE_SECONDS)

        i += BATCH_SIZE

    # Итог пишем при ручном запуске всегда, при автозапуске — только если реально
    # что-то произошло (удалили, не смогли, заблокировали или отложили по лимиту).
    #
    # ВАЖНО: не пишем по записи на каждую карточку — при 4000+ книг в очереди это
    # сотни строк в журнале на каждый проход. Удалённые SKU перечисляем одной
    # итоговой записью (так же как и ошибки). Счётчики не пересекаются:
    # deleted — ушли в корзину; failed — не удались, но ещё будут ретраиться
    # (trash_failures < MAX); blocked — «битые», навсегда выведены из очереди
    # (trash_failures >= MAX).
    processed = deleted + failed + blocked
    if verbose or deleted or failed or skipped or blocked:
        msg_parts = []
        if deleted:
            msg_parts.append(f"удалено {deleted}")
        if failed:
            msg_parts.append(f"не удалось {failed}")
        if blocked:
            msg_parts.append(f"заблокировано {blocked} (битые)")
        if skipped:
            msg_parts.append(f"отложено {skipped} (лимит WB)")

        message = f"Очистка корзины WB: {', '.join(msg_parts)}"
        if deleted_skus:
            # Показываем первые 10 SKU + счётчик остальных
            shown = ", ".join(deleted_skus[:10])
            if len(deleted_skus) > 10:
                shown += f"… (всего {len(deleted_skus)})"
            message += f" | Удалены: {shown}"
        if failed_skus:
            shown = ", ".join(failed_skus[:10])
            if len(failed_skus) > 10:
                shown += f"… (всего {len(failed_skus)})"
            message += f" | Ошибки: {shown}"
        if blocked_skus:
            # Эти SKU больше не будут пробоваться (уже в корзине / карточки нет
            # на WB) — в failed_skus их нет (счётчики не пересекаются).
            shown = ", ".join(blocked_skus[:10])
            if len(blocked_skus) > 10:
                shown += f"… (всего {len(blocked_skus)})"
            message += f" | Заблокированы: {shown}"
        _log(db, action="wb_trash", ok=(failed == 0), message=message)

    return {
        "processed": processed,
        "deleted": deleted,
        "failed": failed,
        "blocked": blocked,
        "skipped": skipped,
    }
