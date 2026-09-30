#!/usr/bin/env python3
"""
Standalone CLI script to import boarder registration form residents into PostgreSQL.
Usage:
    python3 import_form_data.py [DATABASE_URL]
"""
import os
import sys
from app import (
    FORM_ROOMS,
    FORM_RESIDENTS,
    cleanup_demo_data,
    import_form_residents,
    ensure_monthly_fees_db,
    refresh_all_statuses_db,
    current_month,
)
import psycopg
from psycopg.rows import dict_row

def main():
    db_url = None
    if len(sys.argv) > 1 and sys.argv[1].strip():
        db_url = sys.argv[1].strip()
    else:
        db_url = os.environ.get("DATABASE_URL")

    if not db_url:
        # Check if .env file exists
        env_path = os.path.join(os.path.dirname(__file__), ".env")
        if os.path.exists(env_path):
            with open(env_path, "r", encoding="utf-8") as f:
                for line in f:
                    if line.startswith("DATABASE_URL="):
                        db_url = line.strip().split("=", 1)[1].strip().strip('"').strip("'")
                        break

    if not db_url:
        print("ERROR: DATABASE_URL is not set.")
        print("Please provide it as an argument or set the DATABASE_URL environment variable:")
        print("    python3 import_form_data.py 'postgres://user:pass@host/dbname'")
        sys.exit(1)

    print(f"Connecting to database...")
    with psycopg.connect(db_url, row_factory=dict_row) as db:
        print("Cleaning up any lingering demo data...")
        cleanup_demo_data(db)

        print(f"Importing {len(FORM_ROOMS)} rooms and {len(FORM_RESIDENTS)} residents...")
        import_form_residents(db)

        month = current_month()
        print(f"Ensuring monthly fees for active residents for {month}...")
        ensure_monthly_fees_db(db, month)
        refresh_all_statuses_db(db, month)

        # Print summary
        with db.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS total_rooms FROM rooms")
            total_rooms = cur.fetchone()["total_rooms"]

            cur.execute("SELECT COUNT(*) AS total_residents FROM residents")
            total_residents = cur.fetchone()["total_residents"]

            cur.execute(
                """
                SELECT r.full_name, r.nic, rooms.room_number, r.phone, r.monthly_fee,
                       mf.amount_due, mf.amount_paid, mf.balance, mf.status
                FROM residents r
                LEFT JOIN rooms ON rooms.id = r.room_id
                LEFT JOIN monthly_fees mf ON mf.resident_id = r.id AND mf.month = %s
                ORDER BY r.full_name
                """,
                (month,),
            )
            residents = cur.fetchall()

            print("\n" + "=" * 90)
            print(f"IMPORT COMPLETE: {total_residents} Residents, {total_rooms} Rooms in Database")
            print("=" * 90)
            print(f"{'Resident Name':<35} | {'Room':<12} | {'NIC':<14} | {'Due':<6} | {'Status'}")
            print("-" * 90)
            for row in residents:
                name = row["full_name"][:33]
                room = (row["room_number"] or "-")[:12]
                nic = (row["nic"] or "-")[:14]
                due = str(row["amount_due"] or 0)
                status = row["status"] or "Unpaid"
                print(f"{name:<35} | {room:<12} | {nic:<14} | {due:<6} | {status}")
            print("=" * 90)

if __name__ == "__main__":
    main()
