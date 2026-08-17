"""Ручной запуск очистки корзины WB с полным отчётом (verbose=True).

Пытается удалить в корзину ОДНУ самую старую карточку из очереди и печатает,
что вернуло: сколько удалено/не удалось/заблокировано/отложено и сам ответ.
Запуск короткой строкой:

    docker compose exec app python3 -m app.wb_trash_oneshot

Побочный эффект: реально удаляет одну карточку в корзину WB (как кнопка
«Очистить корзину WB» с периодом 7 дней), и пишет запись в журнал. Для
диагностики это безопасно: карточка и так стоит в очереди на удаление.
"""
from __future__ import annotations

from app.db import SessionLocal
from app.wb_trash import move_withdrawn_to_trash


def main() -> int:
    db = SessionLocal()
    try:
        result = move_withdrawn_to_trash(db, limit=1, verbose=True)
        db.commit()
        print("Результат move_withdrawn_to_trash(limit=1, verbose=True):")
        for k, v in result.items():
            print(f"  {k} = {v}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
