"""Разовый перенос каталога из локальной SQLite в PostgreSQL на сервере.

Копирует книги, лоты, заказы и журнал через модели (надёжнее ручного SQL:
сохраняет id, статусы и external_id, чтобы авто-снятие и связь с площадками
продолжали работать). Ключи площадок (таблица marketplace_accounts) НЕ переносим:
они зашифрованы ключом FERNET_KEY со старой машины — проще ввести заново в
Настройках на сервере.

Как пользоваться (на сервере, где уже поднят PostgreSQL):
  1. Скопируйте сюда старую базу, напр. как ./data/magaz.db
  2. Задайте адрес PostgreSQL и запустите:
       SOURCE_SQLITE_URL=sqlite:///./data/magaz.db \
       DATABASE_URL=postgresql+psycopg2://magaz:ПАРОЛЬ@localhost:5432/magaz \
       python migrate_to_postgres.py
  Внутри docker это удобнее выполнять командой из инструкции (см. ответ).

Скрипт идемпотентен по первичному ключу: повторный запуск не создаст дублей,
но и не затрёт уже перенесённые записи (пропускает существующие id).
"""
import os
import sys

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Модели берём из приложения — целевые таблицы создаст metadata.create_all.
from app.db import Base
from app.models import Book, Listing, Order, SyncLog

SOURCE_URL = os.environ.get("SOURCE_SQLITE_URL", "sqlite:///./data/magaz.db")
TARGET_URL = os.environ.get("DATABASE_URL", "")

# Порядок важен: сначала книги (на них ссылаются лоты/заказы/журнал).
MODELS = [Book, Listing, Order, SyncLog]


def _columns(model):
    """Имена колонок модели — по ним копируем значения один в один."""
    return [c.name for c in model.__table__.columns]


def main() -> int:
    if not TARGET_URL:
        print("ОШИБКА: задайте DATABASE_URL (адрес PostgreSQL).", file=sys.stderr)
        return 1
    if TARGET_URL.startswith("sqlite"):
        print("ОШИБКА: DATABASE_URL указывает на SQLite, а нужен PostgreSQL.", file=sys.stderr)
        return 1

    src_engine = create_engine(SOURCE_URL, connect_args={"check_same_thread": False})
    dst_engine = create_engine(TARGET_URL, pool_pre_ping=True)

    # Таблицы в PostgreSQL на случай, если приложение ещё не стартовало.
    Base.metadata.create_all(bind=dst_engine)

    SrcSession = sessionmaker(bind=src_engine)
    DstSession = sessionmaker(bind=dst_engine)

    total = 0
    with SrcSession() as src, DstSession() as dst:
        for model in MODELS:
            cols = _columns(model)
            existing_ids = {row[0] for row in dst.query(model.id).all()}
            copied = 0
            for obj in src.query(model).all():
                if obj.id in existing_ids:
                    continue  # уже перенесён — не дублируем
                data = {c: getattr(obj, c) for c in cols}
                dst.add(model(**data))
                copied += 1
            dst.commit()

            # Синхронизируем счётчик автоинкремента, чтобы новые записи не
            # налетали на перенесённые id (в PostgreSQL это отдельная sequence).
            _fix_sequence(dst, model)
            total += copied
            print(f"{model.__tablename__}: перенесено {copied}")

    print(f"Готово. Всего перенесено записей: {total}")
    print("Ключи площадок не переносились — введите их заново в разделе Настройки.")
    return 0


def _fix_sequence(session, model) -> None:
    """Выставить sequence PostgreSQL на max(id), иначе вставка новых упадёт на дубль."""
    from sqlalchemy import text

    table = model.__tablename__
    session.execute(
        text(
            "SELECT setval("
            "  pg_get_serial_sequence(:t, 'id'),"
            "  COALESCE((SELECT MAX(id) FROM " + table + "), 1),"
            "  (SELECT MAX(id) IS NOT NULL FROM " + table + ")"
            ")"
        ),
        {"t": table},
    )
    session.commit()


if __name__ == "__main__":
    raise SystemExit(main())
