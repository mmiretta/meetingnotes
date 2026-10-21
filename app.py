"""MeetingNotes: record or upload meetings, transcribe them with Whisper and
summarize them with a local LLM.

All configuration comes from environment variables (see .env.example).
"""

import hmac
import html
import ipaddress
import logging
import os
import secrets
import sqlite3
import sys
import threading
import traceback
import uuid
from datetime import datetime
from functools import wraps
from pathlib import Path
from urllib.parse import urlparse

import click
import markdown
import requests
from flask import (Flask, abort, g, jsonify, redirect, render_template, request,
                   send_file, session, url_for)
from flask_babel import Babel, gettext as _, lazy_gettext as _l
from markupsafe import Markup
from pydub import AudioSegment
from werkzeug.security import check_password_hash, generate_password_hash
from werkzeug.utils import secure_filename

__version__ = "1.0.0"

# ── Configuration ─────────────────────────────────────────────────────────────


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, default))
    except (TypeError, ValueError):
        return default


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR") or BASE_DIR / "data")
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEMO_MODE = env_bool("DEMO_MODE")

# Speech-to-text: a small HTTP service (see whisper_server.py) that accepts
# POST multipart "file" (+ optional "model", "language") and returns {"text": ...}.
WHISPER_URL = os.getenv("WHISPER_URL") or "http://localhost:9000/transcribe"
WHISPER_DEFAULT_MODEL = os.getenv("WHISPER_DEFAULT_MODEL") or "base"
WHISPER_TIMEOUT = env_int("WHISPER_TIMEOUT", 3600)

# Summaries: an Ollama server (default) or any OpenAI-compatible API
# (llama.cpp server, vLLM, LM Studio, LocalAI, hosted providers...).
LLM_PROVIDER = (os.getenv("LLM_PROVIDER") or "ollama").strip().lower()
OLLAMA_URL = (os.getenv("OLLAMA_URL") or "http://localhost:11434").rstrip("/")
OPENAI_BASE_URL = (os.getenv("OPENAI_BASE_URL") or "http://localhost:8000/v1").rstrip("/")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
SUMMARY_MODEL = os.getenv("SUMMARY_MODEL") or "llama3.2:3b"
LLM_TIMEOUT = env_int("LLM_TIMEOUT", 900)
LLM_TEMPERATURE = float(os.getenv("LLM_TEMPERATURE") or "0.7")
SUMMARY_LANGUAGES = [l for l in (s.strip() for s in (os.getenv("SUMMARY_LANGUAGES") or "en,es").split(","))
                     if l in ("en", "es")] or ["en"]

# How many transcription/summary jobs may run at the same time.
JOB_CONCURRENCY = max(1, env_int("JOB_CONCURRENCY", 1))
MAX_UPLOAD_MB = env_int("MAX_UPLOAD_MB", 500)
MIN_PASSWORD_LENGTH = 8

# Optional single sign-on through an authenticating reverse proxy
# (oauth2-proxy, Authelia, Authentik...). When TRUSTED_AUTH_HEADER is set and
# the request comes from one of TRUSTED_PROXY_CIDRS, the e-mail in that header
# opens a session for an EXISTING user. Accounts are never created this way.
TRUSTED_AUTH_HEADER = os.getenv("TRUSTED_AUTH_HEADER", "").strip()
TRUSTED_PROXY_CIDRS = [ipaddress.ip_network(c.strip(), strict=False)
                       for c in (os.getenv("TRUSTED_PROXY_CIDRS") or "127.0.0.1/32").split(",") if c.strip()]

LANGUAGES = {"en": "English", "es": "Español"}
DEFAULT_LANGUAGE = os.getenv("DEFAULT_LANGUAGE") or "en"
if DEFAULT_LANGUAGE not in LANGUAGES:
    DEFAULT_LANGUAGE = "en"

