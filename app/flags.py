"""Глобальные переключатели приложения, меняемые в UI на лету.

Пока один флаг — «Автоснятие с продажи» (auto_withdraw). Мониторинг (сверка,
статусы, опрос заказов, слежение за остатками) работает всегда. А вот реальное
снятие книги с ДРУГИХ площадок при продаже/пропаже включается этим рубильником.

По умолчанию ВЫКЛ: после установки программа сначала просто показывает каталог и
статусы, а хозяин осознанно включает автоматику, когда убедился, что всё верно.
"""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import AppSetting

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


def is_auto_withdraw_enabled(db: Session) -> bool:
    """Включён ли рубильник автоснятия. По умолчанию (нет записи) — ВЫКЛ."""
    return get_flag(db, AUTO_WITHDRAW_KEY, default=False)


def set_auto_withdraw(db: Session, on: bool) -> None:
    set_flag(db, AUTO_WITHDRAW_KEY, on)
