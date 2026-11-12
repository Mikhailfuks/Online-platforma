import sqlite3
import re
import json
import markdown
import os
import secrets
import uuid
import hashlib
import hmac
import logging
from datetime import datetime, timezone, timedelta
from decimal import Decimal, InvalidOperation
from io import BytesIO
from pathlib import Path
from flask import Flask, abort, g, render_template, request, redirect, url_for, flash, jsonify, send_file, send_from_directory, session as flask_session
from flask_login import (LoginManager, UserMixin, login_user,
                         login_required, logout_user, current_user)
from sqlalchemy import CheckConstraint, DateTime, ForeignKey, Integer, Numeric, String, Table, Column, UniqueConstraint, create_engine, event, select, text
from sqlalchemy.engine import URL
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker
from werkzeug.security import generate_password_hash, check_password_hash
from functools import wraps
from course_data import COURSE

app = Flask(__name__)
def load_flask_secret_key():
    configured_key = os.environ.get("FLASK_SECRET_KEY")
    if configured_key:
        return configured_key

    secret_path = Path(app.instance_path) / "flask_secret_key"
    secret_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        descriptor = os.open(
            secret_path,
            os.O_CREAT | os.O_EXCL | os.O_WRONLY,
            0o600,
        )
    except FileExistsError:
        secret_key = secret_path.read_text(encoding="ascii").strip()
        if not secret_key:
            raise RuntimeError(f"Flask secret key file is empty: {secret_path}")
        return secret_key

    with os.fdopen(descriptor, "w", encoding="ascii") as secret_file:
        secret_key = secrets.token_hex(32)
        secret_file.write(secret_key)
    return secret_key

app.secret_key = load_flask_secret_key()
DATABASE = str(Path(app.root_path) / "site.db")
COURSE_PRICE = Decimal("2990.00")
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024

login_manager = LoginManager(app)
login_manager.login_view = "login"
login_manager.login_message = "Пожалуйста, авторизуйтесь для доступа."
login_manager.login_message_category = "warning"

def get_db():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn

class NotesBase(DeclarativeBase):
    pass

Table(
    "users",
    NotesBase.metadata,
    Column("id", Integer, primary_key=True),
    extend_existing=True,
)

class OperationNote(NotesBase):
    __tablename__ = "operation_notes"
    __table_args__ = (
        CheckConstraint("kind IN ('credit', 'debit')", name="ck_operation_notes_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")

class Card(NotesBase):
    __tablename__ = "cards"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    number: Mapped[str] = mapped_column(String(16), nullable=False, unique=True)
    balance: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False, default=Decimal("0.00"))
    is_active: Mapped[bool] = mapped_column(default=True, nullable=False)

class Course(NotesBase):
    __tablename__ = "courses"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)

