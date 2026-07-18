"""Точка входа. FastAPI + сессии + вход по паролю + подключение роутов."""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.config import BASE_DIR, HTTPS_ONLY, settings
from app.db import Base, engine, ensure_schema
from app.scheduler import start_scheduler, stop_scheduler
from app.security import check_password
from app.tunnel import start_tunnel, stop_tunnel
from app.templating import templates
from app.routes import catalog, imports, log, settings as settings_routes

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


# Разделы, закрытые отдельным паролем. Каждый раздел разблокируется САМ ПО СЕБЕ
# (пароль к одному не открывает другой) и только на время визита в него: стоит
# уйти из раздела — доступ снова закрывается и пароль нужно вводить заново.
ADMIN_SECTIONS = {
    "settings": "/settings",
    "log": "/log",
}


def _section_for(path: str) -> str | None:
    """Какому защищённому разделу принадлежит путь (или None). /login не попадает
    под /log, потому что сверяем точный раздел либо раздел + '/'."""
    for name, prefix in ADMIN_SECTIONS.items():
        if path == prefix or path.startswith(prefix + "/"):
            return name
    return None


@app.middleware("http")
async def require_login(request: Request, call_next):
    """Пускаем внутрь только после входа. Открыты: логин и статика.

    Журнал и Настройки закрыты отдельным паролем (admin_password) — у каждого свой
    вход. Разблокировка живёт только пока пользователь внутри раздела: как только
    запрос уходит на другую страницу, замок снова защёлкивается (разовый доступ,
    не на всю сессию).
    """
    open_paths = ("/login", "/static", "/logout", "/admin-login")
    path = request.url.path

    if path.startswith(open_paths):
        return await call_next(request)
    if not request.session.get("authed"):
        return RedirectResponse("/login", status_code=303)

    section = _section_for(path)
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
    if _section_for(next_path):
        return next_path
    return "/settings"


SECTION_TITLES = {"settings": "Настройки", "log": "Журнал"}


@app.get("/admin-login", response_class=HTMLResponse)
def admin_login_form(request: Request, next: str = "/settings"):
    target = _safe_next(next)
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {"error": None, "next": target, "title": SECTION_TITLES.get(_section_for(target), "разделу")},
    )


@app.post("/admin-login")
def admin_login(request: Request, password: str = Form(...), next: str = Form("/settings")):
    target = _safe_next(next)
    section = _section_for(target)
    if password == settings.admin_password:
        # Разблокируем ровно тот раздел, куда идём. Другой раздел останется закрытым.
        request.session["admin_unlocked"] = section
        return RedirectResponse(target, status_code=303)
    return templates.TemplateResponse(
        request,
        "admin_login.html",
        {"error": "Неверный пароль", "next": target, "title": SECTION_TITLES.get(section, "разделу")},
        status_code=401,
    )


app.include_router(catalog.router)
app.include_router(imports.router)
app.include_router(settings_routes.router)
app.include_router(log.router)