logging.basicConfig(level=(os.getenv("LOG_LEVEL") or "INFO").upper(),
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logger = logging.getLogger("meetingnotes")


def _load_secret_key() -> str:
    """SECRET_KEY from the environment, or a random one persisted in DATA_DIR."""
    key = os.getenv("SECRET_KEY", "").strip()
    if key:
        return key
    key_file = DATA_DIR / ".secret_key"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.write_text(key)
    os.chmod(key_file, 0o600)
    logger.warning("SECRET_KEY not set; generated one and stored it in %s", key_file)
    return key


app = Flask(__name__)
app.config.update(
    SECRET_KEY=_load_secret_key(),
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env_bool("SESSION_COOKIE_SECURE"),
    BABEL_DEFAULT_LOCALE=DEFAULT_LANGUAGE,
)


def get_locale():
    lang = request.cookies.get("lang")
    if lang in LANGUAGES:
        return lang
    return request.accept_languages.best_match(LANGUAGES.keys()) or DEFAULT_LANGUAGE


babel = Babel(app, locale_selector=get_locale)

WHISPER_MODELS = [
    ("tiny", _l("Tiny (fastest, least accurate)")),
    ("base", _l("Base (good balance)")),
    ("small", "Small"),
    ("medium", "Medium"),
    ("large-v2", "Large v2"),
    ("large-v3", _l("Large v3 (most accurate, slowest)")),
]

WHISPER_LANGUAGES = [
    ("auto", _l("Auto-detect")),
    ("en", "English"),
    ("es", "Español"),
    ("fr", "Français"),
    ("de", "Deutsch"),
    ("it", "Italiano"),
    ("pt", "Português"),
    ("zh", "中文"),
    ("ja", "日本語"),
    ("ko", "한국어"),
    ("ar", "العربية"),
    ("ru", "Русский"),
]

_job_slots = threading.BoundedSemaphore(JOB_CONCURRENCY)
http = requests.Session()

# ── Storage ───────────────────────────────────────────────────────────────────


def get_user_dir(user_id) -> Path:
    d = DATA_DIR / str(int(user_id))
    d.mkdir(parents=True, exist_ok=True)
    return d


def get_user_meetings_folder(user_id) -> Path:
    d = get_user_dir(user_id) / "meetings"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── Users database ────────────────────────────────────────────────────────────


def get_auth_db():
    conn = sqlite3.connect(str(DATA_DIR / "users.db"))
    conn.row_factory = sqlite3.Row
    return conn


def init_auth_db():
    conn = get_auth_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS users (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT    NOT NULL UNIQUE,
            password   TEXT    NOT NULL,
            is_admin   INTEGER NOT NULL DEFAULT 0,
            created_at TEXT    NOT NULL DEFAULT (datetime('now'))
        )
    """)
    # Columns added after the first release.
    for col, definition in [("is_admin", "INTEGER NOT NULL DEFAULT 0"),
                            ("created_at", "TEXT NOT NULL DEFAULT (datetime('now'))")]:
        try:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {definition}")
        except sqlite3.OperationalError:
            pass  # column already exists
    # The first user is always an admin.
    conn.execute("UPDATE users SET is_admin = 1 WHERE id = (SELECT MIN(id) FROM users) AND is_admin = 0")
    conn.commit()
    conn.close()


def create_user(email: str, password: str, is_admin: bool = False) -> str:
    conn = get_auth_db()
    cur = conn.execute("INSERT INTO users (email, password, is_admin) VALUES (?, ?, ?)",
                       (email.lower().strip(), generate_password_hash(password), int(is_admin)))
    conn.commit()
    user_id = str(cur.lastrowid)
    conn.close()
    init_user_db(user_id)
    return user_id


def count_users() -> int:
    conn = get_auth_db()
    n = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    conn.close()
    return n


def get_user_by_email(email: str):
    conn = get_auth_db()
    user = conn.execute("SELECT * FROM users WHERE email = ?", (email.lower().strip(),)).fetchone()
    conn.close()
    return user


def get_user_by_id(user_id):
    conn = get_auth_db()
    user = conn.execute("SELECT * FROM users WHERE id = ?", (int(user_id),)).fetchone()
    conn.close()
    return user


def list_users():
    conn = get_auth_db()
    rows = conn.execute("SELECT * FROM users ORDER BY id").fetchall()
    conn.close()
    return rows


def change_password(user_id, new_password: str):
    conn = get_auth_db()
    conn.execute("UPDATE users SET password = ? WHERE id = ?",
                 (generate_password_hash(new_password), int(user_id)))
    conn.commit()
    conn.close()


def delete_user(user_id):
    conn = get_auth_db()
    conn.execute("DELETE FROM users WHERE id = ?", (int(user_id),))
    conn.commit()
    conn.close()


def authenticate_user(email: str, password: str):
    user = get_user_by_email(email)
    if user and check_password_hash(user["password"], password):
        return user
    return None


# ── Per-user meetings database ────────────────────────────────────────────────


def get_db(user_id=None):
    if user_id is None:
        user_id = session.get("user_id")
    if not user_id:
        raise RuntimeError("No user in session")
    conn = sqlite3.connect(str(get_user_dir(user_id) / "meetings.db"))
    conn.row_factory = sqlite3.Row
    return conn


def init_user_db(user_id):
    """Create (or migrate) the meetings tables of one user."""
    conn = get_db(user_id)
    try:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meetings (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                date TEXT NOT NULL,
                audio_path TEXT,
                transcription TEXT,
                summary TEXT,
                model TEXT,
                summary_model TEXT,
                is_transcribing BOOLEAN DEFAULT 0
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS tags (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL
            )
        """)
        conn.execute("""
            CREATE TABLE IF NOT EXISTS meeting_tags (
                meeting_id TEXT,
                tag_id INTEGER,
                PRIMARY KEY (meeting_id, tag_id),
                FOREIGN KEY (meeting_id) REFERENCES meetings(id) ON DELETE CASCADE,
                FOREIGN KEY (tag_id) REFERENCES tags(id) ON DELETE CASCADE
            )
        """)
        cols = [r[1] for r in conn.execute("PRAGMA table_info(meetings)").fetchall()]
        for col, definition in [("summary_model", "TEXT"),
                                ("transcription_language", "TEXT DEFAULT 'auto'"),
                                ("summary_es", "TEXT"),
                                ("summary_en", "TEXT")]:
            if col not in cols:
                conn.execute(f"ALTER TABLE meetings ADD COLUMN {col} {definition}")
        conn.commit()
    finally:
        conn.close()


