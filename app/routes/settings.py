"""Настройки подключения площадок: ввод ключей, включение/выключение, проверка связи.

Ключи храним шифрованно (Fernet) в MarketplaceAccount. В форму реальные значения
секретов не выводим — показываем только «ключи сохранены».
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.flags import is_auto_withdraw_enabled, set_auto_withdraw
from app.marketplaces import MarketplaceError, get_client, is_supported
from app.models import Marketplace, MarketplaceAccount, SyncLog
from app.security import decrypt_credentials, encrypt_credentials
from app.templating import templates

router = APIRouter(prefix="/settings")

# Какие поля ключей нужны каждой площадке. Пока живой клиент есть только у Ozon.
CREDENTIAL_FIELDS = {
    "ozon": [
        ("client_id", "Client-Id"),
        ("api_key", "Api-Key"),
        ("warehouse_id", "ID склада FBS (Мои склады, для остатков)"),
    ],
    "wildberries": [
        ("api_token", "API-токен"),
        ("warehouse_id", "ID склада FBS (для остатков)"),
    ],
}


def _accounts_by_mp(db: Session) -> dict[str, MarketplaceAccount]:
    rows = db.scalars(select(MarketplaceAccount)).all()
    return {a.marketplace: a for a in rows}


@router.get("", response_class=HTMLResponse)
def settings_page(request: Request, db: Session = Depends(get_db), saved: str = "", checked: str = "", withdraw: str = ""):
    accounts = _accounts_by_mp(db)
    cards = []
    for mp in Marketplace:
        acc = accounts.get(mp.value)
        cards.append(
            {
                "marketplace": mp.value,
                "fields": CREDENTIAL_FIELDS.get(mp.value, []),
                "enabled": bool(acc and acc.enabled),
                "has_credentials": bool(acc and acc.credentials_encrypted),
                "supported": is_supported(mp.value),
                "updated_at": acc.updated_at if acc else None,
            }
        )
    return templates.TemplateResponse(
        request,
        "settings.html",
        {
            "cards": cards,
            "saved": saved,
            "checked": checked,
            "withdraw": withdraw,
            "auto_withdraw": is_auto_withdraw_enabled(db),
        },
    )


@router.post("/auto-withdraw")
def toggle_auto_withdraw(
    db: Session = Depends(get_db),
    enabled: str = Form(""),
):
    """Включить/выключить глобальное автоснятие с продажи.

    Мониторинг (сверка каталога, статусы, опрос заказов) работает всегда. Этот
    рубильник управляет только реальным снятием книги с других площадок.
    """
    on = enabled == "on"
    set_auto_withdraw(db, on)
    db.add(SyncLog(marketplace=None, action="auto_withdraw_toggle", ok=True,
                   message="Автоснятие включено" if on else "Автоснятие выключено"))
    db.commit()
    return RedirectResponse(f"/settings?withdraw={'on' if on else 'off'}", status_code=303)


@router.post("/save")
async def save_credentials(
    request: Request,
    db: Session = Depends(get_db),
    marketplace: str = Form(...),
    enabled: str = Form(""),
):
    """Сохранить ключи площадки. Пустые поля не затирают уже сохранённые секреты."""
    form = await request.form()
    fields = CREDENTIAL_FIELDS.get(marketplace, [])

    account = db.scalar(
        select(MarketplaceAccount).where(MarketplaceAccount.marketplace == marketplace)
    )
    if account is None:
        account = MarketplaceAccount(marketplace=marketplace)
        db.add(account)

    # Берём текущие секреты, чтобы не потерять поля, оставленные пустыми в форме.
    current = {}
    if account.credentials_encrypted:
        try:
            current = decrypt_credentials(account.credentials_encrypted)
        except Exception:
            current = {}

    for key, _label in fields:
        val = (form.get(f"cred_{key}") or "").strip()
        if val:
            current[key] = val

    if current:
        account.credentials_encrypted = encrypt_credentials(current)
    account.enabled = enabled == "on"

    db.commit()
    return RedirectResponse(f"/settings?saved={marketplace}", status_code=303)


@router.post("/check")
def check_connection(
    request: Request,
    db: Session = Depends(get_db),
    marketplace: str = Form(...),
):
    """Проверить связь с площадкой на сохранённых ключах."""
    account = db.scalar(
        select(MarketplaceAccount).where(MarketplaceAccount.marketplace == marketplace)
    )
    ok = False
    message = "Ключи не сохранены"
    if account and account.credentials_encrypted:
        try:
            creds = decrypt_credentials(account.credentials_encrypted)
            client = get_client(marketplace, creds)
            client.check_connection()
            ok = True
            message = "Подключение успешно"
        except MarketplaceError as exc:
            message = str(exc)
        except Exception as exc:  # noqa: BLE001 — любой сбой показываем как есть
            message = f"Ошибка проверки: {exc}"

    db.add(SyncLog(marketplace=marketplace, action="check_connection", ok=ok, message=message))
    db.commit()
    status = "ok" if ok else "err"
    return RedirectResponse(f"/settings?checked={marketplace}:{status}", status_code=303)
