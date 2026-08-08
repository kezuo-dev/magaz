"""Точка входа. FastAPI + сессии + вход по паролю + подключение роутов."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy import select
from starlette.middleware.sessions import SessionMiddleware

from app.access import SECTION_TITLES, section_for_path
from app.config import BASE_DIR, HTTPS_ONLY, settings
from app.db import Base, engine, ensure_schema, get_db
from app.models import User, UserRole
from app.scheduler import start_scheduler, stop_scheduler
from app.security import check_password
from app.tunnel import start_tunnel, stop_tunnel
from app.templating import templates
from app.routes import catalog, imports, log, settings as settings_routes, analytics, live, auth

# Настройка логирования: планировщик и фоновые задачи пишут в stdout, откуда
# их забирает docker compose logs. Без этого логи планировщика терялись.
logging.basicConfig(
    level=logging.INFO,
    format="%(levelname)s:     %(name)s - %(message)s",
)

# Создаём таблицы при старте (для дев-режима на SQLite; на проде — alembic).
Base.metadata.create_all(bind=engine)
ensure_schema()  # дописываем недостающие колонки в уже существующие таблицы


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Запуск/остановка фоновых служб вместе с приложением.

    Помимо опроса заказов поднимаем публичный HTTPS-туннель — по его адресу
    площадки скачивают фото товаров (локальный localhost им недоступен).
    """
    start_tunnel()
    start_scheduler()
    try:
        yield
    finally:
        stop_scheduler()
        stop_tunnel()


app = FastAPI(title="Букинист", lifespan=lifespan)

static_dir = BASE_DIR / "app" / "static"
static_dir.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Разделы, закрытые отдельным паролем ПОВЕРХ общего входа. Замок разовый: стоит
# уйти из раздела — доступ снова закрывается и пароль нужно вводить заново.
# Действует только на пути владельца (вход по общему паролю). Пользователь с
# ролью «Руководитель» проходит по роли — отдельный пароль ему не нужен.
ADMIN_SECTIONS = {
    "settings": "/settings",
}

# Подписи для страницы ввода второго пароля.
ADMIN_SECTION_TITLES = {"settings": "Настройкам"}


def _admin_section_for(path: str) -> str | None:
    """Какому разделу с отдельным паролем принадлежит путь (или None).

    Отдельно от access.section_for_path: там разделы для ролей, здесь — только
    те, что закрыты вторым паролем. /login не попадает под /log, потому что
    сверяем точный префикс либо префикс + '/'.
    """
    for name, prefix in ADMIN_SECTIONS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return name
    return None


def _load_session_user(request: Request) -> User | None:
    """Пользователь из сессии, или None если вход был по общему паролю.

    Своя сессия БД: middleware не роут, зависимости FastAPI здесь не работают.
    """
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    db = next(get_db())
    try:
        return db.get(User, user_id)
    finally:
        db.close()


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Пускаем внутрь только после входа. Открыты: логин, регистрация, статика.

    Два способа войти:
    - общий пароль (/login) — путь владельца, полный доступ, Настройки под вторым
      паролем (admin_password);
    - телефон + пароль (/user-login) — доступ по роли, см. ROLE_SECTIONS.

    Общий пароль оставлен, чтобы не потерять доступ на уже работающем проде и было
    чем одобрить первую заявку: пользователей в базе изначально нет.
    """
    open_paths = (
        "/login", "/static", "/logout", "/admin-login",
        "/register", "/register-done", "/user-login", "/user-logout",
    )
    path = request.url.path

    if path.startswith(open_paths):
        return await call_next(request)
    if not request.session.get("authed"):
        return RedirectResponse("/login", status_code=303)

    user = _load_session_user(request)
    request.state.user = user

    if user is not None:
        # Вход по телефону — доступ решает роль. Заявку и удалённого пользователя
        # выкидываем сразу: права могли отобрать уже после входа.
        if user.role == UserRole.PENDING:
            request.session.clear()
            return RedirectResponse("/user-login?error=pending", status_code=303)

        section = section_for_path(path)
        if section is not None and not user.can(section):
            return templates.TemplateResponse(
                request, "no_access.html",
                {"section_title": SECTION_TITLES.get(section, section), "user": user},
                status_code=403,
            )
        return await call_next(request)

    if request.session.get("user_id"):
        # user_id в сессии есть, а пользователя в базе нет — учётку удалили.
        request.session.clear()
        return RedirectResponse("/user-login", status_code=303)

    # Путь владельца (общий пароль): Настройки под вторым паролем.
    section = _admin_section_for(path)
    if section is None:
        # Ушли из защищённых разделов — сбрасываем разовую разблокировку.
        request.session.pop("admin_unlocked", None)
        return await call_next(request)

    if request.session.get("admin_unlocked") != section:
        return RedirectResponse(f"/admin-login?next={path}", status_code=303)
    return await call_next(request)


# Добавляем последним, чтобы сессия была доступна во всех middleware выше (в т.ч. require_login).
# https_only=True на проде помечает куку Secure (не уходит по http). Локально по http
# остаётся обычной, иначе вход не работал бы.
app.add_middleware(
    SessionMiddleware,
    secret_key=settings.secret_key,
    https_only=HTTPS_ONLY,
    same_site="lax",
)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, password: str = Form(...)):
    if check_password(password):
        # clear() перед входом: если до этого в сессии сидел пользователь с ролью,
        # его user_id остался бы и общий пароль дал бы права ЕГО роли, а не полные.
        request.session.clear()
        request.session["authed"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Неверный пароль"}, status_code=401
    )


@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


def _safe_next(next_path: str) -> str:
    """Разрешаем переход только на внутренний защищённый раздел (защита от open redirect)."""
    if _admin_section_for(next_path):
        return next_path
    return "/settings"


@app.get("/admin-login", response_class=HTMLResponse)
def admin_login_form(request: Request, next: str = "/settings"):
    target = _safe_next(next)
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {"error": None, "next": target, "title": ADMIN_SECTION_TITLES.get(_admin_section_for(target), "разделу")},
    )


@app.post("/admin-login")
def admin_login(request: Request, password: str = Form(...), next: str = Form("/settings")):
    target = _safe_next(next)
    section = _admin_section_for(target)
    if password == settings.admin_password:
        # Разблокируем ровно тот раздел, куда идём. Другой раздел останется закрытым.
        request.session["admin_unlocked"] = section
        return RedirectResponse(target, status_code=303)
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {"error": "Неверный пароль", "next": target, "title": ADMIN_SECTION_TITLES.get(section, "разделу")},
        status_code=401,
    )


app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(imports.router)
app.include_router(settings_routes.router)
app.include_router(log.router)
app.include_router(analytics.router)
app.include_router(live.router)
