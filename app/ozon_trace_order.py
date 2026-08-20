"""Диагностика: история статусов отправления Ozon для одного заказа.

Помогает понять, почему программа решила «восстановить карточку в продажу»
при отмене. Запрашивает у Ozon /v3/posting/fbs/list по номеру отправления и
печатает его статусы + историю. Ничего не меняет — только читает.

Запуск короткой строкой в контейнере:
    docker compose exec app python3 -m app.ozon_trace_order "32271668-5024-1"
"""
from __future__ import annotations

import sys

from sqlalchemy import select

from app.db import SessionLocal
from app.marketplaces import get_client
from app.marketplaces.base import MarketplaceError
from app.models import MarketplaceAccount
from app.security import decrypt_credentials

# Статусы, при которых книга уже передана в доставку (то же, что в ozon.py)
SHIPPED_STATUSES = {"awaiting_deliver", "delivering", "delivered", "driver_pickup"}


def main() -> int:
    if len(sys.argv) < 2:
        print("Использование: python3 -m app.ozon_trace_order <posting_number>")
        return 2
    posting_number = sys.argv[1].strip()
    print(f"Спрашиваю Ozon про отправление: {posting_number}\n")

    db = SessionLocal()
    try:
        account = db.scalar(
            select(MarketplaceAccount).where(MarketplaceAccount.marketplace == "ozon")
        )
    finally:
        pass

    if not account or not account.enabled or not account.credentials_encrypted:
        print("Ozon выключен или нет ключей — проверить нечего.")
        return 1

    creds = decrypt_credentials(account.credentials_encrypted)
    client = get_client("ozon", creds)

    from datetime import datetime, timedelta, timezone

    now = datetime.now(timezone.utc)
    since = now - timedelta(days=7)
    fmt = "%Y-%m-%dT%H:%M:%S.000Z"

    try:
        data = client._post(
            "/v3/posting/fbs/list",
            {
                "dir": "DESC",
                "filter": {
                    "since": since.strftime(fmt),
                    "to": now.strftime(fmt),
                    "status": "cancelled",
                },
                "limit": 10,
                "offset": 0,
                "with": {"status_history": True},
            },
        )
    except MarketplaceError as exc:
        print(f"Ошибка Ozon: {exc}")
        return 1

    postings = (data.get("result") or {}).get("postings") or []
    print(f"Найдено отменённых отправлений в окне: {len(postings)}\n")

    found = False
    for posting in postings:
        num = posting.get("posting_number") or posting.get("order_number")
        if num != posting_number:
            continue
        found = True
        print(f"=== Отправление {num} ===")
        print(f"  статус: {posting.get('status')}")
        print(f"  in_process_at: {posting.get('in_process_at')}")
        print(f"  deliver_date: {posting.get('deliver_date')}")
        # Печатаем ВСЕ поля отправления — ищем признаки отгрузки помимо пустой
        # истории статусов (shipment_date, delivering_date, tracking_number…).
        other = {k: v for k, v in posting.items() if k not in ("products",)}
        print(f"  все поля отправления ({len(other)}):")
        for k, v in other.items():
            print(f"    {k} = {v}")
        history = posting.get("status_history") or []
        print(f"  история статусов ({len(history)}):")
        for h in history:
            print(f"    - {h.get('time')}  {h.get('status')}")
        cancellation = posting.get("cancellation") or {}
        cancelled_after_ship = bool(cancellation.get("cancelled_after_ship"))
        cancel_reason_id = str(cancellation.get("cancel_reason_id"))
        seller_out_of_stock = bool(
            cancellation.get("cancellation_type") == "seller"
            and cancel_reason_id == "352"
        )
        print(f"  delivering_date: {posting.get('delivering_date')}")
        print(f"  cancellation.cancelled_after_ship: {cancelled_after_ship}")
        print(f"  cancellation_type: {cancellation.get('cancellation_type')} "
              f"(reason_id={cancel_reason_id}, {cancellation.get('cancel_reason')})")
        print(f"  seller_out_of_stock (352): {seller_out_of_stock}")
        past = {h.get("status") for h in history if h.get("status")}
        shipped = bool(
            (past & SHIPPED_STATUSES)
            or posting.get("delivering_date")
            or cancelled_after_ship
        )
        print(f"\n  Уже было в доставке (already_shipped)? {shipped}")
        if seller_out_of_stock:
            verdict = "НЕ возвращать в продажу: продавец отменил — товар закончился на складе"
        elif shipped:
            verdict = "НЕ должна была восстанавливать (книга в пути)"
        else:
            verdict = "правильно восстановила (отмена до отгрузки)"
        print(f"  → программа: {verdict}")
        break

    if not found:
        print(f"Отправление {posting_number} не нашлось в отменённых за 7 дней.")
        print("Может быть не cancelled, или номер другой (без суффикса #артикул).")

    db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
