"""Импорт каталога из выгрузок площадок (CSV/XLSX).

Логика в два шага:
1. Загружаем файл → показываем его колонки и просим сопоставить с полями книги.
2. По сопоставлению создаём/обновляем книги. Сопоставление одинаковых книг между
   площадками идёт по SKU или ISBN — если совпало, дополняем существующую книгу
   лотом нужной площадки, а не плодим дубли.
"""
import csv
import io
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.catalog_sync import TARGET_FIELDS, sync_all, sync_marketplace, upsert_catalog_rows
from app.db import get_db
from app.marketplaces import MarketplaceError, is_supported
from app.models import Marketplace, MarketplaceAccount, SyncLog
from app.templating import templates

router = APIRouter(prefix="/import")

# Словарь автосопоставления: какие заголовки колонок в выгрузках Ozon/WB
# соответствуют полям книги. Сравниваем по подстроке в нижнем регистре, поэтому
# хватает характерных кусков названий ("артикул", "цена", "штрихкод" и т.д.).
# Порядок важен: первое совпадение выигрывает.
COLUMN_ALIASES = {
    "sku": ["артикул продавца", "артикул", "offer_id", "sku", "ваш sku", "код товара"],
    "title": ["название товара", "наименование", "название", "заголовок", "title", "name"],
    "author": [
        "автор", "авторы", "author", "автор книги", "автор(ы)",
        "составитель", "писатель", "author name", "авт.",
    ],
    "isbn": ["isbn", "штрихкод", "штрих-код", "barcode", "ean"],
    "publisher": ["издательство", "бренд", "publisher", "brand"],
    "year": ["год выпуска", "год издания", "год", "year"],
    "condition": ["состояние", "качество", "condition"],
    "price": ["цена продажи", "текущая цена", "цена", "price"],
    "description": ["описание", "аннотация", "description"],
    "external_id": [
        "ozon product id", "product id", "product_id", "id товара",
        "ozon id", "sku ozon", "артикул ozon",
    ],
}

# Автоопределение площадки по характерным колонкам выгрузки.
MARKETPLACE_HINTS = {
    "ozon": ["ozon", "offer_id", "артикул ozon", "fbo", "fbs"],
    "wildberries": ["wildberries", "wb", "номенклатура", "предмет", "баркод"],
}


def _auto_map(columns: list[str]) -> dict[str, str]:
    """Подобрать колонку под каждое поле книги по словарю синонимов.

    Возвращает {поле: имя_колонки}. Одну колонку не назначаем двум полям.
    """
    lowered = {c: (c or "").strip().lower() for c in columns}
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for field, aliases in COLUMN_ALIASES.items():
        for alias in aliases:
            match = next(
                (c for c in columns if c not in used and alias in lowered[c]), None
            )
            if match:
                mapping[field] = match
                used.add(match)
                break
    return mapping


def _guess_marketplace(columns: list[str], filename: str) -> str | None:
    """Угадать площадку по колонкам и имени файла. None — если непонятно."""
    haystack = " ".join(columns).lower() + " " + (filename or "").lower()
    for mp, hints in MARKETPLACE_HINTS.items():
        if any(h in haystack for h in hints):
            return mp
    return None


# Простое хранилище загруженного файла между шагом 1 и шагом 2 (по сессии).
_uploads: dict[str, list[dict]] = {}


