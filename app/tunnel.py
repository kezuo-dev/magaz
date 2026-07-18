"""Автоматический публичный HTTPS-туннель для фото на площадках.

Зачем: Ozon и WB скачивают фото товара по ссылке со СВОИХ серверов. Локальный
адрес http://localhost:8000 им недоступен (Ozon отвечает «invalid URL»). Поэтому
при старте поднимаем публичный HTTPS-туннель и подставляем его адрес в
settings.public_base_url — тогда ссылки на фото становятся доступны извне.

Используем localtunnel (npx localtunnel). Он работает поверх обычного HTTPS:443,
что важно на этой машине: сетевой стек (VPN/прокси) режет DNS-запросы Cloudflare,
из-за чего cloudflared не может достучаться до своей краевой сети. localtunnel же
ходит по 443 как обычный сайт и проходит. Аккаунт и ключи не нужны, адрес вида
https://<случайно>.loca.lt меняется при каждом запуске — для отдачи фото на время
создания карточки этого достаточно.
"""
import os
import re
import shutil
import subprocess
import threading

from app.config import settings

# Ссылка вида https://xxxx.loca.lt в выводе localtunnel.
_URL_RE = re.compile(r"https://[a-z0-9-]+\.loca\.lt")

_proc: subprocess.Popen | None = None
_public_url: str | None = None


def _find_npx() -> str | None:
    """Найти npx (через который запускаем localtunnel без глобальной установки)."""
    if settings.npx_path and os.path.isfile(settings.npx_path):
        return settings.npx_path
    for name in ("npx.cmd", "npx"):
        found = shutil.which(name)
        if found:
            return found
    return None


def _reader(proc: subprocess.Popen, ready: threading.Event) -> None:
    """Читает вывод localtunnel, вылавливает публичный адрес и пишет его в настройки."""
    global _public_url
    assert proc.stdout is not None
    for line in proc.stdout:
        if _public_url is None:
            match = _URL_RE.search(line)
            if match:
                _public_url = match.group(0)
                settings.public_base_url = _public_url
                print(f"[tunnel] публичный адрес для фото: {_public_url}")
                ready.set()


def start_tunnel() -> str | None:
    """Поднять localtunnel и вернуть публичный https-адрес.

    Ничего не делает, если туннель отключён или npx не найден (тогда остаётся адрес
    по умолчанию — фото на площадки не уйдут, но приложение работает). Возвращает
    адрес или None.
    """
    global _proc
    if not settings.tunnel_enabled:
        return None
    if _proc is not None and _proc.poll() is None:
        return _public_url  # уже запущен

    npx = _find_npx()
    if not npx:
        print(
            "[tunnel] npx (Node.js) не найден — публичный адрес для фото не поднят. "
            "Установите Node.js или задайте PUBLIC_BASE_URL вручную."
        )
        return None

    cmd = [npx, "--yes", "localtunnel", "--port", str(settings.app_port)]
    creationflags = 0
    if os.name == "nt":
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)

    try:
        _proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            creationflags=creationflags,
        )
    except Exception as exc:  # noqa: BLE001 — не роняем приложение из-за туннеля
        print(f"[tunnel] не удалось запустить localtunnel: {exc}")
        _proc = None
        return None

    ready = threading.Event()
    threading.Thread(target=_reader, args=(_proc, ready), daemon=True).start()

    # Ждём адрес: npx может докачивать пакет при первом запуске, поэтому запас времени.
    if ready.wait(timeout=60):
        return _public_url
    print("[tunnel] адрес пока не получен — фото могут не уйти на площадки в первые секунды.")
    return _public_url


def stop_tunnel() -> None:
    """Погасить туннель при остановке приложения."""
    global _proc
    if _proc is not None and _proc.poll() is None:
        _proc.terminate()
        try:
            _proc.wait(timeout=5)
        except Exception:
            _proc.kill()
    _proc = None


def public_url() -> str | None:
    """Текущий публичный адрес туннеля (или None, если не поднят)."""
    return _public_url
