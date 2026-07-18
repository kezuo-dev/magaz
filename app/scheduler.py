"""Фоновый опрос заказов площадок.

APScheduler раз в poll_interval_minutes проходит по всем включённым площадкам,
запрашивает свежие заказы и обрабатывает продажи (пометка sold + кросс-снятие).
Работает, только пока запущен сервер — это учтено в плане (локальный режим).

Планировщик ходит в БД из отдельного потока, поэтому открывает собственную сессию
через SessionLocal (не через зависимость FastAPI).
"""
from __future__ import annotations

import logging

from apscheduler.schedulers.background import BackgroundScheduler
from sqlalchemy import select

from app.archive import sweep_to_archive
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
    _scheduler.start()
    logger.info("Планировщик запущен: опрос каждые %s мин", settings.poll_interval_minutes)


def stop_scheduler() -> None:
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None
