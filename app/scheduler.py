"""Фоновые механизмы синхронизации с площадками.

Три независимых задачи APScheduler (интервалы в config.py):
1. poll_all_marketplaces — опрос заказов (~1 мин): продажи → кросс-снятие.
2. watch_all_marketplaces_stocks — слежение за остатками наших книг (~5 мин):
   дёшево, ловит снятия/продажи почти сразу.
3. sync_all_catalogs — полная сверка каталога (~60 мин): новые книги + подстраховка.

Работает, только пока запущен сервер. Планировщик ходит в БД из отдельного потока,
поэтому открывает собственную сессию через SessionLocal (не через зависимость FastAPI).
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.catalog_sync import sync_all, watch_all_stocks
from app.config import settings
from app.db import SessionLocal
from app.flags import is_sync_enabled
from app.models import MarketplaceAccount
from app.reconciliation import reconcile_all_marketplaces
from app.sync import poll_marketplace_orders, process_cancelled_orders
from app.wb_trash import move_withdrawn_to_trash

logger = logging.getLogger("scheduler")

_scheduler: BackgroundScheduler | None = None


def poll_all_marketplaces() -> None:
    """Опрос заказов — работает всегда, даже при выключенном рубильнике.

    Записывает продажи в БД для аналитики. Реальное кросс-снятие блокируется
    отдельным флагом auto_withdraw, а не sync_enabled.
    """
    db = SessionLocal()
    try:
        enabled = db.scalars(
            select(MarketplaceAccount.marketplace).where(MarketplaceAccount.enabled == True)  # noqa: E712
        ).all()
        for marketplace in enabled:
            try:
                poll_marketplace_orders(db, marketplace)
                db.commit()
            except Exception:  # noqa: BLE001 — один сбой не должен останавливать остальные площадки
                db.rollback()
                logger.exception("Сбой опроса заказов %s", marketplace)
    finally:
        db.close()


def poll_all_cancellations() -> None:
    """Проверка отменённых заказов — только при включённом рубильнике.

    Вызывает restore API площадки — это действие, не наблюдение.
    """
    db = SessionLocal()
    try:
        if not is_sync_enabled(db):
            return
        enabled = db.scalars(
            select(MarketplaceAccount.marketplace).where(MarketplaceAccount.enabled == True)  # noqa: E712
        ).all()
        for marketplace in enabled:
            try:
                process_cancelled_orders(db, marketplace)
                db.commit()
            except Exception:  # noqa: BLE001 — один сбой не должен останавливать остальные площадки
                db.rollback()
                logger.exception("Сбой проверки отмен %s", marketplace)
    finally:
        db.close()


def watch_all_marketplaces_stocks() -> None:
    """Слежение за остатками — только при включённом рубильнике.

    Может инициировать снятие книги через withdraw API.
    """
    db = SessionLocal()
    try:
        if not is_sync_enabled(db):
            return
        results = watch_all_stocks(db)
        removed = sum(r.get("removed", 0) for r in results.values() if isinstance(r, dict))
        halted = sum(r.get("halted", 0) for r in results.values() if isinstance(r, dict))
        if removed:
            logger.info("Слежение за остатками: снято книг %s (%s)", removed, results)
        if halted:
            # Сработал предохранитель массового снятия — в журнале базы уже есть
            # запись с ошибкой, но в логах сервера это тоже должно быть видно.
            logger.warning(
                "Слежение за остатками остановлено предохранителем: %s книг разом без остатка", halted
            )
    except Exception:  # noqa: BLE001 — сбой слежения не должен ронять планировщик
        db.rollback()
        logger.exception("Сбой слежения за остатками")
    finally:
        db.close()


def sync_all_catalogs() -> None:
    """Сверка каталога — работает всегда, даже при выключенном рубильнике.

    Добавляет новые книги и обновляет статусы. API снятия при этом НЕ вызывается
    (_cross_withdraw меняет только локальные статусы без вызова withdraw API).
    """
    db = SessionLocal()
    try:
        results = sync_all(db)
        if results:
            logger.info("Сверка каталога: %s", results)
    except Exception:  # noqa: BLE001 — сбой сверки не должен ронять планировщик
        db.rollback()
        logger.exception("Сбой полной сверки каталога")
    finally:
        db.close()


def reconcile_all_withdrawn() -> None:
    """Один проход сверки снятых книг с реальным состоянием на площадках.

    Проверяет книги, помеченные как снятые/проданные, но всё ещё видимые
    покупателям на площадке. Повторно снимает такие книги. Идёт каждые 10 минут.
    """
    db = SessionLocal()
    try:
        if not is_sync_enabled(db):
            return
        # verbose=False: автозапуск молчит, когда исправлять нечего, иначе две
        # площадки каждые 10 минут забивают журнал одинаковым «проверять нечего».
        results = reconcile_all_marketplaces(db, verbose=False)
        db.commit()
        if results:
            logger.info("Сверка снятых книг: %s", results)
    except Exception:  # noqa: BLE001 — сбой сверки не должен ронять планировщик
        db.rollback()
        logger.exception("Сбой сверки снятых книг")
    finally:
        db.close()


def cleanup_wb_trash() -> None:
    """Удалить снятые книги в корзину WB (каждые 10 минут).

    Обрабатывает только книги, снятые/проданные за ПОСЛЕДНИЕ 3 ЧАСА — небольшими
    порциями по 5 карточек, чтобы не упереться в лимит API (429).
    """
    db = SessionLocal()
    try:
        if not is_sync_enabled(db):
            return
        # verbose=False: автозапуск молчит, когда удалять нечего, иначе журнал
        # каждые 10 минут забивается одинаковым «нечего удалять».
        result = move_withdrawn_to_trash(db, hours=3, verbose=False)
        db.commit()
        processed = result.get("processed", 0)
        deleted = result.get("deleted", 0)
        failed = result.get("failed", 0)
        if processed:
            logger.info(
                "Очистка корзины WB (за 3 часа): обработано %s, удалено %s, не удалось %s",
                processed, deleted, failed
            )
    except Exception:  # noqa: BLE001 — сбой очистки не должен ронять планировщик
        db.rollback()
        logger.exception("Сбой очистки корзины WB")
    finally:
        db.close()


def start_scheduler() -> None:
    global _scheduler
    if not settings.scheduler_enabled or _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
    _scheduler.add_job(
        poll_all_marketplaces,
        trigger="interval",
        minutes=settings.poll_interval_minutes,
        id="poll_orders",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        poll_all_cancellations,
        trigger="interval",
        minutes=settings.poll_interval_minutes,  # та же частота, что и опрос заказов
        id="poll_cancellations",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        watch_all_marketplaces_stocks,
        trigger="interval",
        minutes=settings.stock_watch_interval_minutes,
        id="watch_stocks",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        sync_all_catalogs,
        trigger="interval",
        minutes=settings.catalog_sync_interval_minutes,
        id="catalog_sync",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        reconcile_all_withdrawn,
        trigger="interval",
        minutes=10,  # каждые 10 минут проверяем снятые книги
        id="reconcile_withdrawn",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.add_job(
        cleanup_wb_trash,
        trigger="interval",
        minutes=10,
        id="wb_trash_cleanup",
        max_instances=1,
        coalesce=True,
    )
    _scheduler.start()
    logger.info(
        "Планировщик запущен: заказы %s мин, остатки %s мин, сверка каталога %s мин, "
        "сверка снятых 10 мин, очистка корзины WB 10 мин",
        settings.poll_interval_minutes,
        settings.stock_watch_interval_minutes,
        settings.catalog_sync_interval_minutes,
    )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
