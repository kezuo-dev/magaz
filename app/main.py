"""Точка входа. FastAPI + сессии + вход по телефону + подключение роутов."""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.middleware.sessions import SessionMiddleware

from app.access import SECTION_TITLES, section_for_path
from app.bootstrap import ensure_owner
from app.config import BASE_DIR, HTTPS_ONLY, settings
from app.db import Base, engine, ensure_schema, get_db
from app.models import User, UserRole
from app.scheduler import start_scheduler, stop_scheduler
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
ensure_owner()   # заводим учётную запись владельца, если её ещё нет


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


def _load_session_user(request: Request) -> User | None:
    """Пользователь из сессии (или None, если сессия пустая/учётку удалили).

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
    """Пускаем внутрь только после входа. Открыты: вход, регистрация, статика.

    Вход один — по телефону и паролю (см. routes/auth.py). Что человеку доступно,
    решает его роль (ROLE_SECTIONS): раздел Настройки открыт владельцу и
    руководителю, остальным — нет.
    """
    open_paths = ("/login", "/logout", "/register", "/register-done", "/static")
    path = request.url.path

    if path.startswith(open_paths):
        return await call_next(request)

    user = _load_session_user(request)
    if user is None:
        # Не вошли, либо учётку удалили уже после входа.
        request.session.clear()
        return RedirectResponse("/login", status_code=303)

    # Права могли отобрать уже после входа — проверяем на каждом запросе.
    if user.role == UserRole.PENDING:
        request.session.clear()
        return RedirectResponse("/login?error=pending", status_code=303)

    request.state.user = user

    section = section_for_path(path)
    if section is not None and not user.can(section):
        return templates.TemplateResponse(
            request, "no_access.html",
            {"section_title": SECTION_TITLES.get(section, section), "user": user},
            status_code=403,
        )
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


app.include_router(auth.router)
app.include_router(catalog.router)
app.include_router(imports.router)
app.include_router(settings_routes.router)
app.include_router(log.router)
app.include_router(analytics.router)
app.include_router(live.router)
