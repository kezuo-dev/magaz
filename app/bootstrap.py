"""Первоначальное заполнение базы: учётная запись владельца.

Вызывается один раз при старте приложения (см. app/main.py). Идемпотентно:
если владелец уже есть, ничего не меняем — пароль, который он себе поставил,
не перетирается при каждом перезапуске.

Владелец — единственная запись с ролью ADMIN. Через интерфейс эту роль выдать
нельзя (см. ASSIGNABLE_ROLES в models.py), поэтому создаём её только здесь.
"""
from __future__ import annotations

import logging
import secrets

from sqlalchemy import select

from app.config import settings
from app.db import SessionLocal
from app.models import User, UserRole
from app.security import hash_password, normalize_phone

logger = logging.getLogger("bootstrap")


def _initial_password() -> tuple[str, bool]:
    """Пароль для новой учётной записи владельца и признак «сгенерирован».

    Пустой OWNER_PASSWORD — обычная ситуация: пароля по умолчанию в коде нет
    намеренно. Тогда придумываем случайный и возвращаем флаг, чтобы вызывающий
    напечатал его в лог: иначе в свежую программу нельзя было бы войти вообще.
    """
    password = settings.owner_password.strip()
    if password:
        return password, False
    return secrets.token_urlsafe(9), True


def ensure_owner() -> None:
    """Создать владельца, если его ещё нет.

    Номер и пароль берём из настроек (owner_phone / owner_password), чтобы их
    можно было переопределить в .env, не трогая код.
    """
    phone = normalize_phone(settings.owner_phone)
    if len(phone) != 11 or not phone.startswith("7"):
        logger.error(
            "OWNER_PHONE задан неверно (%s) — владелец не создан", settings.owner_phone
        )
        return

    db = SessionLocal()
    try:
        # Сначала ищем ИМЕННО номер владельца, а не любого админа: иначе учётка,
        # получившая роль admin в старой версии, заняла бы место владельца и
        # настоящий номер остался бы без доступа в Настройки.
        user = db.scalar(select(User).where(User.phone == phone))
        if user is not None:
            # Запись уже есть (мог зарегистрироваться сам и висеть заявкой).
            # Роль поднимаем до владельца, пароль НЕ перетираем — он мог его сменить.
            if user.role != UserRole.ADMIN:
                user.role = UserRole.ADMIN
                user.source = "owner"
                db.commit()
                logger.info("Запись %s повышена до владельца", phone)
            return

        password, generated = _initial_password()
        db.add(
            User(
                phone=phone,
                full_name=settings.owner_name,
                password_hash=hash_password(password),
                role=UserRole.ADMIN,
                source="owner",
            )
        )
        db.commit()
        logger.info("Создан владелец: %s", phone)
        if generated:
            # Печатаем один раз и только для свежесозданной записи: иначе войти
            # было бы невозможно. Пароль виден в логах (docker compose logs),
            # поэтому сразу просим его сменить.
            logger.warning(
                "OWNER_PASSWORD не задан — сгенерирован временный пароль: %s\n"
                "Войдите с ним и смените пароль в разделе «Мой профиль».",
                password,
            )
    except Exception:  # noqa: BLE001 — сбой сидирования не должен ронять старт
        db.rollback()
        logger.exception("Не удалось создать владельца")
    finally:
        db.close()