def _parse_file(filename: str, raw: bytes) -> list[dict]:
    """Читаем CSV или XLSX в список словарей {колонка: значение}."""
    name = filename.lower()
    if name.endswith(".csv"):
        text = raw.decode("utf-8-sig", errors="replace")
        # Пытаемся угадать разделитель (Ozon/WB часто отдают ; )
        sample = text[:5000]
        delimiter = ";" if sample.count(";") > sample.count(",") else ","
        reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
        return [dict(row) for row in reader]
    if name.endswith(".xls") and not name.endswith(".xlsx"):
        raise ValueError(
            "Старый формат .xls не поддерживается. Откройте файл в Excel и "
            "сохраните как .xlsx (или экспортируйте в CSV)."
        )
    if name.endswith(".xlsx"):
        from openpyxl import load_workbook

        # Намеренно НЕ используем read_only: у выгрузок Ozon/1С часто указан
        # неверный размер листа, из-за чего быстрый парсер читает лишь первую
        # строку. Обычный режим читает лист целиком, пусть и медленнее.
        try:
            wb = load_workbook(io.BytesIO(raw), data_only=True)
        except Exception as exc:
            raise ValueError(f"Не удалось открыть файл Excel: {exc}") from exc
        ws = wb.active

        rows_iter = ws.iter_rows(values_only=True)

        def nonempty(row):
            return [c for c in row if c is not None and str(c).strip() != ""]

        # Ищем строку-шапку: первую, где заполнено хотя бы две ячейки. Так
        # пропускаем титульные строки отчёта ("Отчёт по товарам" в одной ячейке).
        headers = None
        for row in rows_iter:
            if len(nonempty(row)) >= 2:
                headers = [str(h).strip() if h is not None else "" for h in row]
                break
        if not headers:
            return []

        result = []
        for row in rows_iter:
            if not nonempty(row):  # пропускаем полностью пустые строки
                continue
            result.append(
                {headers[i]: (row[i] if i < len(row) else None) for i in range(len(headers))}
            )
        return result
    raise ValueError("Поддерживаются только CSV и XLSX")


@router.get("", response_class=HTMLResponse)
def import_start(request: Request, db: Session = Depends(get_db)):
    # Кнопки «Загрузить из ...» активны только для площадок с включёнными ключами.
    return templates.TemplateResponse(
        request,
        "import_start.html",
        {"marketplaces": list(Marketplace), "sources": _sources(db)},
    )


@router.post("/pull/{marketplace}", response_class=HTMLResponse)
def import_pull(marketplace: str, request: Request, db: Session = Depends(get_db)):
    """Полная сверка каталога одной площадки по сохранённым ключам.

    Тянет каталог, апсертит книги и снимает пропавшее (кросс-снятие). Это та же
    операция, что и фоновая сверка, но запущенная вручную по конкретной площадке.
    """
    def fail(msg: str):
        db.add(SyncLog(marketplace=marketplace, action="import_pull", ok=False, message=msg))
        db.commit()
        return templates.TemplateResponse(
            request,
            "import_start.html",
            {"marketplaces": list(Marketplace), "sources": _sources(db), "error": msg},
            status_code=400,
        )

    if not is_supported(marketplace):
        return fail(f"Площадка «{marketplace}» не поддерживает загрузку по API")

    try:
        result = sync_marketplace(db, marketplace)
        db.commit()
    except MarketplaceError as exc:
        return fail(f"Не удалось сверить каталог: {exc}")
    except Exception as exc:  # noqa: BLE001 — любой сбой показываем как есть
        db.rollback()
        return fail(f"Ошибка сверки: {exc}")

    return templates.TemplateResponse(
        request,
        "import_done.html",
        {**result, "marketplace": marketplace, "auto": True, "via_api": True},
    )


@router.post("/sync")
def import_sync(request: Request, db: Session = Depends(get_db)):
    """Кнопка «Обновить каталог»: полная сверка всех включённых площадок сразу."""
    results = sync_all(db)
    if not results:
        return RedirectResponse("/?synced=" + quote("нет включённых площадок"), status_code=303)

    parts = []
    for mp, res in results.items():
        if "error" in res:
            parts.append(f"{mp}: ошибка")
        else:
            parts.append(
                f"{mp}: +{res['created']} новых, {res['updated']} обновлено, "
                f"{res['removed']} снято"
            )
    return RedirectResponse("/?synced=" + quote("; ".join(parts)), status_code=303)