def reset_stale_jobs():
    """Jobs run in background threads; after a restart none of them is alive."""
    for db_path in DATA_DIR.glob("*/meetings.db"):
        try:
            conn = sqlite3.connect(str(db_path))
            n = conn.execute("UPDATE meetings SET is_transcribing = 0 WHERE is_transcribing = 1").rowcount
            conn.commit()
            conn.close()
            if n:
                logger.warning("Reset %d interrupted job(s) in %s", n, db_path.parent.name)
        except sqlite3.Error as e:
            logger.error("Could not reset jobs in %s: %s", db_path, e)


# ── Sessions, CSRF and access control ─────────────────────────────────────────


def _start_session(user):
    session.clear()
    session["user_id"] = str(user["id"])
    session["user_email"] = user["email"]
    session["is_admin"] = bool(user["is_admin"])
    init_user_db(str(user["id"]))


def csrf_token() -> str:
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_urlsafe(32)
    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = csrf_token


@app.before_request
def _trusted_header_login():
    if not TRUSTED_AUTH_HEADER or "user_id" in session:
        return
    email = (request.headers.get(TRUSTED_AUTH_HEADER) or "").strip().lower()
    if not email:
        return
    try:
        remote = ipaddress.ip_address(request.remote_addr or "")
    except ValueError:
        return
    if not any(remote in net for net in TRUSTED_PROXY_CIDRS):
        return
    user = get_user_by_email(email)
    if user:
        _start_session(user)


@app.before_request
def _csrf_protect():
    if request.method in ("POST", "PUT", "PATCH", "DELETE"):
        sent = request.headers.get("X-CSRF-Token") or request.form.get("csrf_token") or ""
        expected = session.get("_csrf", "")
        if not expected or not hmac.compare_digest(sent, expected):
            abort(400, description="Invalid or missing CSRF token")


@app.after_request
def _security_headers(resp):
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    return resp


def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.full_path))
        return f(*args, **kwargs)
    return decorated


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if "user_id" not in session:
            return redirect(url_for("login", next=request.full_path))
        if not session.get("is_admin"):
            abort(403)
        return f(*args, **kwargs)
    return decorated


def _safe_next(target: str) -> str:
    """Only allow redirects to local paths."""
    if not target:
        return url_for("index")
    parsed = urlparse(target)
    if parsed.scheme or parsed.netloc or not target.startswith("/") or target.startswith("//"):
        return url_for("index")
    return target


@app.context_processor
def inject_globals():
    return dict(
        current_user_email=session.get("user_email", ""),
        current_user_is_admin=session.get("is_admin", False),
        session_user_id=session.get("user_id", ""),
        lang=get_locale(),
        languages=LANGUAGES,
        demo_mode=DEMO_MODE,
        app_version=__version__,
    )


def render_md(text: str) -> Markup:
    """Render Markdown with raw HTML escaped (LLM output and user edits are untrusted)."""
    if not text:
        return Markup("")
    return Markup(markdown.markdown(html.escape(text, quote=False), extensions=["fenced_code", "tables"]))


# ── Auth routes ───────────────────────────────────────────────────────────────


@app.route("/healthz")
def healthz():
    return jsonify(status="ok", version=__version__)


@app.route("/favicon.ico")
def favicon():
    return app.send_static_file("favicon.svg")


@app.route("/login", methods=["GET", "POST"])
def login():
    next_url = request.values.get("next", "")
    if "user_id" in session:
        return redirect(_safe_next(next_url))
    error = None
    if request.method == "POST":
        user = authenticate_user(request.form.get("email", ""), request.form.get("password", ""))
        if user:
            _start_session(user)
            logger.info("User %s signed in", user["id"])
            return redirect(_safe_next(next_url))
        logger.info("Failed sign-in attempt from %s", request.remote_addr)
        error = _("Incorrect e-mail or password.")
    return render_template("login.html", error=error, next=next_url,
                           demo_password=(os.getenv("DEMO_PASSWORD") or "demo") if DEMO_MODE else None)


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/profile", methods=["GET", "POST"])
@login_required
def profile():
    error = success = None
    if request.method == "POST":
        current_pw = request.form.get("current_password", "")
        new_pw = request.form.get("new_password", "")
        confirm_pw = request.form.get("confirm_password", "")
        user = get_user_by_id(session["user_id"])
        if not check_password_hash(user["password"], current_pw):
            error = _("The current password is incorrect.")
        elif len(new_pw) < MIN_PASSWORD_LENGTH:
            error = _("The new password must be at least %(n)d characters long.", n=MIN_PASSWORD_LENGTH)
        elif new_pw != confirm_pw:
            error = _("The new passwords do not match.")
        else:
            change_password(session["user_id"], new_pw)
            success = _("Password updated.")
    return render_template("profile.html", error=error, success=success, min_pw=MIN_PASSWORD_LENGTH)


# ── Admin: user management ────────────────────────────────────────────────────


def _admin_page(error=None, success=None):
    return render_template("admin_users.html", users=list_users(), error=error, success=success,
                           min_pw=MIN_PASSWORD_LENGTH)


@app.route("/admin/users")
@admin_required
def admin_users():
    return _admin_page()


@app.route("/admin/users/create", methods=["POST"])
@admin_required
def admin_create_user():
    email = request.form.get("email", "").strip()
    password = request.form.get("password", "")
    is_admin = bool(request.form.get("is_admin"))
    if not email or not password:
        return _admin_page(error=_("E-mail and password are required."))
    if len(password) < MIN_PASSWORD_LENGTH:
        return _admin_page(error=_("The password must be at least %(n)d characters long.", n=MIN_PASSWORD_LENGTH))
    try:
        create_user(email, password, is_admin=is_admin)
    except sqlite3.IntegrityError:
        return _admin_page(error=_("User %(email)s already exists.", email=email))
    return _admin_page(success=_("User %(email)s created.", email=email))


