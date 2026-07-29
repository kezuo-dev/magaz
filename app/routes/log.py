"""Журнал синхронизации: последние действия с площадками и их результат.

Критично для разбора ошибок на объёме 50k книг — видно, что, куда, когда ушло
и чем закончилось.
"""
from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, JSONResponse
from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import Marketplace, SyncLog
from app.templating import marketplace_label, templates

router = APIRouter(prefix="/log")

PAGE_SIZE = 100


@router.get("", response_class=HTMLResponse)
def log_page(
    request: Request,
    db: Session = Depends(get_db),
    q: str = "",
    marketplace: str = "",
    only_errors: str = "",
):
    stmt = select(SyncLog)
    if q:
        # Поиск по сообщению или артикулу (артикулы часто попадают в message)
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{needle}%"
        stmt = stmt.where(SyncLog.message.ilike(like, escape="\\"))
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
            "q": q,
            "marketplace": marketplace,
            "only_errors": only_errors,
        },
    )


@router.get("/api/log")
def api_log(
    db: Session = Depends(get_db),
    q: str = "",
    marketplace: str = "",
    only_errors: str = "",
):
    """JSON-фрагмент журнала для живого поиска."""
    stmt = select(SyncLog)
    if q:
        needle = q.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like = f"%{needle}%"
        stmt = stmt.where(SyncLog.message.ilike(like, escape="\\"))
    if marketplace:
        stmt = stmt.where(SyncLog.marketplace == marketplace)
    if only_errors:
        stmt = stmt.where(SyncLog.ok == False)  # noqa: E712

    entries = db.scalars(
        stmt.order_by(SyncLog.created_at.desc()).limit(PAGE_SIZE)
    ).all()

    items = []
    for e in entries:
        items.append(
            {
                "created_at": e.created_at.strftime("%d.%m %H:%M:%S"),
                "marketplace": marketplace_label(e.marketplace) if e.marketplace else "—",
                "action": e.action,
                "ok": e.ok,
                "message": e.message or "",
            }
        )
    return JSONResponse({"items": items})
