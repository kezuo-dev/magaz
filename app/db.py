"""Подключение к базе. SQLite локально, PostgreSQL на проде — разница только в DATABASE_URL."""
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

# check_same_thread нужен только для SQLite (фоновый планировщик ходит из другого потока).
connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)

# WAL-режим для SQLite: читатели не блокируют писателей и наоборот.
# Без него планировщик (сверка каталога, опрос заказов) держал эксклюзивную
# блокировку на весь файл — каждый HTTP-запрос в этот момент висел.
# busy_timeout уже выставлен в 5 сек через connect_args ниже — на случай
# одновременной записи двух фоновых задач.
if settings.database_url.startswith("sqlite"):
    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(dbapi_conn, _):
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA journal_mode=WAL")
        cur.execute("PRAGMA synchronous=NORMAL")   # быстрее DELETE, безопасно с WAL
        cur.execute("PRAGMA cache_size=-8000")     # 8 МБ кэша вместо 2 МБ
        cur.execute("PRAGMA busy_timeout=5000")    # ждать до 5 сек вместо сразу упасть
        cur.close()
SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False)


class Base(DeclarativeBase):
    pass


def get_db():
    """Зависимость FastAPI: одна сессия на запрос."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def ensure_schema() -> None:
    """Лёгкие идемпотентные миграции для дев-режима на SQLite.

    create_all() создаёт недостающие таблицы, но не добавляет новые колонки в уже
    существующие. Дописываем их вручную, чтобы обновление кода не требовало ручной
    правки базы. На проде эту роль играет alembic.
    """
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())

    existing = {col["name"] for col in inspector.get_columns("books")} if "books" in tables else set()
    additions = {
        "ozon_category_id": "ALTER TABLE books ADD COLUMN ozon_category_id VARCHAR(32)",
        "ozon_type_id": "ALTER TABLE books ADD COLUMN ozon_type_id VARCHAR(32)",
        "wb_subject_id": "ALTER TABLE books ADD COLUMN wb_subject_id VARCHAR(32)",
        "ozon_direction_id": "ALTER TABLE books ADD COLUMN ozon_direction_id VARCHAR(32)",
        "ozon_direction_name": "ALTER TABLE books ADD COLUMN ozon_direction_name VARCHAR(128)",
        "weight_grams": "ALTER TABLE books ADD COLUMN weight_grams INTEGER",
        "length_mm": "ALTER TABLE books ADD COLUMN length_mm INTEGER",
        "width_mm": "ALTER TABLE books ADD COLUMN width_mm INTEGER",
        "height_mm": "ALTER TABLE books ADD COLUMN height_mm INTEGER",
        # Остаток по умолчанию 1 — у уже заведённых книг он проставится этим же.
        "quantity": "ALTER TABLE books ADD COLUMN quantity INTEGER DEFAULT 1",
    }
    if existing:
        with engine.begin() as conn:
            for column, ddl in additions.items():
                if column not in existing:
                    conn.execute(text(ddl))

    # Колонки таблицы listings, появившиеся позже (слежение за остатками).
    # Проверяем независимо от books: раньше ранний return при отсутствии books
    # пропускал эту миграцию целиком.
    if "listings" in tables:
        listing_cols = {col["name"] for col in inspector.get_columns("listings")}
        listing_additions = {
            "stock_key": "ALTER TABLE listings ADD COLUMN stock_key VARCHAR(128)",
            # Признак «отменена после отгрузки, в продажу никогда не возвращать».
            # DEFAULT FALSE, а не 0: Postgres не приводит integer к boolean и
            # отвергает такой ALTER — на проде это ронял старт приложения целиком.
            "removed_from_sale": (
                "ALTER TABLE listings ADD COLUMN removed_from_sale BOOLEAN NOT NULL DEFAULT FALSE"
            ),
            # Счётчик неудачных попыток корзины WB и флаг «битой» карточки.
            "trash_failures": "ALTER TABLE listings ADD COLUMN trash_failures INTEGER DEFAULT 0",
            "trash_blocked": (
                "ALTER TABLE listings ADD COLUMN trash_blocked BOOLEAN NOT NULL DEFAULT FALSE"
            ),
        }
        with engine.begin() as conn:
            for column, ddl in listing_additions.items():
                if column not in listing_cols:
                    conn.execute(text(ddl))

    # Колонка cancelled в таблице orders для обработки отменённых заказов.
    # DEFAULT FALSE, а не 0: Postgres не приводит integer к boolean и отвергает
    # такой ALTER — на проде это ронял старт приложения целиком.
    if "orders" in tables:
        order_cols = {col["name"] for col in inspector.get_columns("orders")}
        if "cancelled" not in order_cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE orders ADD COLUMN cancelled BOOLEAN NOT NULL DEFAULT FALSE"))

    # Статус «draft» («черновик») убран из логики: программа ничего не создаёт,
    # она зеркалит площадки. Старые записи переводим в in_stock, иначе они висели
    # бы с несуществующим статусом и не попадали ни в один фильтр.
    if "books" in tables:
        with engine.begin() as conn:
            conn.execute(text("UPDATE books SET status = 'in_stock' WHERE status = 'draft'"))

    # Помечаем лоты WB, которые были успешно удалены в корзину до появления
    # статуса TRASHED. Без этого те же карточки снова попадают в очередь,
    # вызывают лишние запросы к API и получают 429.
    #
    # ДВА ПРОХОДА (не один UPDATE с коррелированным подзапросом):
    # 1. По book_id: старые записи wb_trash ссылались на книгу напрямую.
    # 2. По SKU из message: с 2026-08 итоговые записи перечисляют SKU одной
    #    строкой «... | Удалены: SKU1 (nmID), SKU2 (nmID), ...».
    #
    # ВАЖНО ПРО СКОРОСТЬ: прежняя версия (коррелированный EXISTS с JOIN и
    # LIKE по message на КАЖДУЮ строку listings) на проде с сотнями тысяч записей
    # sync_log выполнялась минутами/часами и вешала старт приложения (белый
    # экран). Поэтому первый проход делает ОДИН проход по sync_log без JOIN с
    # listings (только DISTINCT book_id), а второй — один JOIN и одну сверку,
    # без повторных LIKE на каждую строку listings.
    if "listings" in tables and "sync_log" in tables and "books" in tables:
        with engine.begin() as conn:
            # Проход 1: лоты, чьи книги прямо указаны в старых записях wb_trash.
            conn.execute(text("""
                UPDATE listings
                SET status = 'trashed'
                WHERE marketplace = 'wildberries'
                  AND status != 'trashed'
                  AND book_id IN (
                      SELECT DISTINCT book_id FROM sync_log
                      WHERE action = 'wb_trash'
                        AND ok = TRUE
                        AND message LIKE '%удалена в корзину WB%'
                        AND book_id IS NOT NULL
                  )
            """))
            # Проход 2: книги, чей SKU упомянут в итоговых записях без book_id.
            # Чтобы не гонять LIKE по всему журналу для каждой книги, сначала
            # собираем пары (book_id, sku) по непустым ответам, потом удаляем.
            conn.execute(text("""
                UPDATE listings
                SET status = 'trashed'
                WHERE marketplace = 'wildberries'
                  AND status != 'trashed'
                  AND book_id IN (
                      SELECT b.id
                      FROM sync_log l
                      JOIN books b ON b.sku IS NOT NULL AND b.sku != ''
                      WHERE l.action = 'wb_trash'
                        AND l.ok = TRUE
                        AND l.message LIKE '%удалена в корзину WB%'
                        AND l.book_id IS NULL
                        AND l.message LIKE '%' || b.sku || '%'
                  )
            """))

    # Backfill: помечаем removed_from_sale=True для книг, у которых уже были
    # отменённые заказы после отгрузки (до появления этого фикса). Критерий:
    # Order.cancelled=True И в журнале есть запись "отменён ПОСЛЕ отгрузки".
    # Это закрывает дыру для книг, которые уже вернулись и снова продаются.
    if "listings" in tables and "orders" in tables and "sync_log" in tables:
        with engine.begin() as conn:
            conn.execute(text("""
                UPDATE listings
                SET removed_from_sale = TRUE
                WHERE book_id IN (
                    SELECT DISTINCT o.book_id FROM orders o
                    WHERE o.cancelled = TRUE
                      AND o.book_id IS NOT NULL
                      AND EXISTS (
                          SELECT 1 FROM sync_log
                          WHERE book_id = o.book_id
                            AND action = 'order_cancelled'
                            AND message LIKE '%отменён ПОСЛЕ отгрузки%'
                      )
                )
            """))

    # Индексы, добавленные после создания таблиц: create_all() их не досоздаёт.
    # Каталог сортируется по books.updated_at DESC — без индекса на 50k книгах
    # каждая страница вызывает полную сортировку таблицы.
    if "books" in tables:
        index_names = {ix["name"] for ix in inspector.get_indexes("books")}
        if "ix_books_updated_at" not in index_names:
            with engine.begin() as conn:
                conn.execute(text("CREATE INDEX ix_books_updated_at ON books (updated_at)"))
