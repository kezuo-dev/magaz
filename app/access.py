"""Реестр разделов приложения и сопоставление пути с разделом.

Роли и матрица «роль → разделы» живут в models.py (ROLE_SECTIONS). Здесь только
канонические имена разделов и то, какие URL к какому разделу относятся.

Почему у раздела СПИСОК префиксов, а не один: роутеры сложились исторически
неровно. У каталога вообще нет префикса — его страницы висят на «/», «/books/...»,
«/catalog/...» и «/api/books». Свести это к одному префиксу нельзя, а «/» как
префикс поглотил бы вообще все пути приложения.
"""

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
