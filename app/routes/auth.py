"""Регистрация, вход, выход и управление пользователями.

Вход в программу один — по номеру телефона и паролю. Общего пароля «на склад»
больше нет: у каждого своя учётная запись, и по ней видно, кто что делал.

Регистрация свободная, но доступа сразу не даёт: новая запись получает роль
«Заявка» (pending) и войти не может, пока владелец не выдаст ей роль.
"""
import time
from collections import defaultdict

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.db import get_db
from app.models import (
    ASSIGNABLE_ROLES,
    ROLE_ABILITIES,
    ROLE_DESCRIPTIONS,
    ROLE_LABELS,
    ROLE_ORDER,
    SyncLog,
    User,
    UserRole,
    utcnow,
)
from app.security import hash_password, normalize_phone, verify_password
from app.templating import templates

router = APIRouter()

# Защита от перебора паролей: счётчики неудачных попыток входа по IP и по телефону.
# Ключ: (ip, phone), значение: (количество попыток, время первой попытки в окне).
# Окно 15 минут. После 5 попыток — блокировка на 15 минут.
_login_attempts: dict[tuple[str, str], tuple[int, float]] = defaultdict(lambda: (0, 0.0))
MAX_LOGIN_ATTEMPTS = 5
LOGIN_WINDOW_SECONDS = 900  # 15 минут


def _check_rate_limit(ip: str, phone: str) -> bool:
    """Проверить, не превышен ли лимит попыток входа. True = разрешено, False = заблокировано."""
    now = time.time()
    key = (ip, phone)
    attempts, first_attempt = _login_attempts[key]

    # Если окно истекло — сбрасываем счётчик
    if now - first_attempt > LOGIN_WINDOW_SECONDS:
        _login_attempts[key] = (1, now)
        return True

    # Если превышен лимит — блокируем
    if attempts >= MAX_LOGIN_ATTEMPTS:
        return False

    # Увеличиваем счётчик
    _login_attempts[key] = (attempts + 1, first_attempt)
    return True


def _reset_rate_limit(ip: str, phone: str):
    """Сбросить счётчик при успешном входе."""
    key = (ip, phone)
    if key in _login_attempts:
        del _login_attempts[key]


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
    """Создать заявку на доступ (роль pending). Владелец потом выдаст права.

    Защита от перечисления: всегда возвращаем успех, даже если телефон занят —
    атакующий не узнает, зарегистрирован номер или нет.
    """
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

    # Проверяем, занят ли телефон, но НЕ сообщаем об этом пользователю
    exists = db.scalar(select(User.id).where(User.phone == phone_norm).limit(1)) is not None
    if not exists:
        # Только если телефон свободен — создаём заявку
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

    # В любом случае показываем успех — даже если телефон уже есть
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
    client_ip = request.client.host if request.client else "unknown"

    # Защита от перебора паролей: проверяем rate limit
    if not _check_rate_limit(client_ip, phone_norm):
        return RedirectResponse("/login?error=locked", status_code=303)

    user = db.scalar(select(User).where(User.phone == phone_norm))
    if not user or not verify_password(password, user.password_hash):
        return RedirectResponse("/login?error=bad", status_code=303)
    if user.role == UserRole.PENDING:
        return RedirectResponse("/login?error=pending", status_code=303)

    # Успешный вход — сбрасываем счётчик попыток
    _reset_rate_limit(client_ip, phone_norm)

    user.last_login_at = utcnow()
    db.commit()
    # clear() перед входом: чтобы от прошлой сессии не осталось чужого user_id.
    request.session.clear()
    request.session["user_id"] = user.id
    return RedirectResponse("/", status_code=303)


@router.post("/logout")
def logout(request: Request):
    """Выйти (сбросить сессию).

    Только POST: на GET-ссылку хватало картинки <img src="/logout"> на чужом
    сайте, чтобы выкинуть человека из программы (SameSite=Lax пропускает куку
    при переходе верхнего уровня).
    """
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, notice: str = "", error: str = ""):
    """Свой профиль: единственное место, где меняется пароль.

    Живёт вне раздела Настройки намеренно: пароль меняет каждый сам, а Настройки
    открыты только владельцу и руководителю. Раньше сменить пароль было нельзя
    вообще — учётная запись навсегда оставалась с тем паролем, что был выдан.
    """
    return templates.TemplateResponse(
        request,
        "account.html",
        {"notice": notice or None, "error": error or None},
    )


@router.post("/account/password")
def change_own_password(
    request: Request,
    db: Session = Depends(get_db),
    current_password: str = Form(...),
    new_password: str = Form(...),
    repeat_password: str = Form(...),
):
    """Сменить себе пароль. Текущий пароль спрашиваем всегда.

    Проверка текущего пароля защищает от смены с чужого незакрытого компьютера:
    угнанной сессии для захвата учётной записи оказалось бы достаточно.
    """
    user = request.state.user
    # Берём свою запись в этой сессии БД: request.state.user загружен в middleware
    # на своей сессии, которая уже закрыта, — коммит по нему бы не прошёл.
    me = db.get(User, user.id)
    if me is None:
        return RedirectResponse("/login", status_code=303)

    if not verify_password(current_password, me.password_hash):
        return RedirectResponse(
            "/account?error=Текущий+пароль+неверен", status_code=303
        )
    if len(new_password) < 6:
        return RedirectResponse(
            "/account?error=Новый+пароль+короче+6+символов", status_code=303
        )
    if new_password != repeat_password:
        return RedirectResponse("/account?error=Пароли+не+совпадают", status_code=303)
    if new_password == current_password:
        return RedirectResponse(
            "/account?error=Новый+пароль+совпадает+с+текущим", status_code=303
        )

    me.password_hash = hash_password(new_password)
    db.add(
        SyncLog(
            marketplace=None,
            action="password_change",
            ok=True,
            message=f"{me.phone_pretty} сменил себе пароль",
        )
    )
    db.commit()
    return RedirectResponse("/account?notice=Пароль+изменён", status_code=303)