@app.route("/admin/users/<int:user_id>/reset-password", methods=["POST"])
@admin_required
def admin_reset_password(user_id):
    new_pw = request.form.get("password", "")
    if len(new_pw) < MIN_PASSWORD_LENGTH:
        return _admin_page(error=_("The password must be at least %(n)d characters long.", n=MIN_PASSWORD_LENGTH))
    change_password(user_id, new_pw)
    return _admin_page(success=_("Password of user #%(id)d updated.", id=user_id))


@app.route("/admin/users/<int:user_id>/toggle-admin", methods=["POST"])
@admin_required
def admin_toggle_admin(user_id):
    if str(user_id) == session["user_id"]:
        return _admin_page(error=_("You cannot remove your own admin role."))
    conn = get_auth_db()
    conn.execute("UPDATE users SET is_admin = 1 - is_admin WHERE id = ?", (user_id,))
    conn.commit()
    conn.close()
    return redirect(url_for("admin_users"))


@app.route("/admin/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def admin_delete_user(user_id):
    if str(user_id) == session["user_id"]:
        return _admin_page(error=_("You cannot delete your own account."))
    delete_user(user_id)
    # The user's meetings stay on disk (DATA_DIR/<id>/) until removed by an operator.
    return redirect(url_for("admin_users"))


# ── Tags ──────────────────────────────────────────────────────────────────────


def get_meeting_tags(conn, meeting_id):
    rows = conn.execute("""
        SELECT t.name FROM tags t
        JOIN meeting_tags mt ON t.id = mt.tag_id
        WHERE mt.meeting_id = ? ORDER BY t.name
    """, (meeting_id,)).fetchall()
    return [r[0] for r in rows]


def set_meeting_tags(conn, meeting_id, tag_names):
    conn.execute("DELETE FROM meeting_tags WHERE meeting_id = ?", (meeting_id,))
    for name in dict.fromkeys(t.strip() for t in tag_names if t and t.strip()):
        conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
        tag_id = conn.execute("SELECT id FROM tags WHERE name = ?", (name,)).fetchone()[0]
        conn.execute("INSERT OR IGNORE INTO meeting_tags (meeting_id, tag_id) VALUES (?, ?)", (meeting_id, tag_id))
    conn.commit()


def get_all_tags(conn):
    return [r[0] for r in conn.execute("SELECT name FROM tags ORDER BY name").fetchall()]


# ── Meetings ──────────────────────────────────────────────────────────────────


def _load_meeting(conn, meeting_id):
    meeting = conn.execute("SELECT * FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    if meeting is None:
        conn.close()
        abort(404)
    return meeting


@app.route("/")
@login_required
def index():
    user_id = session["user_id"]
    tag_filter = request.args.get("tag")
    search_query = request.args.get("q", "").strip()
    conn = get_db(user_id)

    if search_query:
        like = f"%{search_query}%"
        meetings = conn.execute(
            "SELECT * FROM meetings WHERE title LIKE ? OR description LIKE ? OR transcription LIKE ?"
            " OR summary_en LIKE ? OR summary_es LIKE ? ORDER BY date DESC",
            (like, like, like, like, like)).fetchall()
    elif tag_filter:
        meetings = conn.execute("""
            SELECT DISTINCT m.* FROM meetings m
            JOIN meeting_tags mt ON m.id = mt.meeting_id
            JOIN tags t ON mt.tag_id = t.id
            WHERE t.name = ? ORDER BY m.date DESC
        """, (tag_filter,)).fetchall()
    else:
        meetings = conn.execute("SELECT * FROM meetings ORDER BY date DESC").fetchall()

    now = datetime.now()
    periods = {k: [] for k in ["today", "yesterday", "this_week", "last_week", "this_month", "older"]}
    for row in meetings:
        m = dict(row)
        m["tags"] = get_meeting_tags(conn, m["id"])
        m["word_count"] = len((m.get("transcription") or "").split())
        try:
            diff = (now.date() - datetime.strptime(m["date"], "%Y-%m-%d %H:%M:%S").date()).days
        except (TypeError, ValueError):
            diff = 9999
        if diff <= 0:
            periods["today"].append(m)
        elif diff == 1:
            periods["yesterday"].append(m)
        elif diff < 7:
            periods["this_week"].append(m)
        elif diff < 14:
            periods["last_week"].append(m)
        elif diff < 30:
            periods["this_month"].append(m)
        else:
            periods["older"].append(m)

    all_tags = get_all_tags(conn)
    conn.close()
    return render_template("index.html", meetings_by_period=periods, tags=all_tags,
                           selected_tag=tag_filter, search_query=search_query,
                           total_meetings=len(meetings))


def _run_pipeline(user_id, meeting_id, audio_path, model, language, summary_model):
    """Transcribe, then summarize. Runs in a background thread."""
    with _job_slots:
        try:
            logger.info("Transcribing meeting %s (model=%s, language=%s)", meeting_id, model, language)
            transcription = transcribe_audio(audio_path, model, language)
            conn = get_db(user_id)
            conn.execute("UPDATE meetings SET transcription = ? WHERE id = ?", (transcription, meeting_id))
            conn.commit()
            summarize_all_languages(transcription, summary_model, conn, meeting_id)
            conn.close()
            logger.info("Meeting %s processed", meeting_id)
        except Exception as e:
            logger.error("Processing of meeting %s failed: %s\n%s", meeting_id, e, traceback.format_exc())
        finally:
            try:
                conn = get_db(user_id)
                conn.execute("UPDATE meetings SET is_transcribing = 0 WHERE id = ?", (meeting_id,))
                conn.commit()
                conn.close()
            except sqlite3.Error:
                pass


def _start_pipeline(*args):
    threading.Thread(target=_run_pipeline, args=args, daemon=True).start()


@app.route("/meeting/new", methods=["GET", "POST"])
@login_required
def new_meeting():
    user_id = session["user_id"]
    if request.method == "POST":
        title = request.form.get("title", "").strip() or _("New Meeting")
        description = request.form.get("description", "")
        model = request.form.get("model") or WHISPER_DEFAULT_MODEL
        language = request.form.get("transcription_language", "auto")
        summary_model = request.form.get("summary_model") or SUMMARY_MODEL
        tags = [t.strip() for t in request.form.get("tags", "").split(",") if t.strip()]

        file = request.files.get("audio")
        if file is None:
            return jsonify(error=_("No audio file provided")), 400
        if not file.filename:
            return jsonify(error=_("No file selected")), 400

        meeting_id = str(uuid.uuid4())
        date = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        meeting_folder = get_user_meetings_folder(user_id) / meeting_id
        meeting_folder.mkdir()
        audio_path = meeting_folder / (secure_filename(file.filename) or "audio")
        try:
            file.save(audio_path)
        except OSError as e:
            logger.error("Could not save upload: %s", e)
            return jsonify(error=_("Error creating meeting")), 500

        conn = get_db(user_id)
        conn.execute(
            "INSERT INTO meetings (id, title, description, date, audio_path, model, transcription_language,"
            " summary_model, is_transcribing) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1)",
            (meeting_id, title, description, date, str(audio_path), model, language, summary_model))
        conn.commit()
        if tags:
            set_meeting_tags(conn, meeting_id, tags)
        conn.close()

        _start_pipeline(user_id, meeting_id, audio_path, model, language, summary_model)
        return jsonify(success=True, redirect=url_for("view_meeting", meeting_id=meeting_id))

    conn = get_db(user_id)
    all_tags = get_all_tags(conn)
    conn.close()
    return render_template("new_meeting.html",
                           summary_models=get_available_models(),
                           summary_default=SUMMARY_MODEL,
                           whisper_models=WHISPER_MODELS,
                           whisper_languages=WHISPER_LANGUAGES,
                           whisper_default=WHISPER_DEFAULT_MODEL,
                           max_upload_mb=MAX_UPLOAD_MB,
                           tags=all_tags)


AUDIO_MIME_TYPES = {".mp3": "audio/mpeg", ".wav": "audio/wav", ".m4a": "audio/mp4", ".mp4": "audio/mp4",
                    ".webm": "audio/webm", ".ogg": "audio/ogg", ".flac": "audio/flac"}


@app.route("/meeting/<meeting_id>/audio")
@login_required
def get_meeting_audio(meeting_id):
    conn = get_db()
    meeting = _load_meeting(conn, meeting_id)
    conn.close()
    if not meeting["audio_path"]:
        abort(404)
    audio_path = Path(meeting["audio_path"]).resolve()
    user_dir = get_user_dir(session["user_id"]).resolve()
    if user_dir not in audio_path.parents or not audio_path.exists():
        abort(404)
    return send_file(audio_path, mimetype=AUDIO_MIME_TYPES.get(audio_path.suffix.lower(), "application/octet-stream"),
                     as_attachment=False, download_name=audio_path.name, conditional=True)


@app.route("/meeting/<meeting_id>")
@login_required
def view_meeting(meeting_id):
    conn = get_db()
    meeting = _load_meeting(conn, meeting_id)
    m = dict(meeting)
    m["tags"] = get_meeting_tags(conn, meeting_id)
    conn.close()
    m["summary_es_html"] = render_md(m.get("summary_es") or m.get("summary"))
    m["summary_en_html"] = render_md(m.get("summary_en"))
    m["transcription_html"] = render_md(m.get("transcription"))
    summary_lang = get_locale() if get_locale() in ("en", "es") else "en"
    return render_template("meeting_detail.html", meeting=m,
                           summary_models=get_available_models(),
                           summary_default=m.get("summary_model") or SUMMARY_MODEL,
                           whisper_models=WHISPER_MODELS,
                           whisper_languages=WHISPER_LANGUAGES,
                           summary_lang=summary_lang)


@app.route("/meeting/<meeting_id>/delete", methods=["POST"])
@login_required
def delete_meeting(meeting_id):
    conn = get_db()
    meeting = _load_meeting(conn, meeting_id)
    if meeting["is_transcribing"]:
        conn.close()
        return jsonify(error=_("The meeting is still being processed")), 409
    if meeting["audio_path"]:
        audio = Path(meeting["audio_path"])
        if audio.exists():
            audio.unlink()
        try:
            audio.parent.rmdir()
        except OSError:
            pass
    conn.execute("DELETE FROM meeting_tags WHERE meeting_id = ?", (meeting_id,))
    conn.execute("DELETE FROM meetings WHERE id = ?", (meeting_id,))
    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/meeting/<meeting_id>/tags", methods=["POST"])
@login_required
def update_meeting_tags(meeting_id):
    data = request.get_json(silent=True) or {}
    tags = data.get("tags")
    if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
        return jsonify(error=_("Tags must be a list")), 400
    conn = get_db()
    _load_meeting(conn, meeting_id)
    set_meeting_tags(conn, meeting_id, tags)
    result = get_meeting_tags(conn, meeting_id)
    conn.close()
    return jsonify(success=True, tags=result)


@app.route("/tag/remove", methods=["POST"])
@login_required
def remove_tag():
    """Delete a tag everywhere (all meetings)."""
    data = request.get_json(silent=True) or {}
    tag = data.get("tag")
    if not tag:
        return jsonify(error=_("Invalid data")), 400
    conn = get_db()
    row = conn.execute("SELECT id FROM tags WHERE name = ?", (tag,)).fetchone()
    if not row:
        conn.close()
        return jsonify(error=_("Tag not found")), 404
    conn.execute("DELETE FROM meeting_tags WHERE tag_id = ?", (row["id"],))
    conn.execute("DELETE FROM tags WHERE id = ?", (row["id"],))
    conn.commit()
    conn.close()
    return jsonify(success=True)


@app.route("/meeting/<meeting_id>/transcribe", methods=["POST"])
@login_required
def transcribe_meeting(meeting_id):
    """Start the pipeline for a meeting that has audio but no transcription."""
    user_id = session["user_id"]
    conn = get_db(user_id)
    meeting = _load_meeting(conn, meeting_id)
    if meeting["is_transcribing"]:
        conn.close()
        return jsonify(error=_("Transcription already in progress")), 409
    if meeting["transcription"]:
        conn.close()
        return jsonify(error=_("The meeting is already transcribed")), 400
    if not meeting["audio_path"]:
        conn.close()
        return jsonify(error=_("No audio file available")), 400
    conn.execute("UPDATE meetings SET is_transcribing = 1 WHERE id = ?", (meeting_id,))
    conn.commit()
    conn.close()
    _start_pipeline(user_id, meeting_id, Path(meeting["audio_path"]), meeting["model"] or WHISPER_DEFAULT_MODEL,
                    meeting["transcription_language"] or "auto", meeting["summary_model"] or SUMMARY_MODEL)
    return jsonify(success=True)


@app.route("/meeting/<meeting_id>/reprocess_transcription", methods=["POST"])
@login_required
def reprocess_transcription(meeting_id):
    user_id = session["user_id"]
    conn = get_db(user_id)
    meeting = _load_meeting(conn, meeting_id)
    if meeting["is_transcribing"]:
        conn.close()
        return jsonify(error=_("Transcription already in progress")), 409
    if not meeting["audio_path"]:
        conn.close()
        return jsonify(error=_("No audio file available")), 400
    data = request.get_json(silent=True) or {}
    new_model = data.get("model") or meeting["model"] or WHISPER_DEFAULT_MODEL
    if new_model not in dict(WHISPER_MODELS):
        conn.close()
        return jsonify(error=_("Invalid model")), 400
    new_lang = data.get("language") or meeting["transcription_language"] or "auto"
    conn.execute("UPDATE meetings SET transcription = NULL, summary = NULL, summary_es = NULL, summary_en = NULL,"
                 " is_transcribing = 1, model = ?, transcription_language = ? WHERE id = ?",
                 (new_model, new_lang, meeting_id))
    conn.commit()
    conn.close()
    _start_pipeline(user_id, meeting_id, Path(meeting["audio_path"]), new_model, new_lang,
                    meeting["summary_model"] or SUMMARY_MODEL)
    return jsonify(success=True)


@app.route("/meeting/<meeting_id>/status")
@login_required
def check_transcription_status(meeting_id):
    conn = get_db()
    meeting = _load_meeting(conn, meeting_id)
    conn.close()
    if meeting["is_transcribing"]:
        return jsonify(status="transcribing", has_transcription=bool(meeting["transcription"]))
    if meeting["transcription"]:
        return jsonify(status="completed")
    return jsonify(status="error", error=_("Transcription failed. Check the server logs."))


def _run_summaries(user_id, meeting_id, model_name, langs):
    """Regenerate summaries in the background."""
    with _job_slots:
        try:
            conn = get_db(user_id)
            row = conn.execute("SELECT transcription FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
            if row and row["transcription"]:
                summarize_all_languages(row["transcription"], model_name, conn, meeting_id, langs)
            conn.close()
        except Exception as e:
            logger.error("Summaries of meeting %s failed: %s", meeting_id, e)
        finally:
            try:
                conn = get_db(user_id)
                conn.execute("UPDATE meetings SET is_transcribing = 0 WHERE id = ?", (meeting_id,))
                conn.commit()
                conn.close()
            except sqlite3.Error:
                pass


def _start_summaries(meeting_id, model_name, langs):
    user_id = session["user_id"]
    conn = get_db(user_id)
    meeting = _load_meeting(conn, meeting_id)
    if meeting["is_transcribing"]:
        conn.close()
        return jsonify(error=_("The meeting is still being processed")), 409
    if not meeting["transcription"]:
        conn.close()
        return jsonify(error=_("No transcription available")), 400
    model_name = model_name or meeting["summary_model"] or SUMMARY_MODEL
    conn.execute("UPDATE meetings SET is_transcribing = 1, summary_model = ? WHERE id = ?", (model_name, meeting_id))
    conn.commit()
    conn.close()
    threading.Thread(target=_run_summaries, args=(user_id, meeting_id, model_name, langs), daemon=True).start()
    return jsonify(success=True)


@app.route("/meeting/<meeting_id>/reprocess", methods=["POST"])
@login_required
def reprocess_summary(meeting_id):
    """Regenerate the summaries, optionally with another model."""
    data = request.get_json(silent=True) or {}
    model_name = data.get("model")
    if not model_name or model_name not in get_available_models():
        return jsonify(error=_("Invalid model")), 400
    return _start_summaries(meeting_id, model_name, SUMMARY_LANGUAGES)


@app.route("/meeting/<meeting_id>/summary/<lang>", methods=["POST"])
@login_required
def generate_summary_lang(meeting_id, lang):
    """Generate (or regenerate) the summary in one language."""
    if lang not in ("es", "en"):
        abort(404)
    return _start_summaries(meeting_id, None, [lang])


@app.route("/meeting/<meeting_id>/summary", methods=["POST"])
@login_required
def update_summary(meeting_id):
    """Save a manually edited summary for one language."""
    data = request.get_json(silent=True) or {}
    lang = data.get("lang", "es")
    if "summary" not in data or lang not in ("es", "en"):
        return jsonify(error=_("Invalid data")), 400
    conn = get_db()
    _load_meeting(conn, meeting_id)
    conn.execute(f"UPDATE meetings SET summary_{lang} = ? WHERE id = ?", (data["summary"], meeting_id))
    conn.commit()
    conn.close()
    return jsonify(success=True, formatted_summary=str(render_md(data["summary"])))


@app.route("/meeting/<meeting_id>/transcription", methods=["POST"])
@login_required
def update_transcription(meeting_id):
    """Save a manually edited transcription."""
    data = request.get_json(silent=True) or {}
    if "transcription" not in data:
        return jsonify(error=_("Invalid data")), 400
    conn = get_db()
    _load_meeting(conn, meeting_id)
    conn.execute("UPDATE meetings SET transcription = ? WHERE id = ?", (data["transcription"], meeting_id))
    conn.commit()
    conn.close()
    return jsonify(success=True, formatted_transcription=str(render_md(data["transcription"])))


@app.errorhandler(400)
@app.errorhandler(403)
@app.errorhandler(404)
@app.errorhandler(409)
@app.errorhandler(413)
def _http_error(e):
    if request.path.startswith("/meeting/") and request.method != "GET" or request.is_json:
        return jsonify(error=e.description if e.code != 413 else _("File too large")), e.code
    return render_template("error.html", code=e.code, message=e.description), e.code


# ── Transcription ─────────────────────────────────────────────────────────────


def transcribe_audio(audio_path, model, language=None) -> str:
    audio_path = Path(audio_path)
    send_path, tmp_wav = audio_path, None
    # Browser recordings (webm/ogg/mp4) are converted to WAV first.
    if audio_path.suffix.lower() in (".webm", ".ogg", ".mp4", ".m4a"):
        try:
            tmp_wav = audio_path.with_suffix(".tmp.wav")
            AudioSegment.from_file(str(audio_path)).export(str(tmp_wav), format="wav")
            send_path = tmp_wav
        except Exception as e:
            logger.warning("Audio conversion failed, sending the original file: %s", e)
            tmp_wav = None
    try:
        data = {}
        if model:
            data["model"] = model
        if language and language != "auto":
            data["language"] = language
        with open(send_path, "rb") as f:
            resp = http.post(WHISPER_URL, files={"file": (send_path.name, f)}, data=data, timeout=WHISPER_TIMEOUT)
        resp.raise_for_status()
        body = resp.json()
        text = (body.get("text") or body.get("transcription") or "").strip()
        logger.info("Transcription received (%d characters)", len(text))
        return text
    except requests.RequestException as e:
        if DEMO_MODE:
            import demo
            logger.warning("Speech-to-text service unavailable (%s); using the demo transcript", e)
            return demo.SAMPLE_TRANSCRIPT
        raise
    finally:
        if tmp_wav and tmp_wav.exists():
            tmp_wav.unlink()


# ── Summaries (LLM) ───────────────────────────────────────────────────────────

PROMPTS = {
    "en": ("Create a detailed summary of the following meeting transcript in ENGLISH. Structure the summary "
           "according to the main topics discussed and organize the information into logical sections. For each "
           "topic, summarize who was involved, what was discussed in detail, what decisions were made, what problems "
           "or challenges were identified, and what solutions were proposed or implemented. If specific names are "
           "included in the transcript, use them to accurately attribute the statements. Also document all important "
           "feedback and planned actions. Pay attention to details on time frames, responsibilities, open questions "
           "and any next steps. Conclude the summary with a brief overview of the key findings and next steps. "
           "Use Markdown.\n\nThis is the audio transcription:\n{transcription}\n"),
    "es": ("Crea un resumen detallado de la siguiente transcripción de reunión en ESPAÑOL. Estructura el resumen "
           "según los principales temas tratados y organiza la información en secciones lógicas. Para cada tema, "
           "resume quiénes participaron, qué se discutió en detalle, qué decisiones se tomaron, qué problemas o "
           "desafíos se identificaron y qué soluciones se propusieron o implementaron. Si se incluyen nombres "
           "específicos en la transcripción, úsalos para atribuir correctamente las declaraciones. Documenta también "
           "todos los comentarios importantes y las acciones planificadas. Presta atención a los detalles sobre "
           "plazos, responsabilidades, preguntas abiertas y próximos pasos. Concluye con una breve descripción "
           "general de los hallazgos clave y los próximos pasos. Usa Markdown.\n\n"
           "Esta es la transcripción del audio:\n{transcription}\n"),
}


def _openai_headers():
    return {"Authorization": f"Bearer {OPENAI_API_KEY}"} if OPENAI_API_KEY else {}


def get_available_models() -> dict:
    """Models offered in the UI, as {id: {"name": ...}}."""
    names = []
    try:
        if LLM_PROVIDER == "openai":
            resp = http.get(f"{OPENAI_BASE_URL}/models", headers=_openai_headers(), timeout=5)
            resp.raise_for_status()
            names = [m["id"] for m in resp.json().get("data", [])]
        else:
            resp = http.get(f"{OLLAMA_URL}/api/tags", timeout=5)
            resp.raise_for_status()
            names = [m["name"] for m in resp.json().get("models", [])]
    except (requests.RequestException, ValueError, KeyError) as e:
        logger.warning("Could not list LLM models: %s", e)
    if SUMMARY_MODEL not in names:
        names.insert(0, SUMMARY_MODEL)
    return {n: {"name": n} for n in names}


def llm_generate(prompt: str, model: str) -> str:
    if LLM_PROVIDER == "openai":
        resp = http.post(f"{OPENAI_BASE_URL}/chat/completions", headers=_openai_headers(), timeout=LLM_TIMEOUT,
                         json={"model": model, "temperature": LLM_TEMPERATURE,
                               "messages": [{"role": "user", "content": prompt}]})
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
    resp = http.post(f"{OLLAMA_URL}/api/generate", timeout=LLM_TIMEOUT,
                     json={"model": model, "prompt": prompt, "stream": False,
                           "options": {"temperature": LLM_TEMPERATURE, "top_p": 0.9}})
    resp.raise_for_status()
    return resp.json()["response"]


def summarize_conversation(transcription: str, model_name: str, lang: str = "en") -> str:
    prompt = PROMPTS.get(lang, PROMPTS["en"]).format(transcription=transcription)
    try:
        return llm_generate(prompt, model_name).strip()
    except (requests.RequestException, KeyError, ValueError) as e:
        if DEMO_MODE:
            import demo
            logger.warning("LLM unavailable (%s); using the demo summary", e)
            return demo.SAMPLE_SUMMARY[lang]
        raise


def summarize_all_languages(transcription, model_name, conn, meeting_id, langs=None):
    """Generate the summary in each language, saving each one as soon as it is ready."""
    if not transcription:
        return
    for lang in langs or SUMMARY_LANGUAGES:
        try:
            logger.info("Summarizing meeting %s (%s, model=%s)", meeting_id, lang, model_name)
            text = summarize_conversation(transcription, model_name, lang=lang)
        except Exception as e:
            logger.error("Summary (%s) of meeting %s failed: %s", lang, meeting_id, e)
            continue
        conn.execute(f"UPDATE meetings SET summary_{lang} = ? WHERE id = ?", (text, meeting_id))
        conn.commit()


# ── CLI and startup ───────────────────────────────────────────────────────────


@app.cli.command("create-user")
@click.argument("email")
@click.option("--admin", is_flag=True, help="Give the user the admin role.")
@click.password_option()
def create_user_command(email, admin, password):
    """Create a user account."""
    if len(password) < MIN_PASSWORD_LENGTH:
        raise click.ClickException(f"The password must be at least {MIN_PASSWORD_LENGTH} characters long.")
    try:
        uid = create_user(email, password, is_admin=admin)
    except sqlite3.IntegrityError:
        raise click.ClickException(f"User {email} already exists.")
    click.echo(f"Created user {email} (id {uid}{', admin' if admin else ''}).")


def init_app():
    init_auth_db()
    reset_stale_jobs()
    if DEMO_MODE:
        import demo
        demo.seed(sys.modules[__name__])
    elif count_users() == 0:
        email, password = os.getenv("ADMIN_EMAIL", "").strip(), os.getenv("ADMIN_PASSWORD", "")
        if email and len(password) >= MIN_PASSWORD_LENGTH:
            create_user(email, password, is_admin=True)
            logger.info("Created the initial admin account %s", email)
        else:
            logger.warning("No users yet. Set ADMIN_EMAIL and ADMIN_PASSWORD (min. %d characters) "
                           "or run: flask --app app create-user <email> --admin", MIN_PASSWORD_LENGTH)
    logger.info("MeetingNotes %s ready (data: %s, LLM: %s, demo: %s)", __version__, DATA_DIR, LLM_PROVIDER, DEMO_MODE)


init_app()

if __name__ == "__main__":
    # Development server. In production use gunicorn (see Dockerfile).
    app.run(host=os.getenv("HOST", "127.0.0.1"), port=env_int("PORT", 5002), threaded=True)
