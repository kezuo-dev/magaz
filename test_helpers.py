"""Вход для проверочных скриптов.

Общего пароля «на склад» больше нет: вход по телефону и паролю, у каждого своя
учётная запись (см. app/routes/auth.py). Пароль владельца в свежей базе
генерируется случайным и печатается в лог, поэтому подсмотреть его из теста
нельзя — вместо этого ставим известный тестовый пароль прямо в базу.

Роль передаётся отдельно: часть проверок как раз про то, что разделы и действия
закрыты для слабых ролей.
"""
from __future__ import annotations

from app.config import settings
from app.db import SessionLocal
from app.models import User, UserRole
from app.security import hash_password, normalize_phone

# Владелец (роль admin) — его телефон известен из настроек.
OWNER_PHONE = normalize_phone(settings.owner_phone)
TEST_PASSWORD = "test-pass-2601"

# Телефоны для остальных ролей: не пересекаются с владельцем, иначе повысили бы
# его же запись и потеряли доступ к настройкам.
ROLE_PHONES = {
    UserRole.ADMIN: OWNER_PHONE,
    UserRole.CHIEF: "79000000002",
    UserRole.MANAGER: "79000000003",
    UserRole.VIEWER: "79000000004",
}


def ensure_user(role: str = UserRole.ADMIN, password: str = TEST_PASSWORD) -> str:
    """Создать/поправить пользователя с этой ролью и известным паролем. Вернуть телефон."""
    phone = ROLE_PHONES[role]
    with SessionLocal() as s:
        user = s.query(User).filter_by(phone=phone).one_or_none()
        if user is None:
            user = User(phone=phone, full_name=f"Тест {role}", source="self")
            s.add(user)
        user.password_hash = hash_password(password)
        user.role = role
        s.commit()
    return phone


def login(client, role: str = UserRole.ADMIN, password: str = TEST_PASSWORD) -> str:
    """Войти клиентом TestClient под нужной ролью. Вернуть телефон."""
    phone = ensure_user(role, password)
    r = client.post(
        "/login", data={"phone": phone, "password": password}, follow_redirects=False
    )
    assert r.status_code == 303 and r.headers["location"] == "/", (
        f"вход под ролью {role} не удался: {r.status_code}"
    )
    return phone
