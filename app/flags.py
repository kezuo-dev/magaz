"""Глобальные переключатели приложения, меняемые в UI на лету.

Два флага:
- sync_enabled   — главный рубильник: останавливает ВСЕ фоновые задачи
                   (сверку, опрос заказов, остатки, корзину WB).
- auto_withdraw  — кросс-снятие: когда книга продалась на одной площадке,
                   снимать её с остальных. Работает только при sync_enabled=True.

По умолчанию оба ВЫКЛ.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppSetting

SYNC_ENABLED_KEY = "sync_enabled"
AUTO_WITHDRAW_KEY = "auto_withdraw"


def get_flag(db: Session, key: str, default: bool = False) -> bool:
    row = db.get(AppSetting, key)
    if row is None:
        return default
    return row.value == "1"


def set_flag(db: Session, key: str, value: bool) -> None:
    row = db.get(AppSetting, key)
    if row is None:
        row = AppSetting(key=key)
        db.add(row)
    row.value = "1" if value else "0"


def is_sync_enabled(db: Session) -> bool:
    """Главный рубильник — все фоновые задачи. По умолчанию ВЫКЛ."""
    return get_flag(db, SYNC_ENABLED_KEY, default=False)


def set_sync_enabled(db: Session, on: bool) -> None:
    set_flag(db, SYNC_ENABLED_KEY, on)


def is_auto_withdraw_enabled(db: Session) -> bool:
    """Включён ли рубильник автоснятия. По умолчанию (нет записи) — ВЫКЛ."""
    return get_flag(db, AUTO_WITHDRAW_KEY, default=False)


def set_auto_withdraw(db: Session, on: bool) -> None:
    set_flag(db, AUTO_WITHDRAW_KEY, on)
