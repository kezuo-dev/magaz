"""Ручной запуск полной сверки каталога со всех включённых площадок.

Одноразовая помощь после деплоя: разблокирует сверку, застрявшую на
предохранителе (книги, пропавшие из выгрузки > суток, снимаются партиями).

Запуск (короткой строкой, чтобы консоль Timeweb не резала команду):
    docker compose --env-file .env.prod exec app python3 -m app.manual_sync
"""
from app.catalog_sync import sync_all
from app.db import SessionLocal


def main() -> int:
    db = SessionLocal()
    try:
        result = sync_all(db)
        db.commit()
        print(result)
    finally:
        db.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())