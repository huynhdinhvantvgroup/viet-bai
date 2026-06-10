"""One-time migration: SQLite → PostgreSQL.

Usage:
  1. Set DATABASE_URL in .env to your PostgreSQL connection string
  2. Run: python migrate_to_postgres.py
"""
import os
import sqlite3
from pathlib import Path
from dotenv import load_dotenv
from sqlalchemy import create_engine, text
from app.models import Base

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
SQLITE_PATH = BASE_DIR / "database.sqlite"
PG_URL = os.getenv("DATABASE_URL", "")

if not PG_URL or PG_URL.startswith("sqlite"):
    print("ERROR: Set DATABASE_URL in .env to a PostgreSQL connection string first.")
    print("Example: DATABASE_URL=postgresql://healthwriter:password@localhost:5432/healthwriter")
    exit(1)

if not SQLITE_PATH.exists():
    print(f"ERROR: SQLite database not found at {SQLITE_PATH}")
    exit(1)

# Connect to both databases
sqlite_conn = sqlite3.connect(str(SQLITE_PATH))
sqlite_conn.row_factory = sqlite3.Row

pg_engine = create_engine(PG_URL)

# Create tables in PostgreSQL
print("[1/4] Creating tables in PostgreSQL...")
Base.metadata.create_all(bind=pg_engine)

# Tables to migrate
tables = ["users", "articles", "adsense_tokens"]

with pg_engine.begin() as pg_conn:
    for table_name in tables:
        print(f"[2/4] Migrating table: {table_name}...")

        rows = sqlite_conn.execute(f"SELECT * FROM {table_name}").fetchall()
        if not rows:
            print(f"  → {table_name}: 0 rows (empty)")
            continue

        columns = rows[0].keys()
        col_list = ", ".join(columns)
        param_list = ", ".join(f":{c}" for c in columns)

        # Clear existing data in PG table (in case of re-run)
        pg_conn.execute(text(f"DELETE FROM {table_name}"))

        # Insert rows
        for row in rows:
            row_dict = dict(row)
            pg_conn.execute(text(f"INSERT INTO {table_name} ({col_list}) VALUES ({param_list})"), row_dict)

        print(f"  → {table_name}: {len(rows)} rows migrated")

    # Reset PostgreSQL sequences to avoid ID collisions
    print("[3/4] Resetting PostgreSQL sequences...")
    for table_name in tables:
        try:
            pg_conn.execute(text(
                f"SELECT setval(pg_get_serial_sequence('{table_name}', 'id'), "
                f"COALESCE((SELECT MAX(id) FROM {table_name}), 0) + 1, false)"
            ))
            print(f"  → {table_name}: sequence reset OK")
        except Exception as e:
            print(f"  → {table_name}: sequence reset skipped ({e})")

sqlite_conn.close()

print("[4/4] Migration complete!")
print()
print("Next steps:")
print("  1. Verify data: psql -U healthwriter -d healthwriter -c 'SELECT count(*) FROM users;'")
print("  2. Start the app and test login, dashboard, article creation")
print("  3. Keep database.sqlite as backup for rollback")