@router.get("/settings/users", response_class=HTMLResponse)
def users_list(request: Request, db: Session = Depends(get_db), notice: str = "", error: str = ""):
    """Список пользователей, сгруппированный по ролям от высшей к низшей.

    Группируем на стороне сервера, а не в шаблоне: порядок ролей задан один раз
    в ROLE_ORDER (models.py), и шаблон просто рисует то, что пришло.

    Показываем пользователей с неизвестной ролью отдельной группой внизу — иначе
    они пропали бы из интерфейса, если в базе окажется роль вне ROLE_ORDER.
    """
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()

    # Внутри группы порядок наследуется от запроса — новые сверху.
    groups = []
    seen_ids = set()
    for role in ROLE_ORDER:
        members = [u for u in users if u.role == role]
        seen_ids.update(u.id for u in members)
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

    # Пользователи с ролью вне ROLE_ORDER (битая база или баг миграции).
    orphans = [u for u in users if u.id not in seen_ids]
    if orphans:
        groups.append(
            {
                "role": "unknown",
                "label": "Неизвестная роль",
                "description": "Роль не найдена в списке ролей — возможно, битая база",
                "users": orphans,
            }
        )

    # Памятка «кто что может» — все роли, даже если таких людей ещё нет: владелец
    # смотрит её как раз перед тем, как выдать роль.
    role_guide = [
        {
            "role": role,
            "label": ROLE_LABELS.get(role, role),
            "description": ROLE_DESCRIPTIONS.get(role, ""),
            "abilities": ROLE_ABILITIES.get(role, []),
        }
        for role in ROLE_ORDER
    ]

    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "groups": groups,
            "role_guide": role_guide,
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

    Chief не может менять роль другому chief: это право только владельца.
    Запрещено менять роль себе: разжалование себя закрывает доступ в Настройки.
    """
    me = request.state.user
    user = db.get(User, user_id)
    if user is None:
        return RedirectResponse("/settings/users?error=Пользователь+не+найден", status_code=303)
    if user.id == me.id:
        return RedirectResponse(
            "/settings/users?error=Менять+себе+роль+нельзя", status_code=303
        )
    if user.role == UserRole.ADMIN:
        return RedirectResponse(
            "/settings/users?error=Роль+владельца+менять+нельзя", status_code=303
        )
    # Chief может менять только не-chief (владелец же может менять кого угодно).
    if me.role == UserRole.CHIEF and user.role == UserRole.CHIEF:
        return RedirectResponse(
            "/settings/users?error=Руководитель+не+может+менять+роль+другому+руководителю",
            status_code=303,
        )
    if role not in ASSIGNABLE_ROLES:
        return RedirectResponse("/settings/users?error=Недопустимая+роль", status_code=303)

    old_role = user.role
    user.role = role
    db.add(
        SyncLog(
            marketplace=None,
            action="role_change",
            ok=True,
            message=f"{me.phone_pretty} изменил роль {user.phone_pretty}: {old_role} → {role}",
        )
    )
    db.commit()
    return RedirectResponse("/settings/users?notice=Роль+обновлена", status_code=303)


@router.post("/settings/users/{user_id}/delete")
def delete_user(request: Request, user_id: int, db: Session = Depends(get_db)):
    """Удалить пользователя. Владельца удалить нельзя, себя — тоже.

    Chief не может удалить другого chief: это право только владельца.
    Запись об удалении пишется в журнал.
    """
    me = request.state.user
    user = db.get(User, user_id)
    if user is None:
        return RedirectResponse("/settings/users?error=Пользователь+не+найден", status_code=303)
    if user.id == me.id:
        return RedirectResponse(
            "/settings/users?error=Удалить+себя+нельзя", status_code=303
        )
    if user.role == UserRole.ADMIN:
        return RedirectResponse(
            "/settings/users?error=Владельца+удалить+нельзя", status_code=303
        )
    # Chief может удалять только не-chief (владелец же может удалять кого угодно).
    if me.role == UserRole.CHIEF and user.role == UserRole.CHIEF:
        return RedirectResponse(
            "/settings/users?error=Руководитель+не+может+удалить+другого+руководителя",
            status_code=303,
        )

    db.add(
        SyncLog(
            marketplace=None,
            action="user_delete",
            ok=True,
            message=f"{me.phone_pretty} удалил {user.phone_pretty} ({user.role})",
        )
    )
    db.delete(user)
    db.commit()
    return RedirectResponse("/settings/users?notice=Пользователь+удалён", status_code=303)
