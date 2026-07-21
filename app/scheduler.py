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

from app.archive import sweep_to_archive
from app.catalog_sync import sync_all, watch_all_stocks
from app.config import settings
from app.db import SessionLocal
from app.models import MarketplaceAccount
from app.sync import poll_marketplace_orders

logger = logging.getLogger("scheduler")

_scheduler: BackgroundScheduler | None = None


def poll_all_marketplaces() -> None:
    """Один проход опроса по всем включённым площадкам. Ошибки не роняют планировщик."""
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

        # После обработки заказов переносим в архив всё, у чего истекло окно ожидания.
        try:
            moved = sweep_to_archive(db)
            db.commit()
            if moved:
                logger.info("Перенесено в архив книг: %s", moved)
        except Exception:  # noqa: BLE001
            db.rollback()
            logger.exception("Сбой переноса книг в архив")
    finally:
        db.close()


def watch_all_marketplaces_stocks() -> None:
    """Один проход слежения за остатками наших книг по всем включённым площадкам.

    Дёшево (спрашиваем остатки только по нашим ключам), поэтому идёт часто. Остаток
    0 / пропавшая карточка → кросс-снятие. watch_all_stocks сам коммитит и не роняет
    планировщик на сбое.
    """
    db = SessionLocal()
    try:
        results = watch_all_stocks(db)
        removed = sum(r.get("removed", 0) for r in results.values() if isinstance(r, dict))
        if removed:
            logger.info("Слежение за остатками: снято книг %s (%s)", removed, results)
    except Exception:  # noqa: BLE001 — сбой слежения не должен ронять планировщик
        db.rollback()
        logger.exception("Сбой слежения за остатками")
    finally:
        db.close()


def sync_all_catalogs() -> None:
    """Один проход полной сверки каталога по всем включённым площадкам.

    Тяжелее опроса заказов (тянет все карточки), поэтому идёт по своему, более
    редкому интервалу. Находит НОВЫЕ книги и снимает пропавшие. sync_all сам
    коммитит и не роняет планировщик на сбое.
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
    _scheduler.start()
    logger.info(
        "Планировщик запущен: заказы %s мин, остатки %s мин, сверка каталога %s мин",
        settings.poll_interval_minutes,
        settings.stock_watch_interval_minutes,
        settings.catalog_sync_interval_minutes,
    )


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
