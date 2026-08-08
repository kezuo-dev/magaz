"""Шифрование секретов площадок и проверка пароля входа."""
import hashlib
import json
import secrets
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


# --- Хеширование паролей пользователей ---

def hash_password(password: str) -> str:
    """PBKDF2-SHA256 с солью. Возвращает строку вида «salt$hash» для хранения в БД."""
    salt = secrets.token_hex(16)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${pwd_hash.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Проверить пароль против хеша из БД. stored — результат hash_password()."""
    if not stored or "$" not in stored:
        return False
    salt, expected_hash = stored.split("$", 1)
    pwd_hash = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return pwd_hash.hex() == expected_hash


def normalize_phone(phone: str) -> str:
    """Нормализовать телефон: только цифры, 11 знаков, начинается с 7.

    +7 (999) 123-45-67 → 79991234567
    8 999 123 45 67    → 79991234567
    """
    digits = "".join(c for c in phone if c.isdigit())
    if digits.startswith("8") and len(digits) == 11:
        digits = "7" + digits[1:]
    return digits

