#!/usr/bin/env python3
"""
Utility script to immediately remove the four demo residents and demo rooms
from the PostgreSQL database.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import psycopg
from psycopg.rows import dict_row

BASE_DIR = Path(__file__).resolve().parent

env_file = BASE_DIR / ".env"
if env_file.exists():
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))

database_url = os.environ.get("DATABASE_URL")
if len(sys.argv) > 1:
    database_url = sys.argv[1]

if not database_url:
    print("❌ Error: DATABASE_URL is not set.")
    print("\nPlease provide your PostgreSQL DATABASE_URL by doing one of:")
    print("  1. Run: python3 clean_demo_data.py '<postgresql://...>'")
    print("  2. Set in your environment: export DATABASE_URL='<postgresql://...>'")
    print("  3. Create a .env file with DATABASE_URL=<postgresql://...>")
    sys.exit(1)

print("Connecting to PostgreSQL...")
try:
    with psycopg.connect(database_url, row_factory=dict_row) as db:
        from app import cleanup_demo_data
        cleanup_demo_data(db)
        print("✅ Successfully removed demo residents and demo rooms from PostgreSQL!")
except Exception as e:
    print(f"❌ Error executing cleanup: {e}")
    sys.exit(1)
