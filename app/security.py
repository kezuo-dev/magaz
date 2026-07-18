"""Шифрование секретов площадок и проверка пароля входа."""
import json
from pathlib import Path

from cryptography.fernet import Fernet

from app.config import BASE_DIR, settings


def _load_or_create_key() -> bytes:
    """Ключ шифрования из .env; если пусто — генерируем и сохраняем рядом, чтобы токены не протухли."""
    if settings.fernet_key:
        return settings.fernet_key.encode()

    key_file = BASE_DIR / "data" / "fernet.key"
    if key_file.exists():
        return key_file.read_bytes()

    key = Fernet.generate_key()
    key_file.write_bytes(key)
    return key


_fernet = Fernet(_load_or_create_key())


def encrypt_credentials(data: dict) -> str:
    return _fernet.encrypt(json.dumps(data).encode()).decode()


def decrypt_credentials(token: str) -> dict:
    return json.loads(_fernet.decrypt(token.encode()).decode())


def check_password(password: str) -> bool:
    return password == settings.app_password
