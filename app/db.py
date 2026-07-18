"""Подключение к базе. SQLite локально, PostgreSQL на проде — разница только в DATABASE_URL."""
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import settings

# check_same_thread нужен только для SQLite (фоновый планировщик ходит из другого потока).
connect_args = {}
if settings.database_url.startswith("sqlite"):
    connect_args = {"check_same_thread": False}

engine = create_engine(settings.database_url, connect_args=connect_args, pool_pre_ping=True)
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
    if "books" not in inspector.get_table_names():
        return  # таблицу создаст create_all со всеми колонками сразу
    existing = {col["name"] for col in inspector.get_columns("books")}
    additions = {
        "removed_at": "ALTER TABLE books ADD COLUMN removed_at DATETIME",
        "archived_at": "ALTER TABLE books ADD COLUMN archived_at DATETIME",
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
    with engine.begin() as conn:
        for column, ddl in additions.items():
            if column not in existing:
                conn.execute(text(ddl))
