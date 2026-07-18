"""Журнал синхронизации: последние действия с площадками и их результат.

Критично для разбора ошибок на объёме 50k книг — видно, что, куда, когда ушло
и чем закончилось.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Marketplace, SyncLog
from app.templating import templates

router = APIRouter(prefix="/log")

PAGE_SIZE = 100


@router.get("", response_class=HTMLResponse)
def log_page(
    request: Request,
    db: Session = Depends(get_db),
    marketplace: str = "",
    only_errors: str = "",
):
    stmt = select(SyncLog)
    if marketplace:
        stmt = stmt.where(SyncLog.marketplace == marketplace)
    if only_errors:
        stmt = stmt.where(SyncLog.ok == False)  # noqa: E712

    entries = db.scalars(
        stmt.order_by(SyncLog.created_at.desc()).limit(PAGE_SIZE)
    ).all()

    return templates.TemplateResponse(
        request,
        "log.html",
        {
            "entries": entries,
            "marketplaces": list(Marketplace),
            "marketplace": marketplace,
            "only_errors": only_errors,
        },
    )
