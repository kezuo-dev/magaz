"""Скрипт для обновления external_id у WB-лотов: заменяет vendorCode на nmID.

Запуск:
    docker compose exec app python3 -m app.fix_wb_nmid

Проходит по всем WB-лотам, у которых external_id не число (старый формат с
vendorCode), запрашивает у WB API nmID по vendorCode и обновляет базу.
"""
from __future__ import annotations

import sys

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.db import SessionLocal
from app.marketplaces import MarketplaceError, get_client
from app.models import Book, Listing, MarketplaceAccount
from app.security import decrypt_credentials


def main():
    db = SessionLocal()
    try:
        # Проверяем настройки WB
        account = db.scalar(
            select(MarketplaceAccount).where(MarketplaceAccount.marketplace == "wildberries")
        )
        if not account or not account.enabled or not account.credentials_encrypted:
            print("Wildberries не настроен или выключен")
            return

        creds = decrypt_credentials(account.credentials_encrypted)
        client = get_client("wildberries", creds)

        # Находим все WB-лоты с external_id не числом (vendorCode вместо nmID)
        listings = db.scalars(
            select(Listing)
            .options(selectinload(Listing.book))
            .where(Listing.marketplace == "wildberries")
        ).all()

        need_update = []
        for listing in listings:
            try:
                int(listing.external_id)  # если число — пропускаем
            except (ValueError, TypeError):
                need_update.append(listing)

        if not need_update:
            print("Все WB-лоты уже содержат nmID")
            return

        print(f"Найдено {len(need_update)} WB-лотов с vendorCode в external_id")
        print("Запрашиваем nmID у WB...")

        # Запрашиваем каталог WB целиком — там есть маппинг vendorCode → nmID
        try:
            catalog = client.fetch_catalog()
        except MarketplaceError as exc:
            print(f"Ошибка загрузки каталога WB: {exc}")
            return

        # Строим индекс vendorCode → nmID
        vc_to_nm = {}
        for row in catalog:
            sku = row.get("sku")
            ext_id = row.get("external_id")
            if sku and ext_id:
                try:
                    nm_id = int(ext_id)
                    vc_to_nm[str(sku)] = nm_id
                except (ValueError, TypeError):
                    pass

        print(f"Получено {len(vc_to_nm)} карточек с nmID из каталога WB")

        # Обновляем лоты
        updated = 0
        not_found = []
        for listing in need_update:
            vendor_code = listing.external_id
            nm_id = vc_to_nm.get(vendor_code)
            if nm_id:
                listing.external_id = str(nm_id)
                updated += 1
                print(f"✓ {listing.book.sku}: vendorCode={vendor_code} → nmID={nm_id}")
            else:
                not_found.append(listing.book.sku)
                print(f"✗ {listing.book.sku}: карточка с vendorCode={vendor_code} не найдена в каталоге WB")

        db.commit()
        print(f"\nОбновлено: {updated}")
        if not_found:
            print(f"Не найдено в каталоге WB ({len(not_found)}): {', '.join(not_found)}")
            print("Возможно эти карточки удалены из каталога или лежат в корзине")

    except Exception as exc:
        db.rollback()
        print(f"Ошибка: {exc}", file=sys.stderr)
        raise
    finally:
        db.close()


if __name__ == "__main__":
    main()
