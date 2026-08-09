#!/usr/bin/env python3
"""Аудит: может ли chief повысить себя до admin или удалить владельца."""
import os
import sys
sys.path.insert(0, "/Users/kezuo/projects/magaz")

os.environ["DATABASE_URL"] = "sqlite:///./data/_audit_auth.db"
os.environ["SCHEDULER_ENABLED"] = "false"
os.environ["TUNNEL_ENABLED"] = "false"

from starlette.testclient import TestClient
from app.main import app
from app.db import Base, engine, SessionLocal
from app.models import User, UserRole
from app.security import hash_password
from test_helpers import login

# Свежая база
Base.metadata.drop_all(bind=engine)
Base.metadata.create_all(bind=engine)

# Создаём владельца и руководителя
db = SessionLocal()
owner = User(phone="79000000001", full_name="Owner", password_hash=hash_password("test-pass-2601"),
             role=UserRole.ADMIN, source="owner")
chief = User(phone="79000000002", full_name="Chief", password_hash=hash_password("test-pass-2601"),
             role=UserRole.CHIEF, source="self")
db.add_all([owner, chief])
db.commit()
owner_id = owner.id
chief_id = chief.id
db.close()

client = TestClient(app)

print("=" * 80)
print("ПРОВЕРКА: МОЖЕТ ЛИ CHIEF ПОВЫСИТЬ СЕБЯ ДО ADMIN")
print("=" * 80)

# Входим как chief
login(client, UserRole.CHIEF)

# Пробуем сменить себе роль на admin
print("\n1. Chief пробует сменить себе роль на admin...")
resp = client.post(f"/settings/users/{chief_id}/role", data={"role": "admin"}, follow_redirects=False)
print(f"   Статус: {resp.status_code}")
print(f"   Location: {resp.headers.get('location', 'нет редиректа')}")
if "error" in resp.headers.get("location", ""):
    print(f"   ✓ Отклонено: {resp.headers['location']}")
else:
    print(f"   ✗ ОПАСНОСТЬ: запрос прошёл!")

# Проверяем роль в базе
db = SessionLocal()
chief_user = db.get(User, chief_id)
print(f"   Роль в базе: {chief_user.role}")
if chief_user.role == UserRole.ADMIN:
    print("   ✗✗✗ БАГ: chief стал admin!")
else:
    print("   ✓ Роль не изменилась")
db.close()

print("\n" + "=" * 80)
print("ПРОВЕРКА: МОЖЕТ ЛИ CHIEF УДАЛИТЬ ВЛАДЕЛЬЦА")
print("=" * 80)

print("\n2. Chief пробует удалить владельца...")
resp = client.post(f"/settings/users/{owner_id}/delete", follow_redirects=False)
print(f"   Статус: {resp.status_code}")
print(f"   Location: {resp.headers.get('location', 'нет редиректа')}")
if "error" in resp.headers.get("location", ""):
    print(f"   ✓ Отклонено: {resp.headers['location']}")
else:
    print(f"   ✗ ОПАСНОСТЬ: запрос прошёл!")

# Проверяем наличие владельца в базе
db = SessionLocal()
owner_exists = db.get(User, owner_id) is not None
print(f"   Владелец в базе: {'есть' if owner_exists else 'УДАЛЁН'}")
if not owner_exists:
    print("   ✗✗✗ БАГ: владелец удалён!")
else:
    print("   ✓ Владелец цел")
db.close()

print("\n" + "=" * 80)
print("ПРОВЕРКА: МОЖЕТ ЛИ CHIEF ПОВЫСИТЬ ДРУГОГО ДО ADMIN")
print("=" * 80)

# Создаём обычного менеджера
db = SessionLocal()
manager = User(phone="79000000003", full_name="Manager", password_hash=hash_password("test-pass-2601"),
               role=UserRole.MANAGER, source="self")
db.add(manager)
db.commit()
manager_id = manager.id
db.close()

print("\n3. Chief пробует повысить manager до admin...")
resp = client.post(f"/settings/users/{manager_id}/role", data={"role": "admin"}, follow_redirects=False)
print(f"   Статус: {resp.status_code}")
print(f"   Location: {resp.headers.get('location', 'нет редиректа')}")
if "error" in resp.headers.get("location", ""):
    print(f"   ✓ Отклонено: {resp.headers['location']}")
else:
    print(f"   ✗ ОПАСНОСТЬ: запрос прошёл!")

db = SessionLocal()
manager_user = db.get(User, manager_id)
print(f"   Роль manager: {manager_user.role}")
if manager_user.role == UserRole.ADMIN:
    print("   ✗✗✗ БАГ: manager стал admin!")
else:
    print("   ✓ Роль не изменилась")
db.close()

print("\n" + "=" * 80)
print("ИТОГ")
print("=" * 80)
print("Все проверки завершены. Смотрите на ✗ — это баги.")
print()
