"""Настройки приложения. Читаются из .env (см. .env.example)."""
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
FEEDS_DIR = BASE_DIR / "feeds"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BASE_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )

    database_url: str = f"sqlite:///{DATA_DIR / 'magaz.db'}"
    secret_key: str = "change-me"
    fernet_key: str = ""
    # Отдельный пароль для полной очистки каталога — разрушительная операция.
    wipe_password: str = "2601"

    # Учётная запись владельца. Создаётся один раз при старте (app/bootstrap.py),
    # это единственная роль ADMIN. Если владелец уже есть в базе, эти значения
    # больше не применяются — пароль меняется в Настройках.
    #
    # Пароля по умолчанию здесь НЕТ намеренно: любое значение в коде попадает в
    # git, а телефон владельца известен, поэтому дефолт равнозначен публичному
    # входу в программу. Если OWNER_PASSWORD не задан, bootstrap придумает
    # случайный и один раз напечатает его в лог — сменить можно в Настройках.
    owner_phone: str = "+79822798700"
    owner_password: str = ""
    owner_name: str = "Владелец"
    # Базовый публичный адрес, по которому площадки скачивают фото. Если включён
    # туннель (см. ниже), он перезапишет это значение выданным https-адресом.
    public_base_url: str = "http://localhost:8000"
    # Автоматический публичный туннель (Cloudflare) для фото на площадках.
    # Ozon/WB скачивают картинки со своей стороны и до localhost не дотянутся,
    # поэтому при старте поднимаем бесплатный https-туннель и подставляем его адрес.
    tunnel_enabled: bool = True
    # Порт, на котором крутится приложение (куда указывает туннель).
    app_port: int = 8000
    # Явный путь к npx (через него запускается localtunnel). Пусто — ищем в PATH.
    npx_path: str = ""
    # Включить/выключить фоновый планировщик (опрос заказов, сверка каталога).
    scheduler_enabled: bool = True
    # Как часто фоново опрашивать заказы площадок (минуты). Опрос заказов дешёвый,
    # поэтому держим частым — это главный механизм авто-снятия проданного.
    poll_interval_minutes: int = 1
    # Как часто следить за остатками наших книг (минуты). Дёшево (спрашиваем остатки
    # только по нашим SKU, ~1 запрос на 1000 книг), поэтому можно часто. Ловит
    # продажи/снятия, не пришедшие через заказы, и почти сразу зеркалит на др. площадку.
    stock_watch_interval_minutes: int = 5
    # Как часто фоново сверять ВЕСЬ каталог с площадками (минуты). Тяжёлая операция
    # (тянет все карточки), поэтому редко. Нужна для обнаружения НОВЫХ книг и как
    # авторитетная подстраховка. Есть и ручная кнопка «Обновить каталог».
    catalog_sync_interval_minutes: int = 60
    # Пароль для раздела аналитики (в разработке).
    analytics_password: str = "05062006"

    # Габариты и вес книги по умолчанию (типичная книга). Ozon требует их для
    # карточки; если у книги не заданы свои — подставляем эти. Вес в граммах,
    # размеры в миллиметрах, вместе с упаковкой.
    default_weight_grams: int = 300
    default_length_mm: int = 220
    default_width_mm: int = 150
    default_height_mm: int = 30

    # Помечать ли куку сессии Secure. Пусто — решаем по public_base_url (https →
    # да). Отдельный ключ нужен, потому что адрес может стать https уже во время
    # работы: туннель перезаписывает public_base_url в lifespan, а кука
    # настраивается один раз при старте. Если включаете туннель и входите по его
    # https-адресу — поставьте SESSION_HTTPS_ONLY=true, иначе кука уйдёт и по http.
    session_https_only: bool | None = None


settings = Settings()


def _resolve_https_only() -> bool:
    """Помечать ли куку сессии Secure.

    Явное значение из настроек важнее автоопределения: адрес в public_base_url
    может не совпадать с тем, по которому реально открывают программу.
    """
    if settings.session_https_only is not None:
        return settings.session_https_only
    return settings.public_base_url.strip().lower().startswith("https://")


# На проде адрес публичный (https) — тогда куку сессии помечаем Secure, чтобы она
# не уходила по незашифрованному соединению. Локально (http) остаётся обычной,
# иначе вход по http://localhost не работал бы.
HTTPS_ONLY = _resolve_https_only()

# Каталоги данных создаём заранее, чтобы SQLite и фиды не падали на старте.
DATA_DIR.mkdir(exist_ok=True)
FEEDS_DIR.mkdir(exist_ok=True)
