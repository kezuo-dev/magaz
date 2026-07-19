"""Диагностика карточки Ozon: почему товар «не создан» / не уходит на модерацию.

Запуск на сервере:
    docker compose run --rm app python diag_ozon_card.py 9авоавшоБК-001

Скрипт берёт сохранённые ключи Ozon из базы, спрашивает у Ozon полный статус
товара по offer_id (наш SKU) и печатает состояние карточки и все замечания
(что именно мешает уйти на модерацию). Ничего не меняет — только читает.
"""
import sys
import json

from sqlalchemy import select

from app.db import SessionLocal
from app.models import Book, Listing, MarketplaceAccount
from app.security import decrypt_credentials
from app.marketplaces.ozon import OzonClient


def main() -> None:
    # SKU можно передать аргументом; если не передан (кириллицу в консоли вводить
    # неудобно) — берём последнюю книгу с лотом на Ozon из базы.
    offer_id = sys.argv[1] if len(sys.argv) >= 2 else None

    with SessionLocal() as db:
        acc = db.scalar(
            select(MarketplaceAccount).where(MarketplaceAccount.marketplace == "ozon")
        )
        if not acc or not acc.credentials_encrypted:
            print("Ключи Ozon не сохранены в настройках.")
            return
        creds = decrypt_credentials(acc.credentials_encrypted)

        if not offer_id:
            listing = db.scalar(
                select(Listing)
                .where(Listing.marketplace == "ozon")
                .order_by(Listing.id.desc())
            )
            if listing:
                book = db.get(Book, listing.book_id)
                offer_id = listing.external_id or (book.sku if book else None)

    if not offer_id:
        print("Не найдено книги с лотом на Ozon. Укажите SKU: python diag_ozon_card.py <SKU>")
        return

    client = OzonClient(creds)

    # /v3/product/info/list — полный статус товара по offer_id: состояние
    # карточки, валидация, модерация и список замечаний. (/v2/product/info Ozon
    # отключил — отдаёт 404.)
    print(f"=== Запрашиваю статус товара {offer_id} у Ozon ===\n")
    try:
        data = client._post(
            "/v3/product/info/list",
            {"offer_id": [offer_id]},
        )
    except Exception as exc:  # noqa: BLE001
        print(f"Ошибка запроса статуса: {exc}")
        return

    items = (data.get("result") or {}).get("items") or data.get("items") or []
    if not items:
        print("Ozon не вернул товар по этому offer_id. Полный ответ:")
        print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
        return
    result = items[0]

    # Ключевые поля статуса. Ozon хранит состояние в блоке statuses.
    statuses = result.get("statuses") or {}
    print("Название:      ", result.get("name"))
    print("offer_id:      ", result.get("offer_id"))
    print("product_id:    ", result.get("id"))
    print()
    print("--- Статусы карточки ---")
    if statuses:
        print(json.dumps(statuses, ensure_ascii=False, indent=2))
    else:
        print("(блок statuses пуст)")

    # Причины, по которым карточка не проходит: Ozon кладёт их в errors/по полям.
    errors = (
        result.get("errors")
        or statuses.get("errors")
        or result.get("status_failed")
        or []
    )
    if errors:
        print("\n--- ЗАМЕЧАНИЯ Ozon (что мешает) ---")
        print(json.dumps(errors, ensure_ascii=False, indent=2))

    # Полный ответ — на случай, если статус лежит в неожиданном поле.
    print("\n--- Полный ответ Ozon (для разбора) ---")
    print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])


if __name__ == "__main__":
    main()
