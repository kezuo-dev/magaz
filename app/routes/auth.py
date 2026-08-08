"""Регистрация, вход, выход и управление пользователями."""
from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from app import google_forms
from app.db import get_db
from app.flags import get_form_csv_url, set_form_csv_url
from app.models import User, UserRole, ROLE_LABELS, utcnow
from app.security import hash_password, verify_password, normalize_phone
from app.templating import templates

router = APIRouter()


@router.get("/register", response_class=HTMLResponse)
def register_form(request: Request):
    """Форма регистрации. Человек вводит телефон, ФИО, пароль — заявка с ролью pending."""
    return templates.TemplateResponse(request, "register.html", {"error": None})


@router.post("/register")
def register(
    request: Request,
    db: Session = Depends(get_db),
    phone: str = Form(...),
    full_name: str = Form(...),
    password: str = Form(...),
    comment: str = Form(""),
):
    """Создать заявку на доступ (роль pending). Админ потом одобрит и выдаст права."""
    phone_norm = normalize_phone(phone)
    if len(phone_norm) != 11 or not phone_norm.startswith("7"):
        return templates.TemplateResponse(
            request, "register.html", {"error": "Неверный формат телефона. Укажите российский номер."}, status_code=400
        )
    if len(password) < 6:
        return templates.TemplateResponse(
            request, "register.html", {"error": "Пароль должен быть не короче 6 символов."}, status_code=400
        )
    exists = db.scalar(select(User.id).where(User.phone == phone_norm).limit(1)) is not None
    if exists:
        return templates.TemplateResponse(
            request, "register.html", {"error": "Этот номер уже зарегистрирован."}, status_code=400
        )

    user = User(
        phone=phone_norm,
        full_name=full_name.strip(),
        password_hash=hash_password(password),
        role=UserRole.PENDING,
        source="form",
        comment=comment.strip() or None,
    )
    db.add(user)
    db.commit()
    return RedirectResponse("/register-done", status_code=303)


@router.get("/register-done", response_class=HTMLResponse)
def register_done(request: Request):
    """Успешная регистрация — ждите одобрения."""
    return templates.TemplateResponse(request, "register_done.html", {})


@router.get("/user-login", response_class=HTMLResponse)
def user_login_form(request: Request, error: str = ""):
    """Вход по телефону + пароль."""
    return templates.TemplateResponse(request, "user_login.html", {"error": error or None})


@router.post("/user-login")
def user_login(
    request: Request,
    db: Session = Depends(get_db),
    phone: str = Form(...),
    password: str = Form(...),
):
    """Проверить телефон+пароль, поставить user_id в сессию."""
    phone_norm = normalize_phone(phone)
    user = db.scalar(select(User).where(User.phone == phone_norm))
    if not user or not verify_password(password, user.password_hash):
        return RedirectResponse("/user-login?error=bad", status_code=303)
    if user.role == UserRole.PENDING:
        return RedirectResponse("/user-login?error=pending", status_code=303)

    user.last_login_at = utcnow()
    db.commit()
    request.session["user_id"] = user.id
    request.session["authed"] = True  # совместимость со старым middleware
    return RedirectResponse("/", status_code=303)


@router.get("/user-logout")
def user_logout(request: Request):
    """Выйти (сбросить сессию)."""
    request.session.clear()
    return RedirectResponse("/user-login", status_code=303)


@router.get("/settings/users", response_class=HTMLResponse)
def users_list(request: Request, db: Session = Depends(get_db), notice: str = "", error: str = ""):
    """Список пользователей (только для админов). Показывает все заявки и активных."""
    # Проверка роли будет в middleware — здесь предполагаем, что админ уже пропущен.
    users = db.scalars(select(User).order_by(User.created_at.desc())).all()
    return templates.TemplateResponse(
        request,
        "users.html",
        {
            "users": users,
            "role_labels": ROLE_LABELS,
            "form_csv_url": get_form_csv_url(db),
            "notice": notice or None,
            "error": error or None,
        },
    )


@router.post("/settings/users/{user_id}/role")
def change_role(
    user_id: int,
    db: Session = Depends(get_db),
    role: str = Form(...),
):
    """Изменить роль пользователя (одобрить заявку / повысить / понизить)."""
    user = db.get(User, user_id)
    if user and role in [r.value for r in UserRole]:
        user.role = role
        db.commit()
    return RedirectResponse("/settings/users", status_code=303)


@router.post("/settings/users/form-url")
def save_form_url(db: Session = Depends(get_db), csv_url: str = Form("")):
    """Сохранить ссылку на опубликованный CSV с ответами Google-формы."""
    set_form_csv_url(db, csv_url)
    db.commit()
    return RedirectResponse("/settings/users?notice=Ссылка+сохранена", status_code=303)


@router.post("/settings/users/import")
def import_from_form(db: Session = Depends(get_db)):
    """Скачать ответы формы и завести новые заявки (роль pending)."""
    csv_url = get_form_csv_url(db)
    if not csv_url:
        return RedirectResponse("/settings/users?error=Сначала+сохраните+ссылку+на+форму", status_code=303)
    try:
        result = google_forms.import_applications(db, csv_url)
    except Exception as exc:  # noqa: BLE001 — показываем причину пользователю
        return RedirectResponse(f"/settings/users?error=Не+удалось+загрузить:+{exc}", status_code=303)

    parts = [f"Добавлено заявок: {result['added']}", f"пропущено: {result['skipped']}"]
    if result["errors"]:
        parts.append(f"с ошибками: {len(result['errors'])}")
    return RedirectResponse(f"/settings/users?notice={'; '.join(parts)}", status_code=303)
