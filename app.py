from __future__ import annotations

import csv
import io
import os
import re
import urllib.parse
import zipfile

from datetime import date, datetime

from decimal import Decimal, ROUND_HALF_UP

from pathlib import Path

import psycopg

from psycopg.rows import dict_row

from flask import Flask, flash, g, redirect, render_template, request, send_file, url_for

BASE_DIR = Path(__file__).resolve().parent

env_file = BASE_DIR / ".env"
if env_file.exists():
    with open(env_file, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip("'").strip('"'))

DATABASE_URL = os.environ.get("DATABASE_URL")

PAYMENT_METHODS = ("Cash", "Bank Transfer", "Online/Mobile Transfer", "Other")

def create_app(test_config: dict | None = None) -> Flask:

    app = Flask(__name__)

    app.config.from_mapping(

        SECRET_KEY=os.environ.get("SECRET_KEY", "change-this-secret-key"),

        DATABASE_URL=DATABASE_URL,

    )

    if test_config:

        app.config.update(test_config)

    @app.before_request

    def before_request() -> None:

        g.db = get_db()

    @app.teardown_appcontext

    def close_db(error: Exception | None = None) -> None:

        db = g.pop("db", None)

        if db is not None:

            db.close()

    @app.context_processor

    def inject_helpers() -> dict:

        return {

            "money": money,

            "today": date.today().isoformat(),

            "payment_methods": PAYMENT_METHODS,

        }

    @app.route("/")

    def dashboard():

        month = request.args.get("month") or current_month()

        ensure_monthly_fees(month)

        refresh_all_statuses(month)

        totals = g.db.execute(

            """

            SELECT

                COUNT(*) AS count,

                COALESCE(SUM(amount_due), 0) AS due,

                COALESCE(SUM(amount_paid), 0) AS paid,

                COALESCE(SUM(balance), 0) AS balance

            FROM monthly_fees

            WHERE month = %s

            """,

            (month,),

        ).fetchone()

        statuses = g.db.execute(

            "SELECT status, COUNT(*) AS count FROM monthly_fees WHERE month = %s GROUP BY status",

            (month,),

        ).fetchall()

        recent_payments = g.db.execute(

            """

            SELECT p.*, r.full_name, mf.month

            FROM payments p

            JOIN monthly_fees mf ON mf.id = p.monthly_fee_id

            JOIN residents r ON r.id = mf.resident_id

            ORDER BY p.created_at DESC

            LIMIT 8

            """

        ).fetchall()

        breakdown = collection_breakdown(month)

        fees = fee_rows(month)

        return render_template(

            "dashboard.html",

            active="dashboard",

            month=month,

            totals=totals,

            statuses={row["status"]: row["count"] for row in statuses},

            recent_payments=recent_payments,

            breakdown=breakdown,

            fees=fees,

        )

    @app.route("/residents", methods=["GET", "POST"])

    def residents():

        if request.method == "POST":

            g.db.execute(
                """
                INSERT INTO residents (
                    full_name, phone, nic, date_of_birth, address, faculty,
                    guardian_name, guardian_phone, room_id, monthly_fee, status
                )
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    request.form["full_name"].strip(),
                    request.form.get("phone", "").strip(),
                    request.form.get("nic", "").strip(),
                    request.form.get("date_of_birth") or None,
                    request.form.get("address", "").strip(),
                    request.form.get("faculty", "").strip(),
                    request.form.get("guardian_name", "").strip(),
                    request.form.get("guardian_phone", "").strip(),
                    value_or_none(request.form.get("room_id")),
                    decimal_string(request.form.get("monthly_fee", "0")),
                    request.form.get("status", "Active"),
                ),
            )

            g.db.commit()

            flash("Resident saved successfully.", "success")

            return redirect(url_for("residents"))

        rows = g.db.execute(

            """

            SELECT r.*, rooms.room_number

            FROM residents r

            LEFT JOIN rooms ON rooms.id = r.room_id

            ORDER BY r.status, r.full_name

            """

        ).fetchall()

        rooms = g.db.execute("SELECT * FROM rooms ORDER BY room_number").fetchall()

        return render_template("residents.html", active="residents", residents=rows, rooms=rooms)

    @app.route("/residents/<int:resident_id>/edit", methods=["GET", "POST"])

    def edit_resident(resident_id: int):

        resident = get_one("SELECT * FROM residents WHERE id = %s", (resident_id,))

        if request.method == "POST":

            g.db.execute(
                """
                UPDATE residents
                SET full_name = %s, phone = %s, nic = %s, date_of_birth = %s, address = %s, faculty = %s,
                    guardian_name = %s, guardian_phone = %s, room_id = %s, monthly_fee = %s, status = %s
                WHERE id = %s
                """,
                (
                    request.form["full_name"].strip(),
                    request.form.get("phone", "").strip(),
                    request.form.get("nic", "").strip(),
                    request.form.get("date_of_birth") or None,
                    request.form.get("address", "").strip(),
                    request.form.get("faculty", "").strip(),
                    request.form.get("guardian_name", "").strip(),
                    request.form.get("guardian_phone", "").strip(),
                    value_or_none(request.form.get("room_id")),
                    decimal_string(request.form.get("monthly_fee", "0")),
                    request.form.get("status", "Active"),
                    resident_id,
                ),
            )

            g.db.commit()

            flash("Resident updated.", "success")

            return redirect(url_for("residents"))

        rooms = g.db.execute("SELECT * FROM rooms ORDER BY room_number").fetchall()

        return render_template("resident_form.html", active="residents", resident=resident, rooms=rooms)

    @app.route("/residents/<int:resident_id>/delete", methods=["POST"])
    def delete_resident(resident_id: int):
        resident = get_one("SELECT * FROM residents WHERE id = %s", (resident_id,))
        with g.db.cursor() as cur:
            # Delete payments associated with this resident's monthly fees
            cur.execute(
                """
                DELETE FROM payments
                WHERE monthly_fee_id IN (
                    SELECT id FROM monthly_fees WHERE resident_id = %s
                )
                """,
                (resident_id,),
            )
            # Delete monthly fees for this resident
            cur.execute(
                "DELETE FROM monthly_fees WHERE resident_id = %s",
                (resident_id,),
            )
            # Delete resident
            cur.execute(
                "DELETE FROM residents WHERE id = %s",
                (resident_id,),
            )
        log_audit(
            "DELETE",
            "residents",
            str(resident_id),
            resident["full_name"],
            "Deleted resident and all related monthly fee and payment records",
        )
        g.db.commit()
        flash(f"Resident '{resident['full_name']}' was deleted successfully.", "success")
        return redirect(url_for("residents"))

    @app.route("/rooms", methods=["GET", "POST"])

    def rooms():

        if request.method == "POST":

            g.db.execute(

                "INSERT INTO rooms (room_number, floor, capacity, notes) VALUES (%s, %s, %s, %s)",

                (

                    request.form["room_number"].strip(),

                    request.form.get("floor", "").strip(),

                    int(request.form.get("capacity") or 1),

                    request.form.get("notes", "").strip(),

                ),

            )

            g.db.commit()

            flash("Room saved successfully.", "success")

            return redirect(url_for("rooms"))

        rows = g.db.execute(

            """

            SELECT rooms.*, COUNT(residents.id) AS occupied

            FROM rooms

            LEFT JOIN residents ON residents.room_id = rooms.id AND residents.status = 'Active'

            GROUP BY rooms.id

            ORDER BY rooms.room_number

            """

        ).fetchall()

        return render_template("rooms.html", active="rooms", rooms=rows)

    @app.route("/rooms/<int:room_id>/edit", methods=["GET", "POST"])
    def edit_room(room_id: int):
        room = get_one("SELECT * FROM rooms WHERE id = %s", (room_id,))
        if request.method == "POST":
            room_number = request.form["room_number"].strip()
            floor = request.form.get("floor", "").strip()
            capacity = int(request.form.get("capacity") or 1)
            notes = request.form.get("notes", "").strip()
            g.db.execute(
                """
                UPDATE rooms
                SET room_number = %s, floor = %s, capacity = %s, notes = %s
                WHERE id = %s
                """,
                (room_number, floor, capacity, notes, room_id),
            )
            log_audit("UPDATE", "rooms", str(room_id), room["room_number"], f"Updated room {room_number}")
            g.db.commit()
            flash(f"Room '{room_number}' updated successfully.", "success")
            return redirect(url_for("rooms"))

        occupied_count = g.db.execute(
            "SELECT COUNT(*) AS count FROM residents WHERE room_id = %s AND status = 'Active'",
            (room_id,),
        ).fetchone()["count"]
        return render_template("room_form.html", active="rooms", room=room, occupied=occupied_count)

    @app.route("/rooms/<int:room_id>/delete", methods=["POST"])
    def delete_room(room_id: int):
        room = get_one("SELECT * FROM rooms WHERE id = %s", (room_id,))
        with g.db.cursor() as cur:
            cur.execute("UPDATE residents SET room_id = NULL WHERE room_id = %s", (room_id,))
            cur.execute("DELETE FROM rooms WHERE id = %s", (room_id,))
        log_audit("DELETE", "rooms", str(room_id), room["room_number"], "Deleted room; unassigned associated residents")
        g.db.commit()
        flash(f"Room '{room['room_number']}' deleted successfully.", "success")
        return redirect(url_for("rooms"))

    @app.route("/fees", methods=["GET", "POST"])

    def fees():

        month = request.values.get("month") or current_month()

        if request.method == "POST":

            ensure_monthly_fees(month)

            for key, value in request.form.items():

                if key.startswith("due_date_"):

                    fee_id = int(key.replace("due_date_", ""))

                    amount = decimal_string(request.form.get(f"amount_due_{fee_id}", "0"))

                    g.db.execute(

                        "UPDATE monthly_fees SET due_date = %s, amount_due = %s WHERE id = %s",

                        (value, amount, fee_id),

                    )

            g.db.commit()

            refresh_all_statuses(month)

            flash("Monthly fees updated.", "success")

            return redirect(url_for("fees", month=month))

        ensure_monthly_fees(month)

        refresh_all_statuses(month)

        return render_template("fees.html", active="fees", month=month, fees=fee_rows(month))

    @app.route("/fees/<int:fee_id>/edit", methods=["GET", "POST"])
    def edit_fee(fee_id: int):
        fee = get_one(
            """
            SELECT mf.*, r.full_name, r.phone, r.nic, rooms.room_number
            FROM monthly_fees mf
            JOIN residents r ON r.id = mf.resident_id
            LEFT JOIN rooms ON rooms.id = r.room_id
            WHERE mf.id = %s
            """,
            (fee_id,),
        )
        if request.method == "POST":
            due_date = request.form["due_date"].strip()
            amount_due = decimal_string(request.form.get("amount_due", "0"))
            before_val = f"Due: {fee['amount_due']}, Due Date: {fee['due_date']}"
            after_val = f"Due: {amount_due}, Due Date: {due_date}"

            g.db.execute(
                "UPDATE monthly_fees SET due_date = %s, amount_due = %s WHERE id = %s",
                (due_date, amount_due, fee_id),
            )
            g.db.commit()
            update_fee_status(fee_id)
            log_audit("UPDATE", "monthly_fees", str(fee_id), before_val, after_val)
            g.db.commit()
            flash(f"Fee for '{fee['full_name']}' ({fee['month']}) updated successfully.", "success")
            return redirect(url_for("fees", month=fee["month"]))

        return render_template("fee_form.html", active="fees", fee=fee)

    @app.route("/payments", methods=["GET", "POST"])

    def payments():

        month = request.values.get("month") or current_month()

        ensure_monthly_fees(month)

        refresh_all_statuses(month)

        if request.method == "POST":

            monthly_fee_id = int(request.form["monthly_fee_id"])

            method = request.form["payment_method"]

            if method not in PAYMENT_METHODS:

                flash("Choose a valid payment method.", "error")

                return redirect(url_for("payments", month=month))

            if method in ("Bank Transfer", "Online/Mobile Transfer") and not request.form.get("transaction_reference", "").strip():

                flash("Transaction/reference is required for bank or online/mobile transfers.", "error")

                return redirect(url_for("payments", month=month))

            receipt_number = next_receipt_number()

            g.db.execute(

                """

                INSERT INTO payments

                    (monthly_fee_id, receipt_number, amount, payment_method, bank_name,

                     transaction_reference, payment_date, notes, created_at)

                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)

                """,

                (

                    monthly_fee_id,

                    receipt_number,

                    decimal_string(request.form["amount"]),

                    method,

                    request.form.get("bank_name", "").strip(),

                    request.form.get("transaction_reference", "").strip(),

                    request.form.get("payment_date") or date.today().isoformat(),

                    request.form.get("notes", "").strip(),

                    now(),

                ),

            )

            log_audit("CREATE_PAYMENT", "payments", receipt_number, "", f"Recorded {receipt_number}")

            g.db.commit()

            update_fee_status(monthly_fee_id)

            flash(f"Payment recorded. Receipt {receipt_number} created.", "success")

            return redirect(url_for("receipt", receipt_number=receipt_number))

        history = payment_history(month)

        return render_template("payments.html", active="payments", month=month, fees=fee_rows(month), payments=history)

    @app.route("/payments/<int:payment_id>/edit", methods=["GET", "POST"])

    def edit_payment(payment_id: int):

        payment = payment_detail(payment_id)

        if payment["is_reversed"]:

            flash("Reversed payments cannot be edited.", "error")

            return redirect(url_for("payments", month=payment["month"]))

        if request.method == "POST":

            method = request.form["payment_method"]

            if method not in PAYMENT_METHODS:

                flash("Choose a valid payment method.", "error")

                return redirect(url_for("edit_payment", payment_id=payment_id))

            before = dict(payment)

            g.db.execute(

                """

                UPDATE payments

                SET amount = %s, payment_method = %s, bank_name = %s, transaction_reference = %s,

                    payment_date = %s, notes = %s, updated_at = %s

                WHERE id = %s

                """,

                (

                    decimal_string(request.form["amount"]),

                    method,

                    request.form.get("bank_name", "").strip(),

                    request.form.get("transaction_reference", "").strip(),

                    request.form.get("payment_date") or date.today().isoformat(),

                    request.form.get("notes", "").strip(),

                    now(),

                    payment_id,

                ),

            )

            after = dict(payment_detail(payment_id, fresh=False))

            log_audit("EDIT_PAYMENT", "payments", payment["receipt_number"], str(before), str(after))

            g.db.commit()

            update_fee_status(payment["monthly_fee_id"])

            flash("Payment updated and audit trail recorded.", "success")

            return redirect(url_for("payments", month=payment["month"]))

        return render_template("payment_form.html", active="payments", payment=payment)

    @app.route("/payments/<int:payment_id>/reverse", methods=["POST"])

    def reverse_payment(payment_id: int):

        payment = payment_detail(payment_id)

        reason = request.form.get("reason", "").strip() or "No reason provided"

        g.db.execute(

            "UPDATE payments SET is_reversed = 1, reversal_reason = %s, updated_at = %s WHERE id = %s",

            (reason, now(), payment_id),

        )

        log_audit("REVERSE_PAYMENT", "payments", payment["receipt_number"], "", reason)

        g.db.commit()

        update_fee_status(payment["monthly_fee_id"])

        flash("Payment reversed. The original record remains in history.", "success")

        return redirect(url_for("payments", month=payment["month"]))

    @app.route("/receipt/<receipt_number>")

    def receipt(receipt_number: str):

        payment = g.db.execute(

            """

            SELECT p.*, mf.month, mf.amount_due, mf.amount_paid, mf.balance,

                   r.full_name, r.phone, rooms.room_number

            FROM payments p

            JOIN monthly_fees mf ON mf.id = p.monthly_fee_id

            JOIN residents r ON r.id = mf.resident_id

            LEFT JOIN rooms ON rooms.id = r.room_id

            WHERE p.receipt_number = %s

            """,

            (receipt_number,),

        ).fetchone()

        if not payment:

            flash("Receipt not found.", "error")

            return redirect(url_for("payments"))

        return render_template("receipt.html", active="payments", payment=payment)

    @app.route("/reminders")
    def reminders():
        month = request.args.get("month") or current_month()
        ensure_monthly_fees(month)
        refresh_all_statuses(month)
        rows = g.db.execute(
            """
            SELECT mf.*, r.full_name, r.phone, r.nic, rooms.room_number
            FROM monthly_fees mf
            JOIN residents r ON r.id = mf.resident_id
            LEFT JOIN rooms ON rooms.id = r.room_id
            WHERE mf.month = %s AND mf.status IN ('Unpaid', 'Part Paid', 'Overdue')
            ORDER BY mf.status, r.full_name
            """,
            (month,),
        ).fetchall()
        enriched = []
        for row in rows:
            row_dict = dict(row)
            row_dict["whatsapp_message"] = build_whatsapp_notice(row_dict)
            row_dict["whatsapp_url"] = generate_whatsapp_url(row_dict.get("phone"), row_dict["whatsapp_message"])
            enriched.append(row_dict)
        return render_template("reminders.html", active="reminders", month=month, reminders=enriched)

    @app.route("/reports")

    def reports():

        month = request.args.get("month") or current_month()

        ensure_monthly_fees(month)

        refresh_all_statuses(month)

        return render_template(

            "reports.html",

            active="reports",

            month=month,

            fees=fee_rows(month),

            payments=payment_history(month),

            breakdown=collection_breakdown(month),

        )

    @app.route("/audit")

    def audit():

        rows = g.db.execute("SELECT * FROM audit_log ORDER BY created_at DESC, id DESC LIMIT 100").fetchall()

        return render_template("audit.html", active="audit", audits=rows)

    @app.route("/download-database")

    def download_database():

        return send_postgres_backup()

    @app.cli.command("cleanup-demo-data")
    def cleanup_demo_data_command():
        with app.app_context():
            with get_db() as db:
                cleanup_demo_data(db)
        print("Demo data successfully removed from PostgreSQL.")

    @app.cli.command("import-form-data")
    def import_form_data_command():
        with app.app_context():
            with get_db() as db:
                import_form_residents(db)
                ensure_monthly_fees_db(db, current_month())
                refresh_all_statuses_db(db, current_month())
        print("Form residents and rooms successfully imported.")

    @app.cli.command("seed-data")
    def seed_data_command():
        with app.app_context():
            with get_db() as db:
                seed_data(db)
        print("Demo data seeded.")

    with app.app_context():
        if app.config.get("DATABASE_URL"):
            init_db()

    return app

def get_db():

    database_url = current_app_database_url()

    return psycopg.connect(database_url, row_factory=dict_row)

def current_app_database_url() -> str:

    from flask import current_app

    database_url = current_app.config.get("DATABASE_URL")

    if not database_url:

        raise RuntimeError("DATABASE_URL is not set. Add the Neon PostgreSQL DATABASE_URL environment variable in Vercel.")

    return database_url

def execute_script(db, script: str) -> None:

    with db.cursor() as cursor:

        for statement in [part.strip() for part in script.split(";") if part.strip()]:

            cursor.execute(statement)

def init_db() -> None:

    with psycopg.connect(current_app_database_url(), row_factory=dict_row) as db:

        execute_script(

            db,

            """

            CREATE TABLE IF NOT EXISTS rooms (

                id SERIAL PRIMARY KEY,

                room_number TEXT NOT NULL UNIQUE,

                floor TEXT,

                capacity INTEGER NOT NULL DEFAULT 1,

                notes TEXT

            );

            CREATE TABLE IF NOT EXISTS residents (

                id SERIAL PRIMARY KEY,

                full_name TEXT NOT NULL,

                phone TEXT,

                nic TEXT,

                date_of_birth DATE,

                address TEXT,

                faculty TEXT,

                guardian_name TEXT,

                guardian_phone TEXT,

                room_id INTEGER REFERENCES rooms(id),

                monthly_fee NUMERIC(12, 2) NOT NULL DEFAULT 0,

                status TEXT NOT NULL DEFAULT 'Active'

            );

            CREATE TABLE IF NOT EXISTS monthly_fees (

                id SERIAL PRIMARY KEY,

                resident_id INTEGER NOT NULL REFERENCES residents(id),

                month TEXT NOT NULL,

                due_date DATE NOT NULL,

                amount_due NUMERIC(12, 2) NOT NULL,

                amount_paid NUMERIC(12, 2) NOT NULL DEFAULT 0,

                balance NUMERIC(12, 2) NOT NULL DEFAULT 0,

                status TEXT NOT NULL DEFAULT 'Unpaid',

                UNIQUE (resident_id, month)

            );

            CREATE TABLE IF NOT EXISTS payments (

                id SERIAL PRIMARY KEY,

                monthly_fee_id INTEGER NOT NULL REFERENCES monthly_fees(id),

                receipt_number TEXT NOT NULL UNIQUE,

                amount NUMERIC(12, 2) NOT NULL,

                payment_method TEXT NOT NULL,

                bank_name TEXT,

                transaction_reference TEXT,

                payment_date DATE NOT NULL,

                notes TEXT,

                is_reversed INTEGER NOT NULL DEFAULT 0,

                reversal_reason TEXT,

                created_at TIMESTAMP NOT NULL,

                updated_at TIMESTAMP

            );

            CREATE TABLE IF NOT EXISTS audit_log (

                id SERIAL PRIMARY KEY,

                action TEXT NOT NULL,

                table_name TEXT NOT NULL,

                record_key TEXT NOT NULL,

                before_value TEXT,

                after_value TEXT,

                created_at TIMESTAMP NOT NULL

            );

            ALTER TABLE residents ADD COLUMN IF NOT EXISTS nic TEXT;
            ALTER TABLE residents ADD COLUMN IF NOT EXISTS date_of_birth DATE;
            ALTER TABLE residents ADD COLUMN IF NOT EXISTS address TEXT;
            ALTER TABLE residents ADD COLUMN IF NOT EXISTS faculty TEXT;

            """,

        )

        db.commit()

        cleanup_demo_data(db)
        import_form_residents(db)
        ensure_monthly_fees_db(db, current_month())

def cleanup_demo_data(db) -> None:
    demo_residents = ["Amani Perera", "Nuwan Silva", "Kavindi Fernando", "Rashid Khan"]
    demo_rooms = ["A101", "A102", "B201", "B202"]

    with db.cursor() as cur:
        # 1. Delete payments tied to demo residents' monthly fees
        cur.execute(
            """
            DELETE FROM payments
            WHERE monthly_fee_id IN (
                SELECT mf.id
                FROM monthly_fees mf
                JOIN residents r ON r.id = mf.resident_id
                WHERE r.full_name = ANY(%s)
            )
            """,
            (demo_residents,),
        )

        # 2. Delete monthly fees for demo residents
        cur.execute(
            """
            DELETE FROM monthly_fees
            WHERE resident_id IN (
                SELECT id FROM residents WHERE full_name = ANY(%s)
            )
            """,
            (demo_residents,),
        )

        # 3. Delete demo residents
        cur.execute(
            "DELETE FROM residents WHERE full_name = ANY(%s)",
            (demo_residents,),
        )

        # 4. Delete demo rooms if no other residents are assigned to them
        cur.execute(
            """
            DELETE FROM rooms
            WHERE room_number = ANY(%s)
              AND id NOT IN (
                SELECT DISTINCT room_id FROM residents WHERE room_id IS NOT NULL
              )
            """,
            (demo_rooms,),
        )

    db.commit()


FORM_ROOMS = [
    {"room_number": "Ground Floor Room 1", "floor": "Ground", "capacity": 2, "notes": "Ground floor room 1"},
    {"room_number": "Ground Floor Room 3", "floor": "Ground", "capacity": 2, "notes": "Ground floor room 3"},
    {"room_number": "1st Floor", "floor": "1", "capacity": 2, "notes": "First floor"},
    {"room_number": "Room 3", "floor": "1", "capacity": 2, "notes": "First floor, third room"},
    {"room_number": "Room 2", "floor": "2", "capacity": 2, "notes": "Second floor"},
    {"room_number": "Room 4", "floor": "2", "capacity": 2, "notes": "Second floor, room no 04"},
    {"room_number": "Room 6", "floor": "2", "capacity": 2, "notes": "Second floor, room no 06"},
    {"room_number": "2nd Floor", "floor": "2", "capacity": 4, "notes": "Second floor shared"},
    {"room_number": "2nd Floor Room 1", "floor": "2", "capacity": 2, "notes": "Second floor room 1"},
    {"room_number": "2nd Floor Master Bed Room", "floor": "2", "capacity": 4, "notes": "Second floor master bedroom"},
    {"room_number": "Room 1", "floor": "3", "capacity": 1, "notes": "Third floor single room"},
    {"room_number": "3rd Floor", "floor": "3", "capacity": 4, "notes": "Third floor shared"},
]

FORM_RESIDENTS = [
    {
        "full_name": "Ranthotuwila Patabendige Thesanya Sanugi Rathnayaka",
        "phone": "0774419567",
        "nic": "200278302070",
        "date_of_birth": "2002-10-09",
        "address": "No 72, Yatiyana road , Weligama",
        "faculty": "Faculty of Law",
        "guardian_name": "RPR Rathnayaka",
        "guardian_phone": "0714469567",
        "room_number": "3rd Floor",
        "monthly_fee": "14000.00",
    },
    {
        "full_name": "Algewaththage Sudeepa Lakshani",
        "phone": "0759578629",
        "nic": "998032423V",
        "date_of_birth": "1999-10-29",
        "address": "No.06, Ranawiru Priyadarshani Mawatha, Walgama, Matara",
        "faculty": "Faculty of Law",
        "guardian_name": "A.W. Sumith Wsantha",
        "guardian_phone": "0743036808",
        "room_number": "2nd Floor",
        "monthly_fee": "15000.00",
    },
    {
        "full_name": "Diduli Sumanarathna",
        "phone": "0766173696",
        "nic": "200179004367",
        "date_of_birth": "2001-10-16",
        "address": "No 5G, Mendoraduwa, Katubedda",
        "faculty": "Bachelor of Laws",
        "guardian_name": "M. D. N. P. Sumanarathna",
        "guardian_phone": "0779869869",
        "room_number": "2nd Floor Master Bed Room",
        "monthly_fee": "14000.00",
    },
    {
        "full_name": "Isuri Dhananjana Ranaweera",
        "phone": "0716539207",
        "nic": "200376010898",
        "date_of_birth": "2003-09-16",
        "address": "Welangahawaththa, Welivita",
        "faculty": "FMSH",
        "guardian_name": "Kumaradasa Ranaweera",
        "guardian_phone": "0702607554",
        "room_number": "Room 6",
        "monthly_fee": "14000.00",
    },
    {
        "full_name": "Methmi Hansini Karunanayaka",
        "phone": "0707800755",
        "nic": "200266902550",
        "date_of_birth": "2002-06-17",
        "address": "No.8/A Pallimulla, Matara",
        "faculty": "Faculty of law",
        "guardian_name": "Tilak Karunanayaka",
        "guardian_phone": "0714773653",
        "room_number": "Room 2",
        "monthly_fee": "15000.00",
    },
    {
        "full_name": "P. G. Nipuni Imasha",
        "phone": "0763660049",
        "nic": "200351201825",
        "date_of_birth": "2003-01-12",
        "address": "Mayura Hardware, Kahaduwa",
        "faculty": "Engineering",
        "guardian_name": "P. G.M.P.Kumara",
        "guardian_phone": "0718181456",
        "room_number": "Room 4",
        "monthly_fee": "15500.00",
    },
    {
        "full_name": "Bothalage Dona Isakya Malsandie Darshanapriya",
        "phone": "0771371410",
        "nic": "200581101843",
        "date_of_birth": "2005-11-06",
        "address": "5/A/1, Temple Road, Kalutara",
        "faculty": "Medicine",
        "guardian_name": "Ayesh Darshanapriya",
        "guardian_phone": "0773419914",
        "room_number": "Room 1",
        "monthly_fee": "27000.00",
    },
    {
        "full_name": "Landage Bhashini Dewindi",
        "phone": "0765702665",
        "nic": "200473703846",
        "date_of_birth": "2004-08-24",
        "address": "No 22, Sobanillagama, Ratnapura",
        "faculty": "FDDS (Strategic Studies)",
        "guardian_name": "Landage Bandula",
        "guardian_phone": "0765702665",
        "room_number": "2nd Floor",
        "monthly_fee": "14500.00",
    },
    {
        "full_name": "Yamuditha Heshani Pathirana",
        "phone": "0718992635",
        "nic": "200358400961",
        "date_of_birth": "2003-03-24",
        "address": "Yataththewala, Yakvila",
        "faculty": "Engineering",
        "guardian_name": "WPS Pathirana",
        "guardian_phone": "0718046323",
        "room_number": "1st Floor",
        "monthly_fee": "15500.00",
    },
    {
        "full_name": "Ashanie Sulakkhana Bandara",
        "phone": "+64224322709",
        "nic": "200578805853",
        "date_of_birth": "2005-10-14",
        "address": "Mahasen paya, 1st lane, Hingurakgoda",
        "faculty": "Medicine",
        "guardian_name": "Asanga",
        "guardian_phone": "+64223412441",
        "room_number": "Room 3",
        "monthly_fee": "22000.00",
    },
    {
        "full_name": "Tharushi Ramodya Ranaweera",
        "phone": "0703116438",
        "nic": "200154502582",
        "date_of_birth": "2001-02-14",
        "address": "No 141, Kapugama, Agalawatta",
        "faculty": "Engineering",
        "guardian_name": "Priyantha Ranaweera",
        "guardian_phone": "0761373080",
        "room_number": "3rd Floor",
        "monthly_fee": "14500.00",
    },
    {
        "full_name": "M.W.Dehemi Nethmini Bandara",
        "phone": "0774965908",
        "nic": "200385511645",
        "date_of_birth": "2003-12-20",
        "address": "Kandy road ,Thirappane",
        "faculty": "FMSH, Logistics management",
        "guardian_name": "M.W.S.Bandara",
        "guardian_phone": "0778292608",
        "room_number": "Room 6",
        "monthly_fee": "14000.00",
    },
    {
        "full_name": "Kirinda Liyanarachchige Thashmi Arundi Liyanarachchi",
        "phone": "0769364130",
        "nic": "200459813526",
        "date_of_birth": "2004-04-07",
        "address": "Lumbini, Pinnaduwa, Walahanduwa",
        "faculty": "FDSS / IR",
        "guardian_name": "K.L.B.C.Liyanarachchi",
        "guardian_phone": "0772797059",
        "room_number": "2nd Floor Room 1",
        "monthly_fee": "14500.00",
    },
    {
        "full_name": "Methuli Anujana Ranasinghe",
        "phone": "0771445996",
        "nic": "200578804951",
        "date_of_birth": "2005-08-25",
        "address": "Kurunegala",
        "faculty": "Faculty of Medicine",
        "guardian_name": "Sampath Janaka Ranasinghe",
        "guardian_phone": "00965 51482285",
        "room_number": "Ground Floor Room 1",
        "monthly_fee": "30000.00",
    },
    {
        "full_name": "Siriwardena Mudalige Dona Ishara Sewmini",
        "phone": "0781892027",
        "nic": "200272503682",
        "date_of_birth": "2002-08-12",
        "address": "302, Makandura road, Badalgama",
        "faculty": "Law",
        "guardian_name": "S.M.D.R.D.Cristopher Appuhamy",
        "guardian_phone": "0777986042",
        "room_number": "Ground Floor Room 3",
        "monthly_fee": "14000.00",
    },
]

def import_form_residents(db) -> None:
    with db.cursor() as cur:
        for r in FORM_ROOMS:
            cur.execute(
                """
                INSERT INTO rooms (room_number, floor, capacity, notes)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (room_number) DO NOTHING
                """,
                (r["room_number"], r["floor"], r["capacity"], r["notes"]),
            )

        cur.execute("SELECT id, room_number FROM rooms")
        room_map = {row["room_number"]: row["id"] for row in cur.fetchall()}

        for r in FORM_RESIDENTS:
            nic = r["nic"].strip()
            full_name = r["full_name"].strip()
            room_id = room_map.get(r["room_number"])

            cur.execute(
                """
                SELECT id, room_id FROM residents
                WHERE (nic IS NOT NULL AND nic != '' AND nic = %s)
                   OR LOWER(full_name) = LOWER(%s)
                """,
                (nic, full_name),
            )
            existing = cur.fetchone()

            if existing:
                assigned_room_id = existing["room_id"] or room_id
                cur.execute(
                    """
                    UPDATE residents
                    SET full_name = %s,
                        phone = %s,
                        nic = %s,
                        date_of_birth = %s,
                        address = %s,
                        faculty = %s,
                        guardian_name = %s,
                        guardian_phone = %s,
                        room_id = %s,
                        monthly_fee = %s,
                        status = 'Active'
                    WHERE id = %s
                    """,
                    (
                        full_name,
                        r["phone"],
                        nic,
                        r["date_of_birth"],
                        r["address"],
                        r["faculty"],
                        r["guardian_name"],
                        r["guardian_phone"],
                        assigned_room_id,
                        r["monthly_fee"],
                        existing["id"],
                    ),
                )
            else:
                cur.execute(
                    """
                    INSERT INTO residents (
                        full_name, phone, nic, date_of_birth, address, faculty,
                        guardian_name, guardian_phone, room_id, monthly_fee, status
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 'Active')
                    """,
                    (
                        full_name,
                        r["phone"],
                        nic,
                        r["date_of_birth"],
                        r["address"],
                        r["faculty"],
                        r["guardian_name"],
                        r["guardian_phone"],
                        room_id,
                        r["monthly_fee"],
                    ),
                )

        # Default amount_due and balance to 0 for fees without any payments
        cur.execute(
            """
            UPDATE monthly_fees
            SET amount_due = 0, balance = 0, status = 'Unpaid'
            WHERE amount_paid = 0
              AND id NOT IN (SELECT DISTINCT monthly_fee_id FROM payments)
            """
        )

    db.commit()

def seed_data(db) -> None:
    import_form_residents(db)

def ensure_monthly_fees(month: str) -> None:
    ensure_monthly_fees_db(g.db, month)

def ensure_monthly_fees_db(db, month: str) -> None:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM residents WHERE status = 'Active'")
        residents = cur.fetchall()
        due_day = 10
        due_date = f"{month}-{due_day:02d}"
        for resident in residents:
            cur.execute(
                """
                INSERT INTO monthly_fees (resident_id, month, due_date, amount_due, balance, status)
                VALUES (%s, %s, %s, %s, %s, 'Unpaid')
                ON CONFLICT (resident_id, month) DO NOTHING
                """,
                (resident["id"], month, due_date, "0", "0"),
            )
        db.commit()

def update_fee_status(monthly_fee_id: int) -> None:
    update_fee_status_db(g.db, monthly_fee_id)

def update_fee_status_db(db, monthly_fee_id: int) -> None:
    with db.cursor() as cur:
        cur.execute("SELECT * FROM monthly_fees WHERE id = %s", (monthly_fee_id,))
        fee = cur.fetchone()
        if not fee:
            return
        cur.execute(
            "SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE monthly_fee_id = %s AND is_reversed = 0",
            (monthly_fee_id,),
        )
        paid = cur.fetchone()["total"]
        due = Decimal(str(fee["amount_due"]))
        paid_decimal = Decimal(str(paid))
        balance = max(Decimal("0"), due - paid_decimal)

        if due == Decimal("0") and paid_decimal == Decimal("0"):
            status = "Unpaid"
        elif paid_decimal >= due and (due > Decimal("0") or paid_decimal > Decimal("0")):
            status = "Paid"
        elif paid_decimal > 0:
            status = "Part Paid"
        elif as_date(fee["due_date"]) < date.today():
            status = "Overdue"
        else:
            status = "Unpaid"

        cur.execute(
            "UPDATE monthly_fees SET amount_paid = %s, balance = %s, status = %s WHERE id = %s",
            (str(paid_decimal), str(balance), status, monthly_fee_id),
        )
    db.commit()

def refresh_all_statuses(month: str) -> None:
    refresh_all_statuses_db(g.db, month)

def refresh_all_statuses_db(db, month: str) -> None:
    with db.cursor() as cur:
        cur.execute("SELECT id FROM monthly_fees WHERE month = %s", (month,))
        rows = cur.fetchall()
        for row in rows:
            update_fee_status_db(db, row["id"])

def fee_rows(month: str):

    return g.db.execute(

        """

        SELECT mf.*, r.full_name, r.phone, r.nic, rooms.room_number

        FROM monthly_fees mf

        JOIN residents r ON r.id = mf.resident_id

        LEFT JOIN rooms ON rooms.id = r.room_id

        WHERE mf.month = %s

        ORDER BY r.full_name

        """,

        (month,),

    ).fetchall()

def payment_history(month: str):

    return g.db.execute(

        """

        SELECT p.*, mf.month, r.full_name

        FROM payments p

        JOIN monthly_fees mf ON mf.id = p.monthly_fee_id

        JOIN residents r ON r.id = mf.resident_id

        WHERE mf.month = %s

        ORDER BY p.payment_date DESC, p.id DESC

        """,

        (month,),

    ).fetchall()

def payment_detail(payment_id: int, fresh: bool = True):

    return get_one(

        """

        SELECT p.*, mf.month, mf.amount_due, mf.amount_paid, mf.balance, r.full_name

        FROM payments p

        JOIN monthly_fees mf ON mf.id = p.monthly_fee_id

        JOIN residents r ON r.id = mf.resident_id

        WHERE p.id = %s

        """,

        (payment_id,),

    )

def collection_breakdown(month: str):

    return g.db.execute(

        """

        SELECT p.payment_method, COALESCE(SUM(p.amount), 0) AS total, COUNT(*) AS count

        FROM payments p

        JOIN monthly_fees mf ON mf.id = p.monthly_fee_id

        WHERE mf.month = %s AND p.is_reversed = 0

        GROUP BY p.payment_method

        ORDER BY total DESC

        """,

        (month,),

    ).fetchall()

def next_receipt_number() -> str:

    prefix = datetime.now().strftime("RCPT-%Y%m")

    count = g.db.execute("SELECT COUNT(*) AS count FROM payments WHERE receipt_number LIKE %s", (f"{prefix}%",)).fetchone()["count"]

    return f"{prefix}-{count + 1:04d}"

def get_one(query: str, params: tuple):

    row = g.db.execute(query, params).fetchone()

    if row is None:

        from flask import abort

        abort(404)

    return row

def log_audit(action: str, table_name: str, record_key: str, before_value: str, after_value: str) -> None:

    g.db.execute(

        """

        INSERT INTO audit_log (action, table_name, record_key, before_value, after_value, created_at)

        VALUES (%s, %s, %s, %s, %s, %s)

        """,

        (action, table_name, record_key, before_value, after_value, now()),

    )

def send_postgres_backup():

    table_names = ["rooms", "residents", "monthly_fees", "payments", "audit_log"]

    output = io.BytesIO()

    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:

        for table_name in table_names:

            rows = g.db.execute(f"SELECT * FROM {table_name} ORDER BY id").fetchall()

            text_output = io.StringIO()

            if rows:

                writer = csv.DictWriter(text_output, fieldnames=list(rows[0].keys()))

                writer.writeheader()

                writer.writerows(rows)

            archive.writestr(f"{table_name}.csv", text_output.getvalue())

    output.seek(0)

    return send_file(output, as_attachment=True, download_name="boarding_fee_postgres_backup.zip", mimetype="application/zip")

def as_date(value) -> date:

    if isinstance(value, date):

        return value

    return date.fromisoformat(str(value))

def current_month() -> str:

    return date.today().strftime("%Y-%m")

def now() -> str:

    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")

def decimal_string(value: str) -> str:

    return str(Decimal(value or "0").quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))

def money(value) -> str:

    return f"{Decimal(str(value or 0)):,.2f}"



def format_month_label(month_str: str) -> str:
    try:
        return datetime.strptime(month_str, "%Y-%m").strftime("%B %Y")
    except Exception:
        return month_str


def clean_phone_for_whatsapp(phone: str | None) -> str:
    if not phone:
        return ""
    digits = re.sub(r"\D", "", phone)
    if not digits:
        return ""
    # Sri Lankan domestic format 07XXXXXXXX (10 digits) -> 947XXXXXXXX
    if len(digits) == 10 and digits.startswith("0"):
        return "94" + digits[1:]
    if len(digits) == 9 and digits.startswith("7"):
        return "94" + digits
    return digits


def build_whatsapp_notice(row: dict) -> str:
    resident_name = row.get("full_name") or "Resident"
    month_label = format_month_label(str(row.get("month") or ""))
    balance_val = money(row.get("balance", 0))
    amount_due_val = money(row.get("amount_due", 0))
    amount_paid_val = money(row.get("amount_paid", 0))
    due_date_val = str(row.get("due_date") or "")

    return (
        f"Dear {resident_name}, this is a reminder that your boarding fee for {month_label} "
        f"has an outstanding balance of Rs. {balance_val}.\n\n"
        f"Fee Details:\n"
        f"• Due Date: {due_date_val}\n"
        f"• Total Due: Rs. {amount_due_val}\n"
        f"• Amount Paid: Rs. {amount_paid_val}\n"
        f"• Outstanding Balance: Rs. {balance_val}\n\n"
        f"Please arrange payment at your earliest convenience. "
        f"If payment has already been made, please disregard this message. Thank you."
    )


def generate_whatsapp_url(phone: str | None, message: str) -> str:
    cleaned_phone = clean_phone_for_whatsapp(phone)
    encoded_message = urllib.parse.quote(message)
    if cleaned_phone:
        return f"https://wa.me/{cleaned_phone}?text={encoded_message}"
    return f"https://wa.me/?text={encoded_message}"

def value_or_none(value: str | None):

    return int(value) if value else None

app = create_app()

if __name__ == "__main__":

    app.run(debug=True)
