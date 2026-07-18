"""Разовый экспорт: артикулы книг, которые есть ИСКЛЮЧИТЕЛЬНО на Wildberries.

Книга попадает в выборку, если у неё есть лот на wildberries и НЕТ лотов ни на
одной другой площадке. Артикулы (SKU) пишем по одному в строку в текстовый файл.
"""
from app.db import SessionLocal
from app.models import Book, BookStatus, Listing, ListingStatus

OUT_FILE = "wb_only_sku.txt"

with SessionLocal() as s:
    books = s.query(Book).all()
    wb_only = []
    for b in books:
        marketplaces = {l.marketplace for l in b.listings}
        # Только WB и ничего больше (и лот на WB действительно есть).
        if marketplaces != {"wildberries"}:
            continue
        # Не в архиве.
        if b.archived_at is not None:
            continue
        # В наличии (не продана, не снята).
        if b.status != BookStatus.IN_STOCK:
            continue
        # Лот на WB активен — то есть реально выставлен на продажу.
        wb_lot = next((l for l in b.listings if l.marketplace == "wildberries"), None)
        if not wb_lot or wb_lot.status != ListingStatus.ACTIVE:
            continue
        wb_only.append(b)

    # Сортируем по артикулу для стабильного, читаемого списка.
    skus = sorted((b.sku or "").strip() for b in wb_only if (b.sku or "").strip())

with open(OUT_FILE, "w", encoding="utf-8") as f:
    f.write("\n".join(skus))
    if skus:
        f.write("\n")

print(f"Книг только на WB: {len(wb_only)}")
print(f"Артикулов записано: {len(skus)}")
print(f"Файл: {OUT_FILE}")
