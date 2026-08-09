"""Реестр разделов приложения, сопоставление пути с разделом и права на действия.

Роли и матрицы «роль → разделы» / «роль → действия» живут в models.py
(ROLE_SECTIONS, ROLE_ACTIONS). Здесь только канонические имена разделов, то,
какие URL к какому разделу относятся, и охранник для отдельных действий.

Почему у раздела СПИСОК префиксов, а не один: роутеры сложились исторически
неровно. У каталога вообще нет префикса — его страницы висят на «/», «/books/...»,
«/catalog/...» и «/api/books». Свести это к одному префиксу нельзя, а «/» как
префикс поглотил бы вообще все пути приложения.
"""
from fastapi import HTTPException, Request

# Разделы в порядке меню: (имя, подпись, префиксы URL).
# Префикс «/» особый — он совпадает только с точным корнем (иначе поглотил бы всё).
SECTIONS: list[tuple[str, str, tuple[str, ...]]] = [
    ("catalog", "Каталог", ("/", "/books", "/catalog", "/api/books", "/api/live/catalog")),
    ("analytics", "Аналитика", ("/analytics", "/api/live/analytics")),
    ("imports", "Импорт", ("/import",)),
    ("log", "Журнал", ("/log", "/api/live/log")),
    ("settings", "Настройки", ("/settings",)),
]

# Быстрый поиск: имя → (подпись, префиксы).
SECTION_MAP = {name: (label, prefixes) for name, label, prefixes in SECTIONS}

# Подписи разделов для сообщений («Нет доступа к разделу ...»).
SECTION_TITLES = {name: label for name, label, _prefixes in SECTIONS}


def _matches(path: str, prefix: str) -> bool:
    """Относится ли путь к префиксу раздела.

    Корень «/» сверяем только на точное совпадение: как префикс он подошёл бы к
    любому URL и закрыл бы всё приложение одним разделом.
    """
    if prefix == "/":
        return path == "/"
    return path == prefix or path.startswith(prefix + "/")


def section_for_path(path: str) -> str | None:
    """Какому разделу принадлежит путь (или None, если путь вне разделов).

    Порядок проверки — порядок SECTIONS, но пересечений между префиксами нет,
    поэтому результат однозначен. «/login» не попадает в «/log», потому что
    сверяется точный префикс либо префикс + «/».
    """
    for name, _label, prefixes in SECTIONS:
        for prefix in prefixes:
            if _matches(path, prefix):
                return name
    return None


def require_action(action: str):
    """Зависимость FastAPI: пустить в роут только если роли разрешено действие.

    Права на РАЗДЕЛ проверяет middleware (см. app/main.py), но раздела мало:
    «Каталог» открыт даже сотруднику, который ничего не меняет, а внутри есть
    кнопки, снимающие книги с площадок и стирающие базу. Матрица «роль →
    действия» живёт в models.ROLE_ACTIONS.

    Отдаём 403 и человеческий текст, а не редирект: это POST-операции, и молча
    вернуть человека на страницу значило бы соврать, что действие выполнено.
    """
    # Импорт внутри функции: models импортирует access (SECTIONS не нужны там,
    # но связь односторонняя), а на уровне модуля это дало бы цикл.
    from app.models import ACTION_TITLES

    def guard(request: Request) -> None:
        user = getattr(request.state, "user", None)
        if user is None or not user.can_do(action):
            raise HTTPException(
                status_code=403,
                detail=(
                    f"Недостаточно прав: «{ACTION_TITLES.get(action, action)}». "
                    "Попросите руководителя или владельца."
                ),
            )

    return guard
