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

                INSERT INTO residents (full_name, phone, email, guardian_name, guardian_phone, room_id, monthly_fee, status)

                VALUES (%s, %s, %s, %s, %s, %s, %s, %s)

                """,

                (

                    request.form["full_name"].strip(),

                    request.form.get("phone", "").strip(),

                    request.form.get("email", "").strip(),

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

                SET full_name = %s, phone = %s, email = %s, guardian_name = %s, guardian_phone = %s,

                    room_id = %s, monthly_fee = %s, status = %s

                WHERE id = %s

                """,

                (

                    request.form["full_name"].strip(),

                    request.form.get("phone", "").strip(),

                    request.form.get("email", "").strip(),

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
            SELECT mf.*, r.full_name, r.phone, r.email, rooms.room_number
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

                email TEXT,

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

            """,

        )

        db.commit()

        cleanup_demo_data(db)

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


def seed_data(db) -> None:
    existing = db.execute("SELECT COUNT(*) AS count FROM residents").fetchone()["count"]
    if existing:
        return

    rooms = [
        ("A101", "1", 2, "Window side room"),
        ("A102", "1", 3, "Near study area"),
        ("B201", "2", 2, "Attached bathroom"),
        ("B202", "2", 4, "Shared room"),
    ]

    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO rooms (room_number, floor, capacity, notes)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (room_number) DO NOTHING
            """,
            rooms,
        )

    db.commit()

    room_ids = {
        row["room_number"]: row["id"]
        for row in db.execute("SELECT id, room_number FROM rooms").fetchall()
    }

    residents = [
        ("Amani Perera", "0771234567", "amani@example.com", "Mrs. Perera", "0711002003", room_ids["A101"], "35000", "Active"),
        ("Nuwan Silva", "0762223344", "nuwan@example.com", "Mr. Silva", "0712223344", room_ids["A102"], "32000", "Active"),
        ("Kavindi Fernando", "0755551188", "kavindi@example.com", "Mr. Fernando", "0705551188", room_ids["B201"], "38000", "Active"),
        ("Rashid Khan", "0748889900", "rashid@example.com", "Mrs. Khan", "0728889900", room_ids["B202"], "30000", "Active"),
    ]

    with db.cursor() as cur:
        cur.executemany(
            """
            INSERT INTO residents
                (full_name, phone, email, guardian_name, guardian_phone, room_id, monthly_fee, status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
            """,
            residents,
        )

    db.commit()

def ensure_monthly_fees(month: str) -> None:

    residents = g.db.execute("SELECT * FROM residents WHERE status = 'Active'").fetchall()

    due_day = 10

    due_date = f"{month}-{due_day:02d}"

    for resident in residents:

        g.db.execute(

            """

            INSERT INTO monthly_fees (resident_id, month, due_date, amount_due, balance)

            VALUES (%s, %s, %s, %s, %s)

            ON CONFLICT (resident_id, month) DO NOTHING

            """,

            (resident["id"], month, due_date, resident["monthly_fee"], resident["monthly_fee"]),

        )

    g.db.commit()

def update_fee_status(monthly_fee_id: int) -> None:

    fee = get_one("SELECT * FROM monthly_fees WHERE id = %s", (monthly_fee_id,))

    paid = g.db.execute(

        "SELECT COALESCE(SUM(amount), 0) AS total FROM payments WHERE monthly_fee_id = %s AND is_reversed = 0",

        (monthly_fee_id,),

    ).fetchone()["total"]

    due = Decimal(str(fee["amount_due"]))

    paid_decimal = Decimal(str(paid))

    balance = max(Decimal("0"), due - paid_decimal)

    if paid_decimal >= due:

        status = "Paid"

    elif paid_decimal > 0:

        status = "Part Paid"

    elif as_date(fee["due_date"]) < date.today():

        status = "Overdue"

    else:

        status = "Unpaid"

    g.db.execute(

        "UPDATE monthly_fees SET amount_paid = %s, balance = %s, status = %s WHERE id = %s",

        (str(paid_decimal), str(balance), status, monthly_fee_id),

    )

    g.db.commit()

def refresh_all_statuses(month: str) -> None:

    for row in g.db.execute("SELECT id FROM monthly_fees WHERE month = %s", (month,)).fetchall():

        update_fee_status(row["id"])

def fee_rows(month: str):

    return g.db.execute(

        """

        SELECT mf.*, r.full_name, r.phone, rooms.room_number

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
