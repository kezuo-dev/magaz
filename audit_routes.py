#!/usr/bin/env python3
"""Аудит: какие роуты приложения какому разделу сопоставляются."""
import sys
sys.path.insert(0, "/Users/kezuo/projects/magaz")

from app.access import SECTIONS, section_for_path

# Все роуты приложения (из grep @router. app/routes/*.py)
ROUTES = [
    # auth.py
    ("GET", "/register"),
    ("POST", "/register"),
    ("GET", "/register-done"),
    ("GET", "/login"),
    ("POST", "/login"),
    ("POST", "/logout"),
    ("GET", "/account"),
    ("POST", "/account/password"),
    ("GET", "/settings/users"),
    ("POST", "/settings/users/{user_id}/role"),
    ("POST", "/settings/users/{user_id}/delete"),

    # catalog.py
    ("GET", "/"),
    ("GET", "/api/books"),
    ("GET", "/books/{book_id}"),
    ("POST", "/catalog/wipe"),
    ("GET", "/catalog/export/pdf"),
    ("GET", "/catalog/check_forbidden"),
    ("GET", "/catalog/forbidden/pdf"),
    ("POST", "/catalog/reconcile"),
    ("POST", "/catalog/wb_trash"),

    # analytics.py (prefix=/analytics)
    ("GET", "/analytics"),
    ("POST", "/analytics"),

    # imports.py (prefix=/import)
    ("GET", "/import"),
    ("POST", "/import/pull/{marketplace}"),
    ("POST", "/import/sync"),
    ("POST", "/import/upload"),
    ("POST", "/import/run"),

    # log.py (prefix=/log)
    ("GET", "/log"),
    ("GET", "/log/api"),
    ("GET", "/log/by-book"),

    # live.py (prefix=/api/live)
    ("GET", "/api/live/catalog"),
    ("GET", "/api/live/log"),
    ("GET", "/api/live/analytics"),

    # settings.py (prefix=/settings)
    ("GET", "/settings"),
    ("POST", "/settings/sync-enabled"),
    ("POST", "/settings/auto-withdraw"),
    ("POST", "/settings/save"),
    ("POST", "/settings/check"),
]

print("СОПОСТАВЛЕНИЕ РОУТОВ С РАЗДЕЛАМИ:")
print("=" * 80)
print()

print("РАЗДЕЛЫ И ИХ ПРЕФИКСЫ:")
for name, label, prefixes in SECTIONS:
    print(f"  {name:12} ({label:12}): {prefixes}")
print()
print("=" * 80)
print()

no_section = []
by_section = {}

for method, path in ROUTES:
    section = section_for_path(path)
    if section is None:
        no_section.append((method, path))
    else:
        by_section.setdefault(section, []).append((method, path))

print("РОУТЫ ПО РАЗДЕЛАМ:")
print()
for name, _label, _prefixes in SECTIONS:
    if name in by_section:
        print(f"{name.upper()}:")
        for method, path in by_section[name]:
            print(f"  {method:6} {path}")
        print()

if no_section:
    print("!!! РОУТЫ БЕЗ РАЗДЕЛА (НЕ ПРОВЕРЯЮТСЯ MIDDLEWARE):")
    print()
    for method, path in no_section:
        print(f"  {method:6} {path}")
    print()
else:
    print("✓ Все роуты сопоставлены с разделом")