class Receipt(NotesBase):
    __tablename__ = "receipts"
    __table_args__ = (
        CheckConstraint("kind IN ('topup', 'course')", name="ck_receipts_kind"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    kind: Mapped[str] = mapped_column(String(10), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False, default="")

class TopUpChallenge(NotesBase):
    __tablename__ = "top_up_challenges"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    card_id: Mapped[int] = mapped_column(ForeignKey("cards.id"), nullable=False, index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    last_sent_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

class Enrollment(NotesBase):
    __tablename__ = "enrollments"
    __table_args__ = (
        UniqueConstraint("user_id", "course_id", name="uq_enrollments_user_course"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), nullable=False, index=True)
    course_id: Mapped[int] = mapped_column(ForeignKey("courses.id"), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=lambda: datetime.now(timezone.utc).replace(tzinfo=None),
        nullable=False,
    )

_notes_engines = {}

def get_notes_engine():
    database_path = str(Path(DATABASE).resolve())
    engine = _notes_engines.get(database_path)
    if engine is None:
        engine = create_engine(
            URL.create("sqlite", database=database_path),
            connect_args={"timeout": 30},
        )

        @event.listens_for(engine, "connect")
        def enable_sqlite_foreign_keys(connection, _):
            cursor = connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

        NotesBase.metadata.create_all(engine)
        _notes_engines[database_path] = engine
    return engine

class _NotesDatabase:
    @property
    def session(self):
        if "operation_note_session" not in g:
            engine = get_notes_engine()
            g.operation_note_engine = engine
            g.operation_note_session = sessionmaker(bind=engine, expire_on_commit=False)()
        return g.operation_note_session

db = _NotesDatabase()

@app.teardown_appcontext
def close_operation_note_session(error=None):
    session = g.pop("operation_note_session", None)
    if session is not None:
        if error is not None:
            session.rollback()
        session.close()

def operation_notes_directory():
    return Path(app.instance_path) / "notes"

def receipts_directory():
    return Path(app.instance_path) / "receipts"

def current_utc_time():
    return datetime.now(timezone.utc).replace(tzinfo=None)

def new_confirmation_code():
    return f"{secrets.randbelow(1_000_000):06d}"

def confirmation_code_hash(code):
    return hmac.new(
        app.secret_key.encode("utf-8"),
        code.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()

def generate_card_number(conn=None):
    owns_connection = conn is None
    if owns_connection:
        conn = get_db()
    try:
        for _ in range(20):
            number = secrets.choice("123456789") + "".join(
                secrets.choice("0123456789") for _ in range(15)
            )
            if not conn.execute("SELECT 1 FROM cards WHERE number=?", (number,)).fetchone():
                return number
        raise RuntimeError("Unable to generate a unique card number.")
    finally:
        if owns_connection:
            conn.close()

def issue_card(user_id):
    session = db.session
    for _ in range(20):
        card = Card(user_id=user_id, number=generate_card_number(), balance=Decimal("0.00"))
        session.add(card)
        try:
            session.flush()
            session.commit()
            return card
        except Exception:
            session.rollback()
            if session.scalar(select(Card.id).where(Card.number == card.number)) is not None:
                continue
            raise
    raise RuntimeError("Unable to issue a unique card.")

def make_receipt_pdf(receipt, user):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font_path = Path(app.root_path) / "fonts" / "DejaVuSans.ttf"
    if not font_path.is_file():
        raise FileNotFoundError(f"PDF font not found: {font_path}")
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", str(font_path)))

    amount = f"{receipt.amount:,.2f}".replace(",", " ").replace(".", ",")
    rows = (
        f"Номер: {receipt.id}",
        f"Дата: {receipt.created_at.strftime('%d.%m.%Y %H:%M')}",
        f"Пользователь: {user.username}",
        f"Описание: {receipt.description}",
        "Статус: Оплачено" if receipt.kind == "course" else "Статус: Зачислено",
        f"{'Итого к оплате' if receipt.kind == 'course' else 'Сумма'}: {amount} ₽",
    )
    output = BytesIO()
    pdf = canvas.Canvas(output, pagesize=A4)
    page_width, page_height = A4
    left = 56
    y = page_height - 72
    pdf.setTitle(f"Квитанция {receipt.id}")
    pdf.setFont("DejaVu", 18)
    pdf.drawString(left, y, "Квитанция операции")
    y -= 42
    pdf.setFont("DejaVu", 11)
    for row in rows:
        for line in simpleSplit(row, "DejaVu", 11, page_width - 2 * left):
            if y < 72:
                pdf.showPage()
                pdf.setFont("DejaVu", 11)
                y = page_height - 72
            pdf.drawString(left, y, line)
            y -= 25
    pdf.save()
    return output.getvalue()

def create_receipt(session, kind, amount, description):
    receipt = Receipt(
        user_id=current_user.id,
        kind=kind,
        amount=amount,
        description=description,
    )
    session.add(receipt)
    session.flush()
    receipt.filename = f"receipt-{receipt.id}.pdf"
    session.flush()
    path = receipts_directory() / receipt.filename
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_bytes(make_receipt_pdf(receipt, current_user))
    except Exception:
        path.unlink(missing_ok=True)
        raise
    return receipt

def make_note_pdf(note, user):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font_path = Path(app.root_path) / "fonts" / "DejaVuSans.ttf"
    if not font_path.is_file():
        raise FileNotFoundError(f"PDF font not found: {font_path}")
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", str(font_path)))

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4
    left = 56
    y = page_height - 72
    formatted_amount = f"{note.amount:,.2f}".replace(",", " ").replace(".", ",")
    pdf.setTitle(f"Операция внутреннего баланса №{note.id}")
    pdf.setFont("DejaVu", 18)
    pdf.drawString(left, y, "Операция внутреннего баланса")
    y -= 42
    pdf.setFont("DejaVu", 11)
    rows = (
        f"Номер: {note.id}",
        f"Дата: {note.created_at.strftime('%d.%m.%Y %H:%M')}",
        f"Пользователь: {user.username}",
        f"Тип операции: {'Зачисление на баланс' if note.kind == 'credit' else 'Оплата с баланса'}",
        f"Описание: {note.description}",
        "Статус: Оплачено" if note.kind == "debit" else "Статус: Зачислено",
        f"Итого к оплате: {formatted_amount} ₽",
    )
    for row in rows:
        for line in simpleSplit(row, "DejaVu", 11, page_width - 2 * left):
            if y < 72:
                pdf.showPage()
                pdf.setFont("DejaVu", 11)
                y = page_height - 72
            pdf.drawString(left, y, line)
            y -= 25
    pdf.save()
    return buffer.getvalue()

def save_operation_note(kind, amount, description):
    session = db.session
    note = OperationNote(
        user_id=current_user.id,
        kind=kind,
        amount=amount,
        description=description,
    )
    session.add(note)
    session.flush()
    note.filename = f"operation-{note.id}.pdf"
    session.flush()
    note_path = operation_notes_directory() / note.filename
    try:
        note_path.parent.mkdir(parents=True, exist_ok=True)
        note_path.write_bytes(make_note_pdf(note, current_user))
        session.commit()
    except Exception:
        session.rollback()
        note_path.unlink(missing_ok=True)
        raise
    return note.id

def get_wallet_balance(user_id):
    conn = get_db()
    rows = conn.execute(
        "SELECT amount_decimal, amount FROM wallet_ledger WHERE user_id = ?",
        (user_id,),
    ).fetchall()
    conn.close()
    balance = sum(
        (Decimal(row["amount_decimal"] if row["amount_decimal"] is not None else str(row["amount"])) for row in rows),
        Decimal("0.00"),
    )
    return balance.quantize(Decimal("0.01"))

def has_full_course_access(user_id):
    conn = get_db()
    purchase = conn.execute(
        """SELECT 1 FROM provider_payments
           WHERE user_id = ? AND purpose = 'course' AND status = 'succeeded'
           LIMIT 1""",
        (user_id,),
    ).fetchone()
    conn.close()
    enrollment = db.session.scalar(
        select(Enrollment.id).where(
            Enrollment.user_id == user_id,
            Enrollment.course_id == 1,
        )
    )
    return purchase is not None or enrollment is not None

def init_db():
    conn = get_db()
    c = conn.cursor()

    c.execute("""CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        email TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        is_admin INTEGER DEFAULT 0,
        xp INTEGER DEFAULT 0,
        registered_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS modules (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT NOT NULL)""")

    c.execute("""CREATE TABLE IF NOT EXISTS lessons (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        module_id INTEGER NOT NULL,
        step_type TEXT NOT NULL,
        slug TEXT UNIQUE NOT NULL,
        title TEXT NOT NULL,
        content TEXT,
        task TEXT,
        expected TEXT,
        quiz_options TEXT,
        quiz_answer INTEGER,
        hint TEXT,
        xp_reward INTEGER DEFAULT 5,
        sort_order INTEGER,
        validation_rules TEXT,
        FOREIGN KEY(module_id) REFERENCES modules(id))""")

    c.execute("""CREATE TABLE IF NOT EXISTS progress (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        lesson_id INTEGER NOT NULL,
        status TEXT DEFAULT 'completed',
        attempts INTEGER DEFAULT 0,
        UNIQUE(user_id, lesson_id))""")

    c.execute("""CREATE TABLE IF NOT EXISTS achievements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id INTEGER NOT NULL,
        title TEXT NOT NULL,
        icon TEXT NOT NULL)""")

    c.execute("""CREATE TABLE IF NOT EXISTS comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        lesson_id INTEGER NOT NULL,
        user_id INTEGER NOT NULL,
        username TEXT NOT NULL,
        text TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)""")

    c.execute("""CREATE TABLE IF NOT EXISTS provider_payments (
        id TEXT PRIMARY KEY,
        user_id INTEGER NOT NULL,
        purpose TEXT NOT NULL CHECK (purpose IN ('course', 'top_up')),
        amount INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'creating',
        method TEXT NOT NULL DEFAULT 'internal_balance',
        provider_payment_id TEXT UNIQUE,
        confirmation_url TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        completed_at TIMESTAMP,
        credited_at TIMESTAMP)""")

    payment_columns = {row["name"] for row in c.execute("PRAGMA table_info(provider_payments)")}
    if "method" not in payment_columns:
        c.execute("ALTER TABLE provider_payments ADD COLUMN method TEXT NOT NULL DEFAULT 'internal_balance'")

    c.execute("""CREATE TABLE IF NOT EXISTS wallet_ledger (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        payment_id TEXT NOT NULL UNIQUE,
        user_id INTEGER NOT NULL,
        amount INTEGER NOT NULL,
        amount_decimal TEXT,
        description TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(payment_id) REFERENCES provider_payments(id))""")

    ledger_columns = {row["name"] for row in c.execute("PRAGMA table_info(wallet_ledger)")}
    if "amount_decimal" not in ledger_columns:
        c.execute("ALTER TABLE wallet_ledger ADD COLUMN amount_decimal TEXT")
    c.execute(
        "UPDATE wallet_ledger SET amount_decimal=CAST(amount AS TEXT) WHERE amount_decimal IS NULL"
    )

    c.execute(
        """CREATE UNIQUE INDEX IF NOT EXISTS one_active_course_payment_per_user
           ON provider_payments(user_id)
           WHERE purpose='course' AND status IN ('creating', 'pending', 'waiting_for_capture')"""
    )

    lesson_columns = {row["name"] for row in c.execute("PRAGMA table_info(lessons)")}
    if "sort_order" not in lesson_columns:
        c.execute("ALTER TABLE lessons ADD COLUMN sort_order INTEGER")
    if "validation_rules" not in lesson_columns:
        c.execute("ALTER TABLE lessons ADD COLUMN validation_rules TEXT")

    for module_id, module in enumerate(COURSE, start=1):
        c.execute(
            """INSERT INTO modules (id, title, description) VALUES (?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET title=excluded.title, description=excluded.description""",
            (module_id, module["title"], module["description"]),
        )
        records = [("theory", "t", "Теория", module["theory"], None, None, None, None, 5, 10)]
        records.extend(
            (
                "practice",
                "p" if practice_number == 1 else f"p{practice_number}",
                practice["title"],
                None,
                practice["task"],
                None,
                json.dumps(practice["rules"], ensure_ascii=False),
                practice["hint"],
                10,
                20 + practice_number,
            )
            for practice_number, practice in enumerate(module["practices"], start=1)
        )
        records.extend(
            (
                "quiz",
                "q" if quiz_number == 1 else f"q{quiz_number}",
                f"Тест {quiz_number}",
                None,
                question["question"],
                None,
                None,
                None,
                5,
                30 + quiz_number,
            )
            for quiz_number, question in enumerate(module["questions"], start=1)
        )
        for step_type, suffix, title, content, task, expected, rules, hint, xp, step_order in records:
            quiz_options = None
            quiz_answer = None
            if step_type == "quiz":
                question = next(
                    item for item in module["questions"]
                    if item["question"] == task
                )
                quiz_options = json.dumps(question["options"], ensure_ascii=False)
                quiz_answer = question["answer"]
            c.execute(
                """INSERT INTO lessons
                   (module_id, step_type, slug, title, content, task, expected, quiz_options,
                    quiz_answer, hint, xp_reward, sort_order, validation_rules)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                    module_id=excluded.module_id, step_type=excluded.step_type, title=excluded.title,
                    content=excluded.content, task=excluded.task, expected=excluded.expected,
                    quiz_options=excluded.quiz_options, quiz_answer=excluded.quiz_answer,
                    hint=excluded.hint, xp_reward=excluded.xp_reward, sort_order=excluded.sort_order,
                    validation_rules=excluded.validation_rules""",
                (module_id, step_type, f"m{module_id}-{suffix}", title, content, task, expected,
                 quiz_options, quiz_answer, hint, xp, module_id * 1000 + step_order, rules),
            )

    c.execute(
        """UPDATE lessons SET sort_order = module_id * 1000 + 40 + id
           WHERE sort_order IS NULL"""
    )

    conn.commit()
    conn.close()
    NotesBase.metadata.create_all(get_notes_engine())
    with get_notes_engine().begin() as notes_conn:
        notes_conn.execute(
            text(
                """INSERT OR IGNORE INTO courses (id, title, price)
                   VALUES (1, :title, :price)"""
            ),
            {"title": "Полный курс Python", "price": format(COURSE_PRICE, ".2f")},
        )
    conn = get_db()
    existing_users = conn.execute("SELECT id FROM users").fetchall()
    for user in existing_users:
        if not conn.execute(
            "SELECT 1 FROM cards WHERE user_id=? LIMIT 1", (user["id"],)
        ).fetchone():
            conn.execute(
                """INSERT INTO cards (user_id, number, balance, is_active)
                   VALUES (?, ?, '0.00', 1)""",
                (user["id"], generate_card_number(conn)),
            )
    conn.commit()
    conn.close()

class User(UserMixin):
    def __init__(self, id, username, email, password_hash, is_admin, xp, registered_at):
        self.id, self.username, self.email, self.password_hash = id, username, email, password_hash
        self.is_admin, self.xp, self.registered_at = is_admin, xp, registered_at

@login_manager.user_loader
def load_user(user_id):
    conn = get_db()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    conn.close()
    if row:
        return User(row["id"], row["username"], row["email"], row["password_hash"], row["is_admin"], row["xp"], row["registered_at"])
    return None

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash("Доступ запрещен. Требуются права администратора.", "error")
            return redirect(url_for('index'))
        return f(*args, **kwargs)
    return decorated_function

# ---------- РОУТЫ АВТОРИЗАЦИИ ----------
@app.route("/register", methods=["GET", "POST"])
def register():
    if request.method == "POST":
        username = request.form["username"].strip()
        email = request.form["email"].strip()
        password = request.form["password"]
        
        if len(username) < 3 or len(password) < 5:
            flash("Логин > 3 символов, пароль > 5 символов.", "error")
            return redirect(url_for("register"))
            
        conn = get_db()
        is_admin = 1 if conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 0 else 0
        
        try:
            user_cursor = conn.execute(
                "INSERT INTO users (username, email, password_hash, is_admin) VALUES (?, ?, ?, ?)",
                (username, email, generate_password_hash(password), is_admin),
            )
            conn.execute(
                """INSERT INTO cards (user_id, number, balance, is_active)
                   VALUES (?, ?, '0.00', 1)""",
                (user_cursor.lastrowid, generate_card_number(conn)),
            )
            conn.commit()
            flash("Аккаунт создан! Войдите.", "success")
            return redirect(url_for("login"))
        except sqlite3.IntegrityError:
            flash("Логин или Email уже заняты.", "error")
        finally: conn.close()
    return render_template("register.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        login_input = request.form["username"].strip()
        password = request.form["password"]
        conn = get_db()
        row = conn.execute("SELECT * FROM users WHERE username = ? OR email = ?", (login_input, login_input)).fetchone()
        conn.close()
        if row and check_password_hash(row["password_hash"], password):
            login_user(User(row["id"], row["username"], row["email"], row["password_hash"], row["is_admin"], row["xp"], row["registered_at"]))
            return redirect(url_for("index"))
        flash("Неверный логин/email или пароль.", "error")
    return render_template("login.html")

@app.route("/logout")
@login_required
def logout():
    logout_user()
    return redirect(url_for("index"))

# ---------- ВОССТАНОВЛЕНИЕ ПАРОЛЯ ----------
@app.route("/forgot_password", methods=["GET", "POST"])
def forgot_password():
    if request.method == "POST":
        email = request.form["email"].strip()
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE email = ?", (email,)).fetchone()
        conn.close()
        
        if user:
            reset_link = url_for('reset_password', user_id=user['id'], _external=True)
            print(f"\n{'='*50}\nССЫЛКА ДЛЯ СБРОСА ПАРОЛЯ: {reset_link}\n{'='*50}\n")
            flash("Ссылка для сброса пароля отправлена на ваш email (проверьте консоль терминала!).", "success")
            return redirect(url_for("login"))
        flash("Пользователь с таким email не найден.", "error")
    return render_template("forgot_password.html")

@app.route("/reset_password/<int:user_id>", methods=["GET", "POST"])
def reset_password(user_id):
    if request.method == "POST":
        password = request.form["password"]
        if len(password) < 5:
            flash("Пароль слишком короткий.", "error")
            return redirect(url_for("reset_password", user_id=user_id))
        conn = get_db()
        conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (generate_password_hash(password), user_id))
        conn.commit()
        conn.close()
        flash("Пароль успешно изменен! Войдите с новым паролем.", "success")
        return redirect(url_for("login"))
    return render_template("reset_password.html")

@app.route("/reset_progress", methods=["POST"])
@login_required
def reset_progress():
    conn = get_db()
    conn.execute("DELETE FROM progress WHERE user_id = ?", (current_user.id,))
    conn.execute("DELETE FROM achievements WHERE user_id = ?", (current_user.id,))
    conn.execute("UPDATE users SET xp = 0 WHERE id = ?", (current_user.id,))
    conn.commit()
    conn.close()
    flash("Прогресс обучения сброшен.", "success")
    return redirect(url_for("profile"))

# ---------- ОСНОВНЫЕ СТРАНИЦЫ ----------
@app.route("/")
def index():
    conn = get_db()
    modules_rows = conn.execute("SELECT * FROM modules ORDER BY id").fetchall()
    all_lessons = conn.execute("SELECT * FROM lessons ORDER BY sort_order, id").fetchall()
    completed_ids = set()
    if current_user.is_authenticated:
        rows = conn.execute("SELECT lesson_id FROM progress WHERE user_id = ? AND status='completed'", (current_user.id,)).fetchall()
        completed_ids = {r["lesson_id"] for r in rows}

    modules = []
    unlocked = True
    has_course_access = has_full_course_access(current_user.id) if current_user.is_authenticated else False
    for m in modules_rows:
        mod_dict = dict(m)
        mod_dict["lessons"] = []
        mod_dict["is_paywalled"] = current_user.is_authenticated and m["id"] > 2 and not has_course_access
        modules.append(mod_dict)

    modules_by_id = {module["id"]: module for module in modules}
    for lesson_row in all_lessons:
        l_dict = dict(lesson_row)
        l_dict["is_completed"] = lesson_row["id"] in completed_ids
        l_dict["is_locked"] = current_user.is_authenticated and (
            (lesson_row["module_id"] > 2 and not has_course_access)
            or (not unlocked and not l_dict["is_completed"])
        )
        modules_by_id[lesson_row["module_id"]]["lessons"].append(l_dict)
        if current_user.is_authenticated and not l_dict["is_completed"]:
            unlocked = False

    total_steps = len(all_lessons)
    done_count = len(completed_ids)
    is_graduated = (done_count == total_steps) if total_steps > 0 else False
    conn.close()
    return render_template(
        "index.html",
        modules=modules,
        total=total_steps,
        done=done_count,
        is_graduated=is_graduated,
        is_authenticated=current_user.is_authenticated,
        has_course_access=has_course_access,
    )

@app.route("/profile")
@login_required
def profile():
    conn = get_db()
    stats = conn.execute("SELECT COUNT(id) as solved, SUM(attempts) as attempts FROM progress WHERE user_id = ? AND status='completed'", (current_user.id,)).fetchone()
    achievements = conn.execute("SELECT title, icon FROM achievements WHERE user_id = ?", (current_user.id,)).fetchall()
    reg_date = datetime.strptime(current_user.registered_at, '%Y-%m-%d %H:%M:%S').strftime('%d.%m.%Y') if current_user.registered_at else ""
    conn.close()
    return render_template("profile.html", solved=stats["solved"] or 0, attempts=stats["attempts"] or 0, reg_date=reg_date, achievements=achievements)

@app.route("/wallet")
@login_required
def wallet():
    if db.session.scalar(
        select(Card.id).where(Card.user_id == current_user.id, Card.is_active.is_(True))
    ) is None:
        issue_card(current_user.id)
    cards = db.session.scalars(
        select(Card).where(Card.user_id == current_user.id).order_by(Card.id)
    ).all()
    active_cards = [card for card in cards if card.is_active]
    receipts = db.session.scalars(
        select(Receipt).where(Receipt.user_id == current_user.id).order_by(Receipt.created_at.desc()).limit(30)
    ).all()
    enrollment = db.session.scalar(
        select(Enrollment.id).where(
            Enrollment.user_id == current_user.id,
            Enrollment.course_id == 1,
        )
    )
    course = db.session.get(Course, 1)
    return render_template(
        "wallet.html",
        cards=cards,
        active_cards=active_cards,
        receipts=receipts,
        purchase=bool(enrollment),
        course=course,
    )

@app.route("/wallet/cards/add", methods=["GET", "POST"])
@login_required
def issue_wallet_card():
    if request.method == "POST":
        issue_card(current_user.id)
        flash("Новая карта выпущена.", "success")
    return redirect(url_for("wallet"))

@app.route("/wallet/top-up", methods=["POST"])
@login_required
def top_up_wallet():
    try:
        amount = Decimal(request.form.get("amount", "").strip())
    except (InvalidOperation, AttributeError):
        flash("Укажите сумму пополнения.", "error")
        return redirect(url_for("wallet"))
    if (
        not amount.is_finite()
        or not Decimal("100") <= amount <= Decimal("50000")
        or amount.as_tuple().exponent < -2
    ):
        flash("Сумма пополнения должна быть от 100 до 50 000 ₽.", "error")
        return redirect(url_for("wallet"))
    session = db.session
    card_id = request.form.get("card_id", type=int)
    card = session.scalar(
        select(Card).where(
            Card.id == card_id,
            Card.user_id == current_user.id,
            Card.is_active.is_(True),
        )
    )
    if card is None:
        session.rollback()
        flash("Выберите свою активную карту.", "error")
        return redirect(url_for("wallet"))
    now = current_utc_time()
    code = new_confirmation_code()
    challenge = TopUpChallenge(
        user_id=current_user.id,
        card_id=card.id,
        amount=amount,
        code_hash=confirmation_code_hash(code),
        created_at=now,
        last_sent_at=now,
        expires_at=now + timedelta(minutes=10),
    )
    session.add(challenge)
    try:
        session.commit()
    except SQLAlchemyError:
        session.rollback()
        app.logger.exception("Could not create top-up confirmation for user %s.", current_user.id)
        flash("Не удалось создать подтверждение. Попробуйте ещё раз.", "error")
        return redirect(url_for("wallet"))
    flask_session[f"topup_code_{challenge.id}"] = code
    return redirect(url_for("verify_top_up", challenge_id=challenge.id))

@app.route("/wallet/top-up/verify/<int:challenge_id>", methods=["GET", "POST"])
@login_required
def verify_top_up(challenge_id):
    session = db.session
    challenge = session.scalar(
        select(TopUpChallenge).where(
            TopUpChallenge.id == challenge_id,
            TopUpChallenge.user_id == current_user.id,
        )
    )
    if challenge is None:
        session.rollback()
        abort(404)
    now = current_utc_time()
    if challenge.completed_at is not None:
        session.rollback()
        flash("Это пополнение уже подтверждено.", "warning")
        return redirect(url_for("wallet"))
    if now >= challenge.expires_at:
        session.rollback()
        flash("Срок действия кода истёк. Начните пополнение заново.", "error")
        return redirect(url_for("wallet"))

    error_message = None
    if request.method == "POST":
        code = request.form.get("code", "").strip()
        if challenge.attempts >= 5:
            error_message = "Превышено число попыток. Запросите новый код после таймера."
        elif not re.fullmatch(r"\d{6}", code) or not hmac.compare_digest(
            challenge.code_hash,
            confirmation_code_hash(code),
        ):
            challenge.attempts += 1
            session.commit()
            error_message = "Неверный код подтверждения."
        else:
            card = session.scalar(
                select(Card).where(
                    Card.id == challenge.card_id,
                    Card.user_id == current_user.id,
                    Card.is_active.is_(True),
                )
            )
            if card is None:
                session.rollback()
                flash("Карта недоступна. Пополнение не выполнено.", "error")
                return redirect(url_for("wallet"))
            card.balance = (card.balance or Decimal("0.00")) + challenge.amount
            challenge.completed_at = now
            receipt_path = None
            try:
                receipt = create_receipt(
                    session,
                    "topup",
                    challenge.amount,
                    "Пополнение карты",
                )
                receipt_path = receipts_directory() / receipt.filename
                session.commit()
            except SQLAlchemyError:
                session.rollback()
                if receipt_path is not None:
                    receipt_path.unlink(missing_ok=True)
                app.logger.exception(
                    "Could not commit confirmed top-up %s for user %s.",
                    challenge_id,
                    current_user.id,
                )
                flash("Не удалось завершить пополнение из-за ошибки базы данных. Попробуйте подтвердить код ещё раз.", "error")
                return redirect(url_for("verify_top_up", challenge_id=challenge_id))
            except Exception:
                session.rollback()
                if receipt_path is not None:
                    receipt_path.unlink(missing_ok=True)
                raise
            flash("Пополнение подтверждено. Квитанция готова.", "push")
            return redirect(url_for("receipt_page", receipt_id=receipt.id))
    else:
        session.rollback()

    initial_code = flask_session.pop(f"topup_code_{challenge_id}", None)
    seconds_until_resend = max(
        0,
        int((challenge.last_sent_at + timedelta(seconds=30) - current_utc_time()).total_seconds()),
    )
    return render_template(
        "topup_verify.html",
        challenge=challenge,
        initial_code=initial_code,
        error_message=error_message,
        seconds_until_resend=seconds_until_resend,
    )

@app.route("/wallet/top-up/<int:challenge_id>/resend", methods=["POST"])
@login_required
def resend_top_up_code(challenge_id):
    session = db.session
    challenge = session.scalar(
        select(TopUpChallenge).where(
            TopUpChallenge.id == challenge_id,
            TopUpChallenge.user_id == current_user.id,
        )
    )
    if challenge is None:
        session.rollback()
        abort(404)
    now = current_utc_time()
    if challenge.completed_at is not None or now >= challenge.expires_at:
        session.rollback()
        return jsonify(error="Запрос подтверждения истёк."), 410
    resend_after = challenge.last_sent_at + timedelta(seconds=30) - now
    seconds_left = resend_after.total_seconds()
    if seconds_left > 0:
        session.rollback()
        return jsonify(
            error="Новый код пока недоступен.",
            retry_after=max(1, int(seconds_left) + 1),
        ), 429

    code = new_confirmation_code()
    challenge.code_hash = confirmation_code_hash(code)
    challenge.last_sent_at = now
    challenge.expires_at = now + timedelta(minutes=10)
    challenge.attempts = 0
    session.commit()
    return jsonify(code=code, retry_after=30)

@app.route("/courses")
def courses():
    all_courses = db.session.scalars(select(Course).order_by(Course.id)).all()
    enrolled_ids = set()
    if current_user.is_authenticated:
        enrolled_ids = set(db.session.scalars(
            select(Enrollment.course_id).where(Enrollment.user_id == current_user.id)
        ).all())
    return render_template("courses.html", courses=all_courses, enrolled_ids=enrolled_ids)

@app.route("/course/<int:course_id>/checkout", methods=["GET"])
@login_required
def course_checkout(course_id):
    course = db.session.get(Course, course_id)
    if course is None:
        abort(404)
    cards = db.session.scalars(
        select(Card).where(
            Card.user_id == current_user.id,
            Card.is_active.is_(True),
        ).order_by(Card.id)
    ).all()
    enrolled = db.session.scalar(
        select(Enrollment.id).where(
            Enrollment.user_id == current_user.id,
            Enrollment.course_id == course_id,
        )
    )
    return render_template(
        "checkout.html",
        course=course,
        cards=cards,
        enrolled=bool(enrolled),
    )

@app.route("/checkout/<int:course_id>", methods=["POST"])
@login_required
def purchase_course(course_id):
    session = db.session
    course = session.get(Course, course_id)
    if course is None:
        session.rollback()
        abort(404)
    if session.scalar(
        select(Enrollment.id).where(
            Enrollment.user_id == current_user.id,
            Enrollment.course_id == course_id,
        )
    ) is not None:
        session.rollback()
        flash("Вы уже записаны на этот курс.", "warning")
        return redirect(url_for("courses"))

    card_id = request.form.get("card_id", type=int)
    card = session.scalar(
        select(Card).where(
            Card.id == card_id,
            Card.user_id == current_user.id,
            Card.is_active.is_(True),
        )
    )
    if card is None:
        session.rollback()
        flash("Выберите свою активную карту.", "error")
        return redirect(url_for("course_checkout", course_id=course_id))
    price = Decimal(course.price)
    if card.balance < price:
        session.rollback()
        flash("На выбранной карте недостаточно средств.", "error")
        return redirect(url_for("course_checkout", course_id=course_id))

    card.balance -= price
    enrollment = Enrollment(user_id=current_user.id, course_id=course_id)
    session.add(enrollment)
    receipt_path = None
    try:
        receipt = create_receipt(
            session,
            "course",
            price,
            f"Запись на курс «{course.title}»",
        )
        receipt_path = receipts_directory() / receipt.filename
        session.commit()
    except Exception:
        session.rollback()
        if receipt_path is not None:
            receipt_path.unlink(missing_ok=True)
        raise
    flash(f"Оплата курса «{course.title}» прошла. Квитанция готова.", "push")
    return redirect(url_for("receipt_page", receipt_id=receipt.id))

@app.route("/checkout", methods=["GET", "POST"])
@login_required
def checkout():
    return redirect(url_for("course_checkout", course_id=1))

@app.route("/receipt")
@login_required
def legacy_receipt_route():
    return redirect(url_for("wallet"))

@app.route("/payments/<order_id>")
@login_required
def payment_status(order_id):
    conn = get_db()
    order = conn.execute(
        "SELECT * FROM provider_payments WHERE id=? AND user_id=?",
        (order_id, current_user.id),
    ).fetchone()
    conn.close()
    if not order:
        flash("Платёж не найден.", "error")
        return redirect(url_for("wallet"))
    return render_template("payment_status.html", payment=order)

@app.route("/note/<int:note_id>")
@login_required
def operation_note(note_id):
    note = db.session.get(OperationNote, note_id)
    if note is None:
        abort(404)
    if note.user_id != current_user.id:
        abort(403)
    return render_template("note.html", note=note)

@app.route("/receipt/<int:receipt_id>")
@login_required
def receipt_page(receipt_id):
    receipt = db.session.get(Receipt, receipt_id)
    if receipt is None:
        abort(404)
    if receipt.user_id != current_user.id:
        abort(403)
    return render_template("receipt.html", receipt=receipt)

@app.route("/receipt/<int:receipt_id>/download")
@login_required
def download_receipt(receipt_id):
    receipt = db.session.get(Receipt, receipt_id)
    if receipt is None:
        abort(404)
    if receipt.user_id != current_user.id:
        abort(403)
    return send_from_directory(
        receipts_directory(),
        receipt.filename,
        mimetype="application/pdf",
        as_attachment=True,
        download_name=receipt.filename,
    )

@app.route("/note/<int:note_id>/download")
@login_required
def download_operation_note(note_id):
    note = db.session.get(OperationNote, note_id)
    if note is None:
        abort(404)
    if note.user_id != current_user.id:
        abort(403)
    return send_from_directory(
        operation_notes_directory(),
        note.filename,
        as_attachment=True,
    )

@app.route("/payments/<order_id>/receipt.pdf")
@login_required
def payment_receipt_pdf(order_id):
    conn = get_db()
    order = conn.execute(
        """SELECT provider_payments.*, users.username, users.email
           FROM provider_payments JOIN users ON users.id=provider_payments.user_id
           WHERE provider_payments.id=? AND provider_payments.user_id=?
             AND provider_payments.purpose='course' AND provider_payments.status='succeeded'""",
        (order_id, current_user.id),
    ).fetchone()
    conn.close()
    if not order:
        flash("Документ доступен для завершённой операции по курсу.", "warning")
        return redirect(url_for("wallet"))
    pdf = create_payment_receipt_pdf(order)
    return send_file(
        BytesIO(pdf),
        mimetype="application/pdf",
        as_attachment=True,
        download_name=f"PyAcademy-receipt-{order_id[:8]}.pdf",
    )

def create_payment_receipt_pdf(order):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.utils import simpleSplit
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.pdfgen import canvas

    font_path = Path(app.root_path) / "fonts" / "DejaVuSans.ttf"
    if not font_path.is_file():
        raise FileNotFoundError(f"PDF font not found: {font_path}")
    if "DejaVu" not in pdfmetrics.getRegisteredFontNames():
        pdfmetrics.registerFont(TTFont("DejaVu", str(font_path)))
    formatted_amount = f"{Decimal(str(order['amount'])):,.2f}".replace(",", " ").replace(".", ",")
    lines = [
        "Операция внутреннего баланса",
        f"Пользователь: {order['username']}",
        f"Заказ: {order['id']}",
        f"Дата: {order['completed_at'] or order['created_at']}",
        "Услуга: полный курс Python",
        "Статус: Оплачено",
        f"Итого к оплате: {formatted_amount} ₽",
    ]

    buffer = BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    page_width, page_height = A4
    left = 58
    y = page_height - 70
    pdf.setTitle(f"Операция внутреннего баланса {order['id']}")
    pdf.setFont("DejaVu", 18)
    pdf.drawString(left, y, lines[0])
    y -= 36
    pdf.setFont("DejaVu", 10)
    for line in lines[1:]:
        for wrapped_line in simpleSplit(line, "DejaVu", 10, page_width - 2 * left):
            pdf.drawString(left, y, wrapped_line)
            y -= 22
    pdf.save()
    return buffer.getvalue()

@app.route("/certificate")
@login_required
def certificate():
    conn = get_db()
    total_steps = conn.execute("SELECT COUNT(*) FROM lessons").fetchone()[0]
    done_count = conn.execute("SELECT COUNT(*) FROM progress WHERE user_id = ? AND status='completed'", (current_user.id,)).fetchone()[0]
    conn.close()
    if done_count < total_steps:
        flash("Вы не прошли весь курс!", "error")
        return redirect(url_for("profile"))
    return render_template("certificate.html", date=datetime.now().strftime('%d.%m.%Y'))

@app.route("/leaderboard")
def leaderboard():
    conn = get_db()
    leaders = conn.execute("SELECT username, xp FROM users ORDER BY xp DESC LIMIT 10").fetchall()
    conn.close()
    return render_template("leaderboard.html", leaders=leaders)

@app.route("/sandbox")
def sandbox():
    return render_template("sandbox.html")

@app.route("/lesson/<slug>")
@login_required
def lesson(slug):
    conn = get_db()
    row = conn.execute("SELECT * FROM lessons WHERE slug = ?", (slug,)).fetchone()
    if not row:
        conn.close()
        return render_template("404.html"), 404

    if row["module_id"] > 2 and not has_full_course_access(current_user.id):
        conn.close()
        flash("Для следующих модулей оплатите доступ с баланса.", "warning")
        return redirect(url_for("checkout"))
        
    course_steps = conn.execute("SELECT id, slug, title, step_type FROM lessons ORDER BY sort_order, id").fetchall()
    current_position = next(i for i, step in enumerate(course_steps) if step["id"] == row["id"])
    previous_ids = [step["id"] for step in course_steps[:current_position]]
    prog = conn.execute("SELECT attempts, status FROM progress WHERE user_id = ? AND lesson_id = ?", (current_user.id, row["id"])).fetchone()
    if previous_ids and (not prog or prog["status"] != "completed"):
        placeholders = ",".join("?" for _ in previous_ids)
        completed_before = conn.execute(
            f"SELECT COUNT(*) FROM progress WHERE user_id = ? AND status='completed' AND lesson_id IN ({placeholders})",
            (current_user.id, *previous_ids),
        ).fetchone()[0]
        if completed_before != len(previous_ids):
            conn.close()
            flash("Сначала пройдите предыдущий шаг!", "warning")
            return redirect(url_for("index"))

    prev_step = course_steps[current_position - 1] if current_position > 0 else None
    next_step = course_steps[current_position + 1] if current_position < len(course_steps) - 1 else None

    comments = conn.execute("SELECT username, text, created_at FROM comments WHERE lesson_id = ? ORDER BY id DESC", (row["id"],)).fetchall()
    nav_rows = conn.execute(
        """SELECT lessons.id, lessons.module_id, lessons.slug, lessons.title, lessons.step_type,
                  CASE WHEN progress.status = 'completed' THEN 1 ELSE 0 END AS is_completed
           FROM lessons
           LEFT JOIN progress ON progress.lesson_id = lessons.id AND progress.user_id = ?
           ORDER BY lessons.sort_order, lessons.id""",
        (current_user.id,),
    ).fetchall()
    modules = conn.execute("SELECT id, title FROM modules ORDER BY id").fetchall()
    course_modules = [{"id": module["id"], "title": module["title"], "lessons": []} for module in modules]
    nav_modules_by_id = {module["id"]: module for module in course_modules}
    nav_unlocked = True
    has_course_access = has_full_course_access(current_user.id)
    for nav_row in nav_rows:
        nav_lesson = dict(nav_row)
        nav_lesson["is_locked"] = (
            (nav_lesson["module_id"] > 2 and not has_course_access)
            or (not nav_unlocked and not nav_lesson["is_completed"])
        )
        nav_lesson["is_active"] = nav_lesson["slug"] == slug
        nav_modules_by_id[nav_row["module_id"]]["lessons"].append(nav_lesson)
        if not nav_lesson["is_completed"]:
            nav_unlocked = False
    conn.close()
    
    data = dict(row)
    data["is_completed"] = prog["status"] == 'completed' if prog else False
    data["attempts"] = prog["attempts"] if prog else 0
    
    if data["step_type"] == 'theory':
        data["content_html"] = markdown.markdown(data["content"], extensions=['fenced_code', 'codehilite'])
    elif data["step_type"] == 'quiz':
        data["quiz_options"] = json.loads(data["quiz_options"])
        
    return render_template(
        "lesson.html",
        lesson=data,
        prev=prev_step,
        next=next_step,
        comments=comments,
        course_modules=course_modules,
    )

# ---------- АДМИНКА ----------
@app.route("/admin", methods=["GET", "POST"])
@admin_required
def admin():
    conn = get_db()
    if request.method == "POST":
        try:
            module_id = int(request.form.get("module_id", ""))
            xp = int(request.form.get("xp_reward", "5"))
        except ValueError:
            conn.close()
            flash("Выберите модуль и укажите целое число XP.", "error")
            return redirect(url_for("admin"))

        step_type = request.form.get("step_type", "")
        title = request.form.get("title", "").strip()
        content = request.form.get("content", "").strip()
        task = request.form.get("task", "").strip()
        hint = request.form.get("hint", "").strip()
        module_exists = conn.execute("SELECT 1 FROM modules WHERE id = ?", (module_id,)).fetchone()
        if not module_exists or step_type not in {"theory", "practice", "quiz"} or not title or not 1 <= xp <= 100:
            conn.close()
            flash("Проверьте модуль, тип урока, название и награду XP (1–100).", "error")
            return redirect(url_for("admin"))

        rules = None
        quiz_options = None
        quiz_answer = None
        if step_type == "theory" and not content:
            conn.close()
            flash("Для теоретического урока добавьте текст лекции.", "error")
            return redirect(url_for("admin"))
        if step_type == "practice":
            raw_rules = request.form.get("validation_rules", "")
            rule_list = [line.strip() for line in raw_rules.splitlines() if line.strip()]
            if not task or not rule_list:
                conn.close()
                flash("Для практики укажите задание и критерии проверки (по одному regex на строку).", "error")
                return redirect(url_for("admin"))
            try:
                for pattern in rule_list:
                    re.compile(pattern)
            except re.error:
                conn.close()
                flash("Один из шаблонов проверки содержит ошибку регулярного выражения.", "error")
                return redirect(url_for("admin"))
            rules = json.dumps(rule_list, ensure_ascii=False)
        if step_type == "quiz":
            options = [line.strip() for line in request.form.get("quiz_options", "").splitlines() if line.strip()]
            try:
                answer = int(request.form.get("quiz_answer", ""))
            except ValueError:
                answer = -1
            if not task or len(options) < 2 or not 0 <= answer < len(options):
                conn.close()
                flash("Для теста добавьте вопрос, варианты ответа и корректный номер верного варианта (начиная с 0).", "error")
                return redirect(url_for("admin"))
            quiz_options = json.dumps(options, ensure_ascii=False)
            quiz_answer = answer

        slug = f"custom-{int(datetime.now().timestamp() * 1000000)}"
        sort_order = conn.execute(
            "SELECT COALESCE(MAX(sort_order), ?) + 10 FROM lessons WHERE module_id = ?",
            (module_id * 1000, module_id),
        ).fetchone()[0]
        conn.execute(
            """INSERT INTO lessons
               (module_id, step_type, slug, title, content, task, quiz_options,
                quiz_answer, hint, xp_reward, sort_order, validation_rules)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (module_id, step_type, slug, title, content or None, task or None,
             quiz_options, quiz_answer, hint or None, xp, sort_order, rules),
        )
        conn.commit()
        conn.close()
        flash("Урок успешно создан!", "success")
        return redirect(url_for("admin"))
        
    modules = conn.execute("SELECT * FROM modules").fetchall()
    conn.close()
    return render_template("admin.html", modules=modules)

# ---------- API ----------
@app.route("/api/add_comment", methods=["POST"])
@login_required
def add_comment():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Некорректный формат запроса."}), 400
    try:
        lesson_id = int(data.get("lesson_id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Урок не найден."}), 400
    text = data.get("text")
    if not isinstance(text, str) or not 1 <= len(text.strip()) <= 1200:
        return jsonify({"ok": False, "error": "Комментарий должен содержать от 1 до 1200 символов."}), 400

    conn = get_db()
    lesson_row = conn.execute("SELECT id FROM lessons WHERE id = ?", (lesson_id,)).fetchone()
    if not lesson_row:
        conn.close()
        return jsonify({"ok": False, "error": "Урок не найден."}), 404
    cursor = conn.execute(
        "INSERT INTO comments (lesson_id, user_id, username, text) VALUES (?, ?, ?, ?)",
        (lesson_id, current_user.id, current_user.username, text.strip()),
    )
    conn.commit()
    comment = conn.execute(
        "SELECT username, text, created_at FROM comments WHERE id = ?",
        (cursor.lastrowid,),
    ).fetchone()
    conn.close()
    return jsonify({"ok": True, "comment": dict(comment)})

@app.route("/api/submit_step", methods=["POST"])
@login_required
def submit_step():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify({"ok": False, "error": "Некорректный формат запроса."}), 400
    slug = data.get("slug")
    answer = data.get("answer")
    if not isinstance(slug, str) or not isinstance(answer, str) or len(answer) > 10000:
        return jsonify({"ok": False, "error": "Укажите урок и ответ не длиннее 10000 символов."}), 400
    
    conn = get_db()
    row = conn.execute("SELECT * FROM lessons WHERE slug = ?", (slug,)).fetchone()
    if not row:
        conn.close()
        return jsonify({"ok": False, "error": "Урок не найден"}), 404

    if row["module_id"] > 2 and not has_full_course_access(current_user.id):
        conn.close()
        return jsonify({"ok": False, "error": "Оплатите доступ к курсу с баланса."}), 403

    preceding_lessons = conn.execute(
        """SELECT lessons.id FROM lessons
           WHERE lessons.sort_order < ?
           ORDER BY lessons.sort_order, lessons.id""",
        (row["sort_order"] or 0,),
    ).fetchall()
    if preceding_lessons:
        preceding_ids = [item["id"] for item in preceding_lessons]
        placeholders = ",".join("?" for _ in preceding_ids)
        completed_before = conn.execute(
            f"""SELECT COUNT(*) FROM progress
                WHERE user_id = ? AND status = 'completed' AND lesson_id IN ({placeholders})""",
            (current_user.id, *preceding_ids),
        ).fetchone()[0]
        if completed_before != len(preceding_ids):
            conn.close()
            return jsonify({"ok": False, "error": "Сначала пройдите предыдущие шаги курса."}), 403

    is_correct = False
    xp_reward = row["xp_reward"]

    if row["step_type"] == 'theory':
        is_correct = True
    elif row["step_type"] == 'practice':
        rules = json.loads(row["validation_rules"] or "[]")
        if not rules:
            conn.close()
            return jsonify({"ok": False, "error": "Для практического задания не настроены критерии проверки."}), 400
        is_correct = all(re.search(pattern, answer, re.MULTILINE) for pattern in rules)
    elif row["step_type"] == 'quiz':
        try:
            is_correct = int(answer) == row["quiz_answer"]
        except ValueError:
            is_correct = False

    prog = conn.execute("SELECT status, attempts FROM progress WHERE user_id = ? AND lesson_id = ?", (current_user.id, row["id"])).fetchone()
    
    if is_correct:
        if not prog or prog["status"] != 'completed':
            conn.execute("UPDATE users SET xp = xp + ? WHERE id = ?", (xp_reward, current_user.id))
            if prog:
                conn.execute("UPDATE progress SET status='completed' WHERE user_id = ? AND lesson_id = ?", (current_user.id, row["id"]))
            else:
                conn.execute("INSERT INTO progress (user_id, lesson_id, status, attempts) VALUES (?, ?, 'completed', 1)", (current_user.id, row["id"]))
    else:
        if prog:
            conn.execute("UPDATE progress SET attempts = attempts + 1 WHERE user_id = ? AND lesson_id = ?", (current_user.id, row["id"]))
        else:
            conn.execute("INSERT INTO progress (user_id, lesson_id, status, attempts) VALUES (?, ?, 'attempted', 1)", (current_user.id, row["id"]))
            
    conn.commit()
    new_attempts = conn.execute("SELECT attempts FROM progress WHERE user_id = ? AND lesson_id = ?", (current_user.id, row["id"])).fetchone()["attempts"]
    conn.close()

    return jsonify({"ok": is_correct, "hint": row["hint"] if (not is_correct and new_attempts >= 3) else None, "attempts": new_attempts, "xp": xp_reward if is_correct else 0})

@app.errorhandler(404)
def not_found(e):
    return render_template("404.html"), 404

if __name__ == "__main__":
    init_db()
    app.run(debug=True, port=5003)