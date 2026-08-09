#!/usr/bin/env bash
# Разворачивание «Букиниста» на сервере одной командой.
#
#   ./deploy.sh magaz.example.ru you@mail.ru
#
# Первый запуск: ставит docker (если нет), генерирует .env.prod со случайными
# паролями и ключами, собирает образ и поднимает стек.
# Повторный запуск: подтягивает код из git и пересобирает. Существующий
# .env.prod НЕ трогает — пароли и ключ шифрования остаются прежними.
set -euo pipefail

cd "$(dirname "$0")"

DOMAIN_ARG="${1:-}"
EMAIL_ARG="${2:-}"

# --- Docker ------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
    echo "==> Ставлю Docker"
    curl -fsSL https://get.docker.com | sh
fi

# --- Настройки ---------------------------------------------------------------
# Секреты генерируем сами: придумывать и хранить их руками незачем.
# FERNET_KEY пишем сразу — иначе он создался бы внутри контейнера, и его
# пришлось бы вручную выковыривать из тома, чтобы не потерять ключи площадок.
if [ ! -f .env.prod ]; then
    if [ -z "$DOMAIN_ARG" ] || [ -z "$EMAIL_ARG" ]; then
        echo "Первый запуск. Домен и почту не угадать, передайте их:" >&2
        echo "  ./deploy.sh magaz.example.ru you@mail.ru" >&2
        exit 1
    fi

    echo "==> Генерирую .env.prod (пароли и ключи — случайные)"
    gen() { openssl rand -hex 24; }
    # Fernet-ключ: 32 случайных байта в base64url — формат, который ждёт cryptography.
    fernet() { openssl rand 32 | base64 | tr '+/' '-_'; }

    umask 077
    cat > .env.prod <<EOF
# Сгенерировано deploy.sh $(date +%Y-%m-%d). Пароли для входа — в конце файла.
DOMAIN=${DOMAIN_ARG}
ACME_EMAIL=${EMAIL_ARG}
PUBLIC_BASE_URL=https://${DOMAIN_ARG}

POSTGRES_DB=magaz
POSTGRES_USER=magaz
POSTGRES_PASSWORD=$(gen)

SECRET_KEY=$(gen)
FERNET_KEY=$(fernet)

TUNNEL_ENABLED=false
POLL_INTERVAL_MINUTES=1
SCHEDULER_ENABLED=true

DEFAULT_WEIGHT_GRAMS=300
DEFAULT_LENGTH_MM=220
DEFAULT_WIDTH_MM=150
DEFAULT_HEIGHT_MM=30

# --- Вход (запишите пароли себе) ---
# Владелец — единственная роль «Владелец», её создаёт первый запуск программы.
# Номер задайте свой: по нему вы будете входить. Пароль сгенерирован случайно,
# сменить его можно потом в разделе «Мой профиль».
OWNER_PHONE=${OWNER_PHONE:-+79990000000}
OWNER_PASSWORD=$(openssl rand -hex 6)
WIPE_PASSWORD=$(openssl rand -hex 4)
EOF
else
    echo "==> .env.prod на месте, обновляю код"
    git pull --ff-only || echo "    (git pull пропущен)"
fi

# --- Запуск ------------------------------------------------------------------
# --env-file обязателен: подстановки ${...} в docker-compose.yml берутся из него,
# а не из env_file: — без флага база поднялась бы с пустым паролем.
echo "==> Собираю и поднимаю стек"
docker compose --env-file .env.prod up -d --build

echo
echo "Готово. Адрес: https://${DOMAIN_ARG:-$(grep '^DOMAIN=' .env.prod | cut -d= -f2)}"
echo "Вход владельца (смените пароль в разделе «Мой профиль»):"
grep -E '^(OWNER_PHONE|OWNER_PASSWORD|WIPE_PASSWORD)=' .env.prod | sed 's/^/  /'
echo
echo "Логи:  docker compose --env-file .env.prod logs -f app"