def _sources(db: Session) -> list[dict]:
    """Список площадок с признаком готовности (ключи включены) для шаблона."""
    accounts = {a.marketplace: a for a in db.scalars(select(MarketplaceAccount)).all()}
    out = []
    for mp in Marketplace:
        acc = accounts.get(mp.value)
        out.append(
            {
                "marketplace": mp.value,
                "ready": bool(
                    is_supported(mp.value) and acc and acc.enabled and acc.credentials_encrypted
                ),
            }
        )
    return out


@router.post("/upload", response_class=HTMLResponse)
async def import_upload(
    request: Request,
    db: Session = Depends(get_db),
    file: UploadFile = File(...),
    marketplace: str = Form(""),
):
    """Загрузка выгрузки. Площадку и сопоставление колонок определяем сами.

    Если удалось распознать SKU/название — импортируем сразу, без лишних шагов.
    Показываем экран сопоставления только когда автоопределение не справилось.
    """
    raw = await file.read()
    try:
        rows = _parse_file(file.filename or "", raw)
    except Exception as exc:
        return templates.TemplateResponse(
            request,
            "import_start.html",
            {"marketplaces": list(Marketplace), "error": str(exc)},
            status_code=400,
        )

    if not rows:
        return templates.TemplateResponse(
            request,
            "import_start.html",
            {"marketplaces": list(Marketplace), "error": "Файл пустой"},
            status_code=400,
        )

    columns = list(rows[0].keys())

    # Площадку берём из формы, если выбрали вручную, иначе угадываем по файлу.
    if not marketplace:
        marketplace = _guess_marketplace(columns, file.filename or "") or "ozon"

    token = f"{marketplace}:{file.filename}"
    _uploads[token] = rows
    request.session["import_token"] = token
    request.session["import_marketplace"] = marketplace

    # Пробуем полностью автоматический импорт.
    auto = _auto_map(columns)
    if auto.get("sku") or auto.get("title"):
        result = _do_import(db, marketplace, rows, auto)
        _uploads.pop(token, None)
        return templates.TemplateResponse(
            request,
            "import_done.html",
            {**result, "marketplace": marketplace, "auto": True},
        )

    # Автоопределение не нашло даже название/артикул — просим сопоставить вручную.
    return templates.TemplateResponse(
        request,
        "import_map.html",
        {
            "columns": columns,
            "target_fields": TARGET_FIELDS,
            "sample": rows[:5],
            "marketplace": marketplace,
            "total": len(rows),
            "auto_map": auto,
        },
    )


@router.post("/run")
async def import_run(request: Request, db: Session = Depends(get_db)):
    form = await request.form()
    token = request.session.get("import_token")
    marketplace = request.session.get("import_marketplace")
    rows = _uploads.get(token)
    if not rows:
        return RedirectResponse("/import", status_code=303)

    # mapping: поле_книги -> имя_колонки_в_файле
    mapping = {
        field: form.get(f"map_{field}", "")
        for field in TARGET_FIELDS
        if form.get(f"map_{field}")
    }

    result = _do_import(db, marketplace, rows, mapping)
    _uploads.pop(token, None)
    return templates.TemplateResponse(
        request,
        "import_done.html",
        {**result, "marketplace": marketplace},
    )


def _do_import(db: Session, marketplace: str, rows: list[dict], mapping: dict) -> dict:
    """Импорт из файла: апсерт книг без сверки пропавших.

    В отличие от полной сверки по API, файл может быть частичной выгрузкой,
    поэтому НЕ снимаем книги, которых в файле нет (reconcile не запускаем).
    Общий апсерт живёт в catalog_sync.upsert_catalog_rows.
    """
    result = upsert_catalog_rows(db, marketplace, rows, mapping)
    db.add(
        SyncLog(
            marketplace=marketplace,
            action="import",
            ok=True,
            message=(f"Импорт файлом: создано {result['created']}, "
                     f"обновлено {result['updated']}, пропущено {result['skipped']}"),
        )
    )
    db.commit()
    return {"created": result["created"], "updated": result["updated"], "skipped": result["skipped"]}
