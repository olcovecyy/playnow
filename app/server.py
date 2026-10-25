"""Private, resumable family video library. No media directory is publicly served."""

import contextlib
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import shutil
import sqlite3
import subprocess
import time
from urllib.parse import urlsplit
import uuid

from flask import Flask, g, jsonify, request, send_file, session
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix
from werkzeug.security import check_password_hash
from transcode import MediaError, migrate_videos, probe_media
from storage import MediaStorage, StorageConflict, StorageError, migrate_storage, validate_filename

CHUNK_SIZE = 8 * 1024 * 1024
MAX_FILE_SIZE = 100 * 1024**3
ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
SHARE_TOKEN_PATTERN = re.compile(r"^[A-Za-z0-9_-]{43,128}$")
REACTIONS = ("👍", "🤣", "🤪", "😭", "😮")
MINIMUM_PLAYBACK_SECONDS = 10
PLAYBACK_TICKET_SECONDS = 24 * 3600


def create_app(overrides=None):
    app = Flask(__name__, static_folder="static", static_url_path="/static")
    app.config.update(
        SECRET_KEY=os.environ.get("PLAYNOW_SECRET_KEY"),
        VIEWER_PASSWORD_HASH=os.environ.get("PLAYNOW_VIEWER_PASSWORD_HASH"),
        ADMIN_PASSWORD_HASH=os.environ.get("PLAYNOW_ADMIN_PASSWORD_HASH"),
        DATA_DIR=os.environ.get("PLAYNOW_DATA_DIR", str(Path(__file__).resolve().parent.parent / "data")),
        STORAGE_DIR=os.environ.get("PLAYNOW_STORAGE_DIR") or None,
        PUBLIC_URL=os.environ.get("PLAYNOW_PUBLIC_URL", "").rstrip("/"),
        SESSION_COOKIE_NAME="playnow_session",
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("PLAYNOW_COOKIE_SECURE", "0") == "1",
        SESSION_HOURS=int(os.environ.get("PLAYNOW_SESSION_HOURS", "168")),
        DISK_RESERVE_BYTES=int(os.environ.get("PLAYNOW_DISK_RESERVE_BYTES", str(10 * 1024**3))),
        TRUSTED_PROXY_COUNT=int(os.environ.get("PLAYNOW_TRUSTED_PROXY_COUNT", "0")),
        MAX_CONTENT_LENGTH=CHUNK_SIZE,
        PROCESS_MEDIA=os.environ.get("PLAYNOW_PROCESS_MEDIA", "1") == "1",
        TRANSCODE_ENABLED=os.environ.get("PLAYNOW_TRANSCODE_ENABLED", "0") == "1",
        LOGIN_LIMIT=5,
        LOGIN_WINDOW_SECONDS=15 * 60,
    )
    if overrides:
        app.config.update(overrides)
    for setting in ("SECRET_KEY", "VIEWER_PASSWORD_HASH", "ADMIN_PASSWORD_HASH"):
        if not app.config.get(setting):
            raise RuntimeError(f"Missing required PLAYNOW_{setting}")
    if len(app.config["SECRET_KEY"]) < 32:
        raise RuntimeError("PLAYNOW_SECRET_KEY must have at least 32 characters")
    for setting in ("VIEWER_PASSWORD_HASH", "ADMIN_PASSWORD_HASH"):
        if not app.config[setting].startswith("scrypt:"):
            raise RuntimeError(f"PLAYNOW_{setting} must use Werkzeug scrypt")
    public = urlsplit(app.config["PUBLIC_URL"])
    if app.config["PUBLIC_URL"] and (public.scheme not in ("http", "https") or not public.netloc or public.path not in ("", "/") or public.username or public.password or public.query or public.fragment):
        raise RuntimeError("PLAYNOW_PUBLIC_URL must be an HTTP(S) origin")
    if app.config["DISK_RESERVE_BYTES"] < 0 or app.config["SESSION_HOURS"] < 1:
        raise RuntimeError("Invalid storage/session configuration")
    if app.config["TRUSTED_PROXY_COUNT"]:
        count = app.config["TRUSTED_PROXY_COUNT"]
        if not 1 <= count <= 2:
            raise RuntimeError("Invalid trusted proxy count")
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=count, x_proto=count)

    data_dir = Path(app.config["DATA_DIR"]).resolve()
    data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    for directory in ("media", "posters", "uploads", "locks"):
        (data_dir / directory).mkdir(exist_ok=True, mode=0o700)
    storage = MediaStorage(data_dir, app.config["STORAGE_DIR"])
    database_path = data_dir / "catalog.sqlite3"

    def connect():
        db = sqlite3.connect(database_path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("PRAGMA busy_timeout=30000")
        return db

    with contextlib.closing(connect()) as initial:
        initial.execute("PRAGMA journal_mode=WAL")
        initial.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id TEXT PRIMARY KEY, role TEXT, csrf TEXT NOT NULL, expires REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS login_attempts (
                address TEXT NOT NULL, role TEXT NOT NULL, at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS login_attempt_lookup ON login_attempts(address,role,at);
            CREATE TABLE IF NOT EXISTS uploads (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT NOT NULL, size INTEGER NOT NULL, offset INTEGER NOT NULL DEFAULT 0,
                last_modified TEXT NOT NULL, created_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS videos (
                id TEXT PRIMARY KEY, filename TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT NOT NULL, size INTEGER NOT NULL, created_at REAL NOT NULL,
                mime TEXT NOT NULL, duration REAL, poster INTEGER NOT NULL DEFAULT 0
            );
        """)
        migrate_videos(initial)
        migrate_storage(initial)
        initial.execute("BEGIN IMMEDIATE")
        try:
            session_columns = {row[1] for row in initial.execute("PRAGMA table_info(sessions)")}
            if "share_id" not in session_columns:
                initial.execute("ALTER TABLE sessions ADD COLUMN share_id TEXT")
            upload_columns = {row[1] for row in initial.execute("PRAGMA table_info(uploads)")}
            if "content_fingerprint" not in upload_columns:
                initial.execute("ALTER TABLE uploads ADD COLUMN content_fingerprint TEXT")
            initial.execute("""CREATE TABLE IF NOT EXISTS video_shares (
                id TEXT PRIMARY KEY, video_id TEXT NOT NULL,
                token_hash TEXT NOT NULL UNIQUE, created_at REAL NOT NULL, revoked_at REAL,
                scope TEXT NOT NULL DEFAULT 'library'
            )""")
            share_columns = {row[1] for row in initial.execute("PRAGMA table_info(video_shares)")}
            if "scope" not in share_columns:
                initial.execute("ALTER TABLE video_shares ADD COLUMN scope TEXT NOT NULL DEFAULT 'library'")
            # The owner explicitly broadened every active link, including links
            # and browser sessions created before the library-sharing feature.
            initial.execute("UPDATE video_shares SET scope='library' WHERE revoked_at IS NULL AND scope='video'")
            initial.execute("CREATE INDEX IF NOT EXISTS video_shares_video ON video_shares(video_id,created_at)")
            initial.execute("CREATE INDEX IF NOT EXISTS sessions_share ON sessions(share_id)")
            initial.execute("""CREATE TABLE IF NOT EXISTS video_engagement (
                video_id TEXT PRIMARY KEY REFERENCES videos(id) ON DELETE CASCADE,
                views INTEGER NOT NULL DEFAULT 0 CHECK(views >= 0)
            )""")
            initial.execute("""CREATE TABLE IF NOT EXISTS video_reactions (
                video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                audience_id TEXT NOT NULL,
                reaction TEXT NOT NULL CHECK(reaction IN ('👍','🤣','🤪','😭','😮')),
                PRIMARY KEY(video_id,audience_id)
            )""")
            initial.execute("""CREATE TABLE IF NOT EXISTS playback_tickets (
                id TEXT PRIMARY KEY, video_id TEXT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
                audience_id TEXT NOT NULL, session_id TEXT NOT NULL,
                created_at REAL NOT NULL, expires REAL NOT NULL, qualified_at REAL
            )""")
            initial.execute("CREATE INDEX IF NOT EXISTS playback_tickets_expiry ON playback_tickets(expires)")
            initial.execute("CREATE INDEX IF NOT EXISTS playback_tickets_video ON playback_tickets(video_id)")
            initial.execute("DELETE FROM playback_tickets WHERE expires <= ?", (time.time(),))
            # A distinct stored role makes older images reject these sessions
            # instead of silently applying their previous single-video policy.
            initial.execute("""UPDATE sessions SET role='share_library_viewer'
                WHERE role IN ('viewer','share_viewer') AND share_id IN (
                    SELECT sh.id FROM video_shares sh JOIN videos v ON v.id=sh.video_id
                    WHERE sh.revoked_at IS NULL AND sh.scope='library'
                )""")
            initial.execute("COMMIT")
        except BaseException:
            initial.execute("ROLLBACK")
            raise
        storage.recover(initial)
    os.chmod(database_path, 0o600)

    def db():
        if "db" not in g:
            g.db = connect()
        return g.db

    @app.teardown_appcontext
    def close_db(_error):
        connection = g.pop("db", None)
        if connection is not None:
            connection.close()

    @contextlib.contextmanager
    def transaction():
        connection = db()
        connection.execute("BEGIN IMMEDIATE")
        try:
            yield connection
            connection.execute("COMMIT")
        except BaseException:
            connection.execute("ROLLBACK")
            raise

    def error(message, status=400, **extra):
        return jsonify(error=message, **extra), status

    def valid_id(value):
        return bool(ID_PATTERN.fullmatch(value))

    @contextlib.contextmanager
    def file_lock(identifier):
        # Only generated/validated identifiers reach this function.
        with (data_dir / "locks" / identifier).open("a+b") as lock:
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)

    class InvalidShare(Exception):
        pass

    def audience_id():
        identifier = session.get("audience_id")
        if not isinstance(identifier, str) or not re.fullmatch(r"[0-9a-f]{64}", identifier):
            old = session.get("sid")
            # Existing signed cookies acquire the same identity even if their
            # first requests arrive concurrently, before a new cookie is saved.
            if isinstance(old, str) and re.fullmatch(r"[0-9a-f]{64}", old):
                key = app.config["SECRET_KEY"]
                key = key.encode() if isinstance(key, str) else key
                identifier = hmac.new(key, b"playnow-audience:" + old.encode(), hashlib.sha256).hexdigest()
            else:
                identifier = secrets.token_hex(32)
            session["audience_id"] = identifier
        return identifier

    def new_session(role=None, share_id=None):
        old = session.get("sid")
        audience = audience_id()
        identifier, csrf = secrets.token_hex(32), secrets.token_urlsafe(32)
        expiry = time.time() + app.config["SESSION_HOURS"] * 3600
        share_scope = None
        with transaction() as connection:
            if share_id is not None:
                grant = connection.execute("SELECT sh.scope FROM video_shares sh JOIN videos v ON v.id=sh.video_id WHERE sh.id=? AND sh.revoked_at IS NULL AND sh.scope='library'", (share_id,)).fetchone()
                if not grant or role != "viewer":
                    raise InvalidShare()
                share_scope = grant["scope"]
            connection.execute("DELETE FROM sessions WHERE expires < ?", (time.time(),))
            if old:
                connection.execute("DELETE FROM sessions WHERE id = ?", (old,))
            stored_role = "share_library_viewer" if share_id is not None else role
            connection.execute("INSERT INTO sessions(id,role,csrf,expires,share_id) VALUES(?,?,?,?,?)", (identifier, stored_role, csrf, expiry, share_id))
        session.clear()
        session["sid"] = identifier
        session["audience_id"] = audience
        session.permanent = True
        g.auth = {"id": identifier, "role": role, "csrf": csrf, "expires": expiry, "share_id": share_id, "video_id": None, "share_scope": share_scope}
        return g.auth

    def auth():
        if "auth" not in g:
            identifier = session.get("sid", "")
            row = db().execute("""SELECT s.*,sh.scope AS share_scope FROM sessions s
                LEFT JOIN video_shares sh ON sh.id=s.share_id
                LEFT JOIN videos v ON v.id=sh.video_id
                WHERE s.id=? AND s.expires>?
                AND (s.share_id IS NULL OR (sh.revoked_at IS NULL AND v.id IS NOT NULL AND sh.scope='library'))""", (identifier, time.time())).fetchone()
            g.auth = dict(row) if row else None
            if g.auth:
                g.auth["video_id"] = None
            if g.auth and g.auth.get("share_id"):
                if g.auth["role"] != "share_library_viewer" or g.auth["share_scope"] != "library":
                    g.auth = None
                else:
                    g.auth["role"] = "viewer"
        return g.auth

    def session_json():
        current = auth() or new_session()
        audience_id()
        return {"role": current["role"], "csrf": current["csrf"], "video_id": current.get("video_id"), "share_scope": current.get("share_scope")}

    def json_object():
        payload = request.get_json(silent=True)
        return payload if isinstance(payload, dict) else {}

    @app.before_request
    def guard():
        if request.path == "/healthz":
            return None
        if public.netloc and request.host.lower() != public.netloc.lower():
            return error("Neplatná adresa serveru.", 400)
        storage.recover(db(), pending_only=True)
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            expected_origin = app.config["PUBLIC_URL"] or request.host_url.rstrip("/")
            supplied = request.headers.get("Origin")
            if supplied and supplied.rstrip("/") != expected_origin:
                return error("Požadavek musí pocházet z tohoto webu.", 403)
            if request.headers.get("Sec-Fetch-Site") == "cross-site":
                return error("Požadavek musí pocházet z tohoto webu.", 403)
            current = auth()
            token = request.headers.get("X-CSRF-Token", "")
            if not current or not token or not secrets.compare_digest(current["csrf"], token):
                return error("Platnost formuláře vypršela. Obnovte stránku.", 403)
        needs_viewer = request.path.startswith(("/api/videos", "/api/engagement", "/media/", "/poster/"))
        needs_admin = request.path.startswith("/api/uploads") or (request.path.startswith("/api/videos") and (request.method in ("POST", "PATCH", "DELETE") or "/shares" in request.path))
        if needs_viewer or needs_admin:
            current = auth()
            if not current or current["role"] not in ("viewer", "admin"):
                return error("Nejprve zadejte heslo.", 401)
            if needs_admin and current["role"] != "admin":
                return error("Tuto akci může provést pouze správce.", 403)

    @app.after_request
    def security_headers(response):
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; media-src 'self' blob:; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        if app.config["SESSION_COOKIE_SECURE"]:
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.errorhandler(HTTPException)
    def http_error(exception):
        if exception.code == 416:
            response = jsonify(error="Neplatný rozsah videa.")
            response.status_code = 416
            # Werkzeug sets the actual resource length on range errors.
            if getattr(exception, "length", None) is not None:
                response.headers["Content-Range"] = f"bytes */{exception.length}"
            return response
        messages = {404: "Požadovaná položka neexistuje.", 405: "Tato metoda není podporovaná.", 413: "Odeslaná část souboru je příliš velká.", 400: "Neplatný požadavek."}
        return error(messages.get(exception.code, "Požadavek se nepodařilo dokončit."), exception.code or 500)

    @app.errorhandler(Exception)
    def internal_error(exception):
        app.logger.error("Request failed: %s", type(exception).__name__)
        return error("Server požadavek nedokončil. Zkuste to znovu.", 500)

    @app.errorhandler(StorageConflict)
    def storage_conflict(exception):
        return error(str(exception), 409)

    @app.errorhandler(StorageError)
    def storage_unavailable(exception):
        return error(str(exception), 503)

    @app.get("/healthz")
    def health():
        db().execute("SELECT 1")
        return jsonify(status="ok")

    @app.get("/")
    @app.get("/admin")
    def index():
        return app.send_static_file("index.html")

    @app.get("/api/session")
    def get_session():
        return jsonify(session_json())

    @app.post("/api/login")
    def login():
        payload = json_object()
        role, password = payload.get("role"), payload.get("password")
        if role not in ("viewer", "admin") or not isinstance(password, str) or len(password) > 1024:
            return error("Zadejte platné heslo a typ přístupu.")
        address = request.remote_addr or "unknown"
        now, cutoff = time.time(), time.time() - app.config["LOGIN_WINDOW_SECONDS"]
        # Reserve each attempt before the costly hash check; concurrent requests cannot bypass the limit.
        with transaction() as connection:
            connection.execute("DELETE FROM login_attempts WHERE at < ?", (cutoff,))
            count = connection.execute("SELECT COUNT(*) FROM login_attempts WHERE address=? AND role=?", (address, role)).fetchone()[0]
            if count >= app.config["LOGIN_LIMIT"]:
                return error("Příliš mnoho pokusů. Zkuste to za 15 minut.", 429)
            connection.execute("INSERT INTO login_attempts(address,role,at) VALUES(?,?,?)", (address, role, now))
        stored = app.config["ADMIN_PASSWORD_HASH" if role == "admin" else "VIEWER_PASSWORD_HASH"]
        if not check_password_hash(stored, password):
            return error("Nesprávné heslo.", 401)
        db().execute("DELETE FROM login_attempts WHERE address=? AND role=?", (address, role))
        new_session(role)
        return jsonify(session_json())

    @app.post("/api/logout")
    def logout():
        new_session()
        return jsonify(session_json())

    @app.post("/api/share/redeem")
    def redeem_share():
        token = json_object().get("token")
        if not isinstance(token, str) or not SHARE_TOKEN_PATTERN.fullmatch(token):
            return error("Sdílecí odkaz není platný.")
        token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
        share = db().execute("SELECT sh.id,sh.video_id FROM video_shares sh JOIN videos v ON v.id=sh.video_id WHERE sh.token_hash=? AND sh.revoked_at IS NULL AND sh.scope='library'", (token_hash,)).fetchone()
        if not share:
            return error("Sdílecí odkaz už není dostupný.", 401)
        current = auth()
        # A valid shared link must never narrow an existing full account.
        if not current or current["role"] not in ("viewer", "admin") or current.get("share_id"):
            try:
                new_session("viewer", share_id=share["id"])
            except InvalidShare:
                return error("Sdílecí odkaz už není dostupný.", 401)
        return jsonify(session_json() | {"requested_video_id": share["video_id"]})

    def engagement_json(identifier, connection=None):
        connection = connection or db()
        summary = connection.execute("SELECT views FROM video_engagement WHERE video_id=?", (identifier,)).fetchone()
        counts = dict.fromkeys(REACTIONS, 0)
        for row in connection.execute("SELECT reaction,COUNT(*) AS total FROM video_reactions WHERE video_id=? GROUP BY reaction", (identifier,)):
            counts[row["reaction"]] = row["total"]
        mine = connection.execute("SELECT reaction FROM video_reactions WHERE video_id=? AND audience_id=?", (identifier, audience_id())).fetchone()
        return {"views": summary["views"] if summary else 0, "reactions": counts, "my_reaction": mine["reaction"] if mine else None}

    def video_json(row):
        item = dict(row)
        item.pop("source_sha256", None)
        item.pop("rename_to", None)
        item["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(item["created_at"]))
        item["poster_url"] = f"/poster/{item['id']}" if item.pop("poster", 0) else None
        item["stream_url"] = f"/media/{item['id']}"
        item["original_url"] = f"/media/{item['id']}?quality=original"
        ready = bool(item.get("compatible") and item.get("processing_status") == "ready" and storage.compatible(item["id"]).is_file())
        if item.get("processing_status") == "ready" and not ready:
            item["processing_status"] = "failed"
            item["processing_error"] = "Přehrávací kopie není dostupná. Originál je zachovaný a kopii lze připravit znovu."
        item["compatible_url"] = f"/media/{item['id']}?quality=compatible" if ready else None
        item["source_hdr"] = bool(item.get("source_hdr"))
        item["compatible"] = ready
        item["compatibility"] = {
            "status": item["processing_status"], "width": item.pop("compatible_width", None),
            "height": item.pop("compatible_height", None), "fps": item.pop("compatible_fps", None),
            "codec": item.pop("compatible_codec", None), "hdr": bool(item.pop("compatible_hdr", 0)),
            "error": item.pop("processing_error", None),
        }
        item["engagement"] = engagement_json(item["id"])
        return item

    def upload_json(row):
        return {key: row[key] for key in ("id", "filename", "title", "size", "offset", "content_fingerprint", "last_modified")} | {"status": "uploading", "chunk_size": CHUNK_SIZE}

    def safe_text(value, maximum):
        if not isinstance(value, str):
            return None
        value = value.strip()
        return value if len(value) <= maximum and not any(ord(c) < 32 and c not in "\n\r\t" for c in value) else None

    @app.get("/api/videos")
    def list_videos():
        current = auth()
        rows = db().execute("SELECT * FROM videos ORDER BY created_at DESC").fetchall()
        if current.get("share_id"):
            return jsonify(videos=[video_json(row) for row in rows], storage=None)
        disk = storage.disk_usage()
        return jsonify(videos=[video_json(row) for row in rows], storage={"free": disk.free, "total": disk.total})

    def engagement_video_exists(identifier, connection=None):
        return valid_id(identifier) and (connection or db()).execute("SELECT 1 FROM videos WHERE id=?", (identifier,)).fetchone()

    @app.get("/api/engagement/<identifier>")
    def get_engagement(identifier):
        if not engagement_video_exists(identifier):
            return error("Video neexistuje.", 404)
        return jsonify(engagement_json(identifier))

    @app.put("/api/engagement/<identifier>/reaction")
    def react(identifier):
        payload = json_object()
        reaction = payload.get("reaction")
        if "reaction" not in payload or (reaction is not None and reaction not in REACTIONS):
            return error("Vyberte platnou reakci.")
        audience = audience_id()
        with transaction() as connection:
            if not engagement_video_exists(identifier, connection):
                return error("Video neexistuje.", 404)
            if reaction is None:
                connection.execute("DELETE FROM video_reactions WHERE video_id=? AND audience_id=?", (identifier, audience))
            else:
                connection.execute("""INSERT INTO video_reactions(video_id,audience_id,reaction) VALUES(?,?,?)
                    ON CONFLICT(video_id,audience_id) DO UPDATE SET reaction=excluded.reaction""", (identifier, audience, reaction))
            result = engagement_json(identifier, connection)
        return jsonify(result)

    @app.post("/api/engagement/<identifier>/playback")
    def start_playback(identifier):
        now = time.time()
        ticket = uuid.uuid4().hex
        current = auth()
        with transaction() as connection:
            connection.execute("DELETE FROM playback_tickets WHERE expires <= ?", (now,))
            if not engagement_video_exists(identifier, connection):
                return error("Video neexistuje.", 404)
            connection.execute("""INSERT INTO playback_tickets(id,video_id,audience_id,session_id,created_at,expires)
                VALUES(?,?,?,?,?,?)""", (ticket, identifier, audience_id(), current["id"], now, min(now + PLAYBACK_TICKET_SECONDS, current["expires"])))
        return jsonify(playback_id=ticket, minimum_seconds=MINIMUM_PLAYBACK_SECONDS), 201

    @app.post("/api/engagement/<identifier>/playback/<ticket>")
    def qualify_playback(identifier, ticket):
        if not valid_id(ticket) or not valid_id(identifier):
            return error("Přehrávání už není dostupné.", 404)
        seconds = json_object().get("played_seconds")
        try:
            valid_seconds = not isinstance(seconds, bool) and isinstance(seconds, (int, float)) and math.isfinite(seconds) and seconds >= MINIMUM_PLAYBACK_SECONDS
        except (ValueError, OverflowError):
            valid_seconds = False
        if not valid_seconds:
            return error("Pro započítání je potřeba alespoň deset sekund přehrávání.")
        now = time.time()
        with transaction() as connection:
            connection.execute("DELETE FROM playback_tickets WHERE expires <= ?", (now,))
            row = connection.execute("""SELECT * FROM playback_tickets
                WHERE id=? AND video_id=? AND audience_id=? AND session_id=?""", (ticket, identifier, audience_id(), auth()["id"])).fetchone()
            if not row:
                return error("Přehrávání už není dostupné.", 404)
            if row["qualified_at"] is None:
                remaining = MINIMUM_PLAYBACK_SECONDS - (now - row["created_at"])
                if remaining > 0:
                    return jsonify(qualified=False, retry_after=max(0.001, round(remaining, 3)), engagement=engagement_json(identifier, connection))
                # The client supplies actual played time; the server separately
                # enforces elapsed time and serializes qualification/increment.
                connection.execute("UPDATE playback_tickets SET qualified_at=? WHERE id=? AND qualified_at IS NULL", (now, ticket))
                connection.execute("""INSERT INTO video_engagement(video_id,views) VALUES(?,1)
                    ON CONFLICT(video_id) DO UPDATE SET views=views+1""", (identifier,))
            result = engagement_json(identifier, connection)
        return jsonify(qualified=True, engagement=result)

    def share_json(row):
        return {
            "id": row["id"],
            "created_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["created_at"])),
            "revoked_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(row["revoked_at"])) if row["revoked_at"] is not None else None,
            "scope": row["scope"],
        }

    @app.get("/api/videos/<identifier>/shares")
    def list_shares(identifier):
        exists = db().execute("SELECT 1 FROM videos WHERE id=?", (identifier,)).fetchone() if valid_id(identifier) else None
        if not exists:
            return error("Video neexistuje.", 404)
        rows = db().execute("SELECT id,created_at,revoked_at,scope FROM video_shares WHERE video_id=? ORDER BY created_at DESC", (identifier,)).fetchall()
        return jsonify(shares=[share_json(row) for row in rows])

    @app.post("/api/videos/<identifier>/shares")
    def create_share(identifier):
        if not valid_id(identifier):
            return error("Video neexistuje.", 404)
        raw_token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(raw_token.encode("ascii")).hexdigest()
        share_id, created = uuid.uuid4().hex, time.time()
        with transaction() as connection:
            if not connection.execute("SELECT 1 FROM videos WHERE id=?", (identifier,)).fetchone():
                return error("Video neexistuje.", 404)
            connection.execute("INSERT INTO video_shares(id,video_id,token_hash,created_at,scope) VALUES(?,?,?,?,'library')", (share_id, identifier, token_hash, created))
        base = app.config["PUBLIC_URL"] or request.host_url.rstrip("/")
        return jsonify(share=share_json({"id": share_id, "created_at": created, "revoked_at": None, "scope": "library"}), url=f"{base}/?video={identifier}#access={raw_token}"), 201

    @app.delete("/api/videos/<identifier>/shares/<share_id>")
    def revoke_share(identifier, share_id):
        if not valid_id(identifier) or not valid_id(share_id):
            return error("Sdílení neexistuje.", 404)
        with transaction() as connection:
            row = connection.execute("SELECT sh.id FROM video_shares sh JOIN videos v ON v.id=sh.video_id WHERE sh.id=? AND sh.video_id=?", (share_id, identifier)).fetchone()
            if not row:
                return error("Sdílení neexistuje.", 404)
            connection.execute("UPDATE video_shares SET revoked_at=COALESCE(revoked_at,?) WHERE id=?", (time.time(), share_id))
            connection.execute("DELETE FROM sessions WHERE share_id=?", (share_id,))
        return jsonify(ok=True)

    @app.get("/media/<identifier>")
    def media(identifier):
        row = db().execute("SELECT * FROM videos WHERE id=?", (identifier,)).fetchone() if valid_id(identifier) else None
        if not row:
            return error("Video neexistuje.", 404)
        quality = request.args.get("quality", "auto")
        if quality not in ("auto", "original", "compatible"):
            return error("Neplatná kvalita přehrávání.")
        compatible_path = storage.compatible(identifier)
        ready = row["compatible"] and row["processing_status"] == "ready" and compatible_path.is_file()
        if quality == "compatible" and not ready:
            return error("Přehrávací verze ještě není připravená.", 409)
        use_compatible = ready and quality != "original"
        path = compatible_path if use_compatible else storage.original(identifier)
        if not path.is_file():
            return error("Soubor videa není dostupný.", 404)
        download_name = f"{Path(row['filename']).stem}.mp4" if use_compatible else row["filename"]
        response = send_file(path, mimetype="video/mp4" if use_compatible else row["mime"], download_name=download_name, conditional=True, etag=False, max_age=0)
        response.headers["Accept-Ranges"] = "bytes"
        return response

    @app.get("/poster/<identifier>")
    def poster(identifier):
        row = db().execute("SELECT poster FROM videos WHERE id=?", (identifier,)).fetchone() if valid_id(identifier) else None
        if not row or not row["poster"]:
            return error("Náhled není dostupný.", 404)
        return send_file(data_dir / "posters" / f"{identifier}.jpg", mimetype="image/jpeg", conditional=True, etag=False)

    def reconcile_upload(connection, row):
        if not row:
            return None
        part = storage.part(row["id"])
        completed = storage.recover_original(row["id"], row["filename"])
        if not part.exists() and not completed.exists():
            # Recover a missing empty file (for example after an interrupted creation).
            # If earlier bytes were lost, the offset returns to zero for a clean resend.
            with part.open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
        actual = part.stat().st_size if part.exists() else (completed.stat().st_size if completed.exists() else 0)
        if actual > row["size"]:
            raise RuntimeError("Upload size mismatch")
        if actual != row["offset"]:
            connection.execute("UPDATE uploads SET offset=? WHERE id=?", (actual, row["id"]))
            row = connection.execute("SELECT * FROM uploads WHERE id=?", (row["id"],)).fetchone()
        return row

    @app.get("/api/uploads")
    def list_uploads():
        rows = db().execute("SELECT * FROM uploads ORDER BY created_at DESC").fetchall()
        return jsonify(uploads=[upload_json(row) for row in rows])

    @app.post("/api/uploads")
    def create_upload():
        payload = json_object()
        raw_filename, size = payload.get("filename"), payload.get("size")
        if not isinstance(raw_filename, str) or not isinstance(size, int) or isinstance(size, bool) or not 1 <= size <= MAX_FILE_SIZE:
            return error("Vyberte soubor o velikosti nejvýše 100 GB.")
        filename = safe_text(raw_filename.replace("\\", "/").rsplit("/", 1)[-1], 255)
        title = safe_text(payload.get("title", ""), 180)
        description = safe_text(payload.get("description", ""), 3000)
        supplied_modified = payload.get("last_modified")
        try:
            valid_modified = isinstance(supplied_modified, (int, float)) and not isinstance(supplied_modified, bool) and math.isfinite(supplied_modified) and supplied_modified > 0
        except (TypeError, OverflowError):
            valid_modified = False
        last_modified = str(supplied_modified)[:100] if valid_modified else ""
        content_fingerprint = payload.get("content_fingerprint")
        if content_fingerprint is not None and (not isinstance(content_fingerprint, str) or not re.fullmatch(r"[0-9a-f]{64}", content_fingerprint)):
            return error("Kontrolní otisk souboru není platný.")
        if not filename or any(ord(char) < 32 for char in filename) or title is None or description is None:
            return error("Název nebo popis souboru není platný.")
        if storage.configured:
            try:
                validate_filename(filename)
            except StorageError as exception:
                return error(str(exception))
        title = title or Path(filename).stem[:180] or "Video"
        with transaction() as connection:
            existing = None
            if content_fingerprint is not None:
                existing = connection.execute("SELECT * FROM uploads WHERE filename=? AND size=? AND content_fingerprint=? ORDER BY created_at LIMIT 1", (filename, size, content_fingerprint)).fetchone()
            elif valid_modified:
                existing = connection.execute("SELECT * FROM uploads WHERE filename=? AND size=? AND last_modified=? AND content_fingerprint IS NULL ORDER BY created_at LIMIT 1", (filename, size, last_modified)).fetchone()
            if existing:
                return jsonify(upload_json(existing))
            free = storage.disk_usage().free
            reserved = connection.execute("SELECT COALESCE(SUM(size-offset),0) FROM uploads").fetchone()[0]
            if free - reserved - size < app.config["DISK_RESERVE_BYTES"]:
                return error("Na disku není dost místa včetně bezpečné rezervy.", 507)
            identifier = uuid.uuid4().hex
            with storage.part(identifier).open("xb") as output:
                os.fchmod(output.fileno(), 0o600)
            connection.execute("INSERT INTO uploads(id,filename,title,description,size,offset,last_modified,created_at,content_fingerprint) VALUES(?,?,?,?,?,0,?,?,?)", (identifier, filename, title, description, size, last_modified, time.time(), content_fingerprint))
            row = connection.execute("SELECT * FROM uploads WHERE id=?", (identifier,)).fetchone()
        return jsonify(upload_json(row)), 201

    @app.get("/api/uploads/<identifier>")
    def get_upload(identifier):
        if not valid_id(identifier):
            return error("Nahrávání neexistuje.", 404)
        with file_lock(identifier), transaction() as connection:
            row = reconcile_upload(connection, connection.execute("SELECT * FROM uploads WHERE id=?", (identifier,)).fetchone())
            if not row:
                return error("Nahrávání neexistuje.", 404)
            return jsonify(upload_json(row))

    @app.put("/api/uploads/<identifier>")
    def put_chunk(identifier):
        if not valid_id(identifier):
            return error("Nahrávání neexistuje.", 404)
        if request.mimetype != "application/octet-stream":
            return error("Část souboru musí mít typ application/octet-stream.", 415)
        offset_text = request.headers.get("Upload-Offset", "")
        if not offset_text.isdigit() or len(offset_text) > 20:
            return error("Chybí platná pozice v souboru.")
        offset = int(offset_text)
        content = request.get_data(cache=False)
        if not content or len(content) > CHUNK_SIZE:
            return error("Část souboru musí mít velikost 1 B až 8 MB.", 413)
        with file_lock(identifier), transaction() as connection:
            row = reconcile_upload(connection, connection.execute("SELECT * FROM uploads WHERE id=?", (identifier,)).fetchone())
            if not row:
                return error("Nahrávání neexistuje.", 404)
            if offset != row["offset"]:
                return error("Pozice se změnila. Nahrávání lze navázat.", 409, offset=row["offset"])
            if offset + len(content) > row["size"]:
                return error("Část přesahuje deklarovanou velikost souboru.")
            if storage.disk_usage().free - len(content) < app.config["DISK_RESERVE_BYTES"]:
                return error("Nahrávání bylo pozastaveno kvůli nedostatku místa.", 507)
            path = storage.part(identifier)
            if not path.is_file():
                return error("Soubor byl již připraven k publikování. Dokončete nahrávání.", 409, offset=row["offset"])
            with path.open("r+b") as output:
                output.seek(offset)
                written = output.write(content)
                if written != len(content):
                    raise RuntimeError("Incomplete chunk write")
                output.flush()
                os.fsync(output.fileno())
            new_offset = offset + len(content)
            connection.execute("UPDATE uploads SET offset=? WHERE id=?", (new_offset, identifier))
        return jsonify(id=identifier, offset=new_offset, size=row["size"])

    def inspect_media(identifier, path):
        metadata, poster_created = {}, False
        if not app.config["PROCESS_MEDIA"]:
            return metadata, poster_created
        try:
            metadata = probe_media(path, timeout=8)
        except MediaError:
            pass
        ffmpeg = shutil.which("ffmpeg")
        if ffmpeg:
            temporary = data_dir / "posters" / f"{identifier}.tmp.jpg"
            target = data_dir / "posters" / f"{identifier}.jpg"
            try:
                position = min(5, (metadata.get("duration") or 0) / 3)
                subprocess.run([ffmpeg, "-nostdin", "-v", "error", "-threads", "1", "-protocol_whitelist", "file,pipe", "-format_whitelist", "mov,matroska,webm,avi,mpeg,mpegts,ogg", "-ss", str(position), "-i", str(path), "-frames:v", "1", "-vf", "scale=640:-2", "-threads", "1", "-filter_threads", "1", "-q:v", "4", "-y", str(temporary)], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15, check=True)
                if temporary.is_file() and temporary.stat().st_size > 0:
                    os.replace(temporary, target)
                    poster_created = True
            except (subprocess.SubprocessError, OSError):
                pass
            finally:
                temporary.unlink(missing_ok=True)
        return metadata, poster_created

    def publish_media_record(identifier, row, source_sha256=None):
        """Publish an already verified, generated media file from web upload or local import."""
        target = storage.original(identifier)
        if not valid_id(identifier) or not target.is_file() or target.stat().st_size != row["size"]:
            raise RuntimeError("Invalid prepared media file")
        metadata, has_poster = inspect_media(identifier, target)
        mime = {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm", ".mov": "video/quicktime", ".mkv": "video/x-matroska", ".avi": "video/x-msvideo"}.get(Path(row["filename"]).suffix.lower(), "application/octet-stream")
        with transaction() as connection:
            connection.execute("INSERT INTO videos(id,filename,title,description,size,created_at,mime,duration,poster,processing_status,source_width,source_height,source_fps,source_codec,source_hdr,source_sha256) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (identifier, row["filename"], row["title"], row["description"], row["size"], time.time(), mime, metadata.get("duration"), int(has_poster), "queued" if app.config["TRANSCODE_ENABLED"] else "none", metadata.get("width"), metadata.get("height"), metadata.get("fps"), metadata.get("codec"), int(bool(metadata.get("hdr"))), source_sha256))
            connection.execute("DELETE FROM uploads WHERE id=?", (identifier,))
            published = connection.execute("SELECT * FROM videos WHERE id=?", (identifier,)).fetchone()
        return published

    @app.post("/api/uploads/<identifier>/complete")
    def complete_upload(identifier):
        if not valid_id(identifier):
            return error("Nahrávání neexistuje.", 404)
        with file_lock(identifier):
            with transaction() as connection:
                published = connection.execute("SELECT * FROM videos WHERE id=?", (identifier,)).fetchone()
                if published:
                    return jsonify(video=video_json(published))
                row = reconcile_upload(connection, connection.execute("SELECT * FROM uploads WHERE id=?", (identifier,)).fetchone())
                if not row:
                    return error("Nahrávání neexistuje.", 404)
                if row["offset"] != row["size"]:
                    return error("Soubor ještě není nahraný celý.", 409, offset=row["offset"])
                target = storage.publish_original(identifier, row["filename"])
            # Metadata work stays outside the SQLite write transaction.
            published = publish_media_record(identifier, row)
        return jsonify(video=video_json(published)), 201

    @app.delete("/api/uploads/<identifier>")
    def delete_upload(identifier):
        if not valid_id(identifier):
            return error("Nahrávání neexistuje.", 404)
        with file_lock(identifier), transaction() as connection:
            row = connection.execute("SELECT * FROM uploads WHERE id=?", (identifier,)).fetchone()
            if not row:
                return error("Nahrávání neexistuje.", 404)
            connection.execute("DELETE FROM uploads WHERE id=?", (identifier,))
            storage.part(identifier).unlink(missing_ok=True)
            # A previous process may have crashed after its atomic rename.
            storage.remove_original(identifier, row["filename"])
            (data_dir / "posters" / f"{identifier}.jpg").unlink(missing_ok=True)
        return jsonify(ok=True)

    @app.patch("/api/videos/<identifier>")
    def edit_video(identifier):
        payload = json_object()
        existing = db().execute("SELECT * FROM videos WHERE id=?", (identifier,)).fetchone() if valid_id(identifier) else None
        if not existing:
            return error("Video neexistuje.", 404)
        title = safe_text(payload.get("title", existing["title"]), 180)
        description = safe_text(payload.get("description", existing["description"]), 3000)
        if not title or description is None:
            return error("Vyplňte název do 180 znaků a popis do 3000 znaků.")
        if "filename" in payload:
            try:
                validate_filename(payload["filename"], existing["filename"])
                storage.rename(db(), identifier, payload["filename"])
            except StorageConflict:
                raise
            except StorageError as exception:
                return error(str(exception))
        with transaction() as connection:
            result = connection.execute("UPDATE videos SET title=?,description=? WHERE id=?", (title, description, identifier))
            if result.rowcount != 1:
                return error("Video neexistuje.", 404)
            row = connection.execute("SELECT * FROM videos WHERE id=?", (identifier,)).fetchone()
        return jsonify(video=video_json(row))

    @app.post("/api/videos/<identifier>/prepare")
    def prepare_video(identifier):
        if not valid_id(identifier):
            return error("Video neexistuje.", 404)
        if not app.config["TRANSCODE_ENABLED"]:
            return error("Příprava přehrávací verze není na serveru zapnutá.", 503)
        with file_lock(identifier), transaction() as connection:
            row = connection.execute("SELECT * FROM videos WHERE id=?", (identifier,)).fetchone()
            if not row:
                return error("Video neexistuje.", 404)
            if row["processing_status"] == "processing":
                return error("Přehrávací verze se právě připravuje.", 409)
            ready = row["compatible"] and row["processing_status"] == "ready" and storage.compatible(identifier).is_file()
            if not ready and row["processing_status"] != "queued":
                connection.execute("UPDATE videos SET processing_status='queued',processing_error=NULL,compatible=0 WHERE id=?", (identifier,))
                row = connection.execute("SELECT * FROM videos WHERE id=?", (identifier,)).fetchone()
        return jsonify(video=video_json(row))

    @app.delete("/api/videos/<identifier>")
    def delete_video(identifier):
        if not valid_id(identifier):
            return error("Video neexistuje.", 404)
        with file_lock(identifier), transaction() as connection:
            existing = connection.execute("SELECT * FROM videos WHERE id=?", (identifier,)).fetchone()
            if not existing:
                return error("Video neexistuje.", 404)
            connection.execute("DELETE FROM sessions WHERE share_id IN (SELECT id FROM video_shares WHERE video_id=?)", (identifier,))
            connection.execute("UPDATE video_shares SET revoked_at=COALESCE(revoked_at,?) WHERE video_id=?", (time.time(), identifier))
            result = connection.execute("DELETE FROM videos WHERE id=?", (identifier,))
            if result.rowcount != 1:
                return error("Video neexistuje.", 404)
            storage.remove_original(identifier, existing["filename"], existing["rename_to"])
            storage.compatible(identifier).unlink(missing_ok=True)
            (data_dir / "posters" / f"{identifier}.jpg").unlink(missing_ok=True)
        return jsonify(ok=True)

    app.extensions["playnow"] = {
        "data_dir": data_dir, "db": db, "file_lock": file_lock,
        "publish_media_record": publish_media_record,
        "storage": storage,
    }
    return app


app = create_app()

if __name__ == "__main__":
    app.run(host="127.0.0.1", port=8080)
