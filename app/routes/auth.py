"""Регистрация, вход, выход и управление пользователями.

Вход в программу один — по номеру телефона и паролю. Общего пароля «на склад»
больше нет: у каждого своя учётная запись, и по ней видно, кто что делал.

Регистрация свободная, но доступа сразу не даёт: новая запись получает роль
«Заявка» (pending) и войти не может, пока владелец не выдаст ей роль.
"""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    ASSIGNABLE_ROLES,
    ROLE_DESCRIPTIONS,
    ROLE_LABELS,
    ROLE_ORDER,
    User,
    UserRole,
    utcnow,
)
from app.security import hash_password, normalize_phone, verify_password
from app.templating import templates

router = APIRouter()


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    """Форма регистрации: телефон, ФИО, пароль — заявка с ролью pending."""
    return templates.TemplateResponse(request, "register.html", {"error": None})


@router.post("/register")
def register(
    request: Request,
    db: Session = Depends(get_db),
    phone: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
):
    """Создать заявку на доступ (роль pending). Владелец потом выдаст права."""
    phone_norm = normalize_phone(phone)
    if len(phone_norm) != 11 or not phone_norm.startswith("7"):
        return templates.TemplateResponse(
            request, "register.html",
            {"error": "Неверный формат телефона. Укажите российский номер."},
            status_code=400,
        )
    if len(password) < 6:
        return templates.TemplateResponse(
            request, "register.html",
            {"error": "Пароль должен быть не короче 6 символов."},
            status_code=400,
        )
    exists = db.scalar(select(User.id).where(User.phone == phone_norm).limit(1)) is not None
    if exists:
        return templates.TemplateResponse(
            request, "register.html",
            {"error": "Этот номер уже зарегистрирован."},
            status_code=400,
        )

    db.add(
        User(
            phone=phone_norm,
            full_name=full_name.strip(),
            password_hash=hash_password(password),
            role=UserRole.PENDING,
            source="self",
        )
    )
    db.commit()
    return RedirectResponse("/register-done", status_code=303)


@router.get("/register-done", response_class=HTMLResponse)
def register_done(request: Request):
    """Успешная регистрация — ждите одобрения."""
    return templates.TemplateResponse(request, "register_done.html", {})


@router.get("/login", response_class=HTMLResponse)
def login_form(request: Request, error: str = ""):
    """Единственная страница входа: телефон + пароль."""
    return templates.TemplateResponse(request, "login.html", {"error": error or None})


@router.post("/login")
def login(
    request: Request,
    db: Session = Depends(get_db),
    phone: str = Form(...),
    password: str = Form(...),
):
    """Проверить телефон+пароль и положить user_id в сессию."""
    phone_norm = normalize_phone(phone)
    user = db.scalar(select(User).where(User.phone == phone_norm))
    if not user or not verify_password(password, user.password_hash):
        return RedirectResponse("/login?error=bad", status_code=303)
    if user.role == UserRole.PENDING:
        return RedirectResponse("/login?error=pending", status_code=303)

    user.last_login_at = utcnow()
    db.commit()
    # clear() перед входом: чтобы от прошлой сессии не осталось чужого user_id.
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.get("/logout")
def logout(request: Request):
    """Выйти (сбросить сессию)."""
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/settings/users", response_class=HTMLResponse)
def users_list(request: Request, db: Session = Depends(get_db), notice: str = "", error: str = ""):
    """Список пользователей, сгруппированный по ролям от высшей к низшей.

    Группируем на стороне сервера, а не в шаблоне: порядок ролей задан один раз
    в ROLE_ORDER (models.py), и шаблон просто рисует то, что пришло.
    """
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()

    # Внутри группы порядок наследуется от запроса — новые сверху.
    groups = []
    for role in ROLE_ORDER:
        members = [u for u in users if u.role == role]
        if not members:
            continue
        groups.append(
            {
                "role": role,
                "label": ROLE_LABELS.get(role, role),
                "description": ROLE_DESCRIPTIONS.get(role, ""),
                "users": members,
            }
        )

    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "groups": groups,
            "total": len(users),
            "role_labels": ROLE_LABELS,
            "role_descriptions": ROLE_DESCRIPTIONS,
            "assignable_roles": ASSIGNABLE_ROLES,
            "notice": notice or None,
            "error": error or None,
        },
    )


@router.post("/settings/users/{user_id}/role")
def change_role(
    request: Request,
    user_id: int,
    db: Session = Depends(get_db),
    role: str = Form(...),
):
    """Изменить роль: одобрить заявку, повысить или понизить.

    Роль владельца (admin) через интерфейс не выдаётся и не снимается — иначе
    можно было бы завести второго владельца или случайно разжаловать себя и
    закрыть себе доступ в Настройки.
    """
    user = db.get(User, user_id)
    if user is None:
        return RedirectResponse("/settings/users?error=Пользователь+не+найден", status_code=303)
    if user.role == UserRole.ADMIN:
        return RedirectResponse(
            "/settings/users?error=Роль+владельца+менять+нельзя", status_code=303
        )
    if role not in ASSIGNABLE_ROLES:
        return RedirectResponse("/settings/users?error=Недопустимая+роль", status_code=303)

    user.role = role
    db.commit()
    return RedirectResponse("/settings/users?notice=Роль+обновлена", status_code=303)


@router.post("/settings/users/{user_id}/delete")
def delete_user(user_id: int, db: Session = Depends(get_db)):
    """Удалить пользователя. Владельца удалить нельзя."""
    user = db.get(User, user_id)
    if user is None:
        return RedirectResponse("/settings/users?error=Пользователь+не+найден", status_code=303)
    if user.role == UserRole.ADMIN:
        return RedirectResponse(
            "/settings/users?error=Владельца+удалить+нельзя", status_code=303
        )
    db.delete(user)
    db.commit()
    return RedirectResponse("/settings/users?notice=Пользователь+удалён", status_code=303)
