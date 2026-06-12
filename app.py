import json
import os
import re
import sqlite3
import tempfile
import threading
import uuid
from datetime import datetime, timedelta
from functools import wraps
from pathlib import Path

from flask import Flask, g, jsonify, request, send_from_directory, session

from google.oauth2 import id_token
from google.auth.transport import requests as google_requests

BASE_DIR = Path(__file__).parent


def _load_dotenv(path):
    if not path.exists():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, val = line.partition("=")
        os.environ.setdefault(key.strip(), val.strip())


_load_dotenv(BASE_DIR / ".env")

DATA_DIR = Path(os.environ.get("DATA_DIR", BASE_DIR / "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_FILE = DATA_DIR / "plate.db"

ASR_MODEL_SIZE = os.environ.get("ASR_MODEL", "base.en")
GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")

MEALS = ["Breakfast", "Lunch", "Dinner", "Snacks"]

WORDS = {
    "zero": 0, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "thirteen": 13, "fourteen": 14, "fifteen": 15, "sixteen": 16, "seventeen": 17,
    "eighteen": 18, "nineteen": 19, "twenty": 20, "thirty": 30, "forty": 40,
    "fifty": 50, "sixty": 60, "seventy": 70, "eighty": 80, "ninety": 90, "hundred": 100,
}

UNIT_WORDS = {"g", "gram", "grams", "kg", "ml", "l", "piece", "pieces", "of"}


def _now():
    return datetime.utcnow().isoformat() + "Z"


def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    conn = get_db()
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                id         TEXT PRIMARY KEY,
                email      TEXT,
                name       TEXT,
                created_at TEXT
            );
            CREATE TABLE IF NOT EXISTS reference (
                id      INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                name    TEXT NOT NULL,
                cal     REAL NOT NULL,
                unit    TEXT NOT NULL,
                aliases TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS log (
                id         TEXT PRIMARY KEY,
                user_id    TEXT NOT NULL,
                date       TEXT NOT NULL,
                meal       TEXT NOT NULL,
                name       TEXT NOT NULL,
                qty        REAL NOT NULL,
                unit       TEXT NOT NULL,
                cal        INTEGER NOT NULL,
                created_at TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_reference_user ON reference(user_id);
            CREATE INDEX IF NOT EXISTS idx_log_user_date ON log(user_id, date);
            """
        )
        conn.commit()
    finally:
        conn.close()


init_db()


def upsert_user(uid, email, name):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO users (id, email, name, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(id) DO UPDATE SET email=excluded.email, name=excluded.name",
            (uid, email, name, _now()),
        )
        conn.commit()
    finally:
        conn.close()


def get_user(uid):
    conn = get_db()
    try:
        row = conn.execute("SELECT id, email, name FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
    if not row:
        return None
    return {"id": row["id"], "email": row["email"], "name": row["name"]}


def user_reference(uid):
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, name, cal, unit, aliases FROM reference WHERE user_id=? ORDER BY rowid",
            (uid,),
        ).fetchall()
    finally:
        conn.close()
    return [
        {"id": r["id"], "name": r["name"], "cal": r["cal"], "unit": r["unit"], "aliases": json.loads(r["aliases"])}
        for r in rows
    ]


def add_user_reference(uid, name, cal, unit, aliases):
    conn = get_db()
    try:
        conn.execute(
            "INSERT INTO reference (user_id, name, cal, unit, aliases) VALUES (?, ?, ?, ?, ?)",
            (uid, name, float(cal), unit, json.dumps(aliases)),
        )
        conn.commit()
    finally:
        conn.close()


def delete_user_reference(uid, ref_id):
    conn = get_db()
    try:
        conn.execute("DELETE FROM reference WHERE user_id=? AND id=?", (uid, ref_id))
        conn.commit()
    finally:
        conn.close()


def empty_day():
    return {m: [] for m in MEALS}


def user_day(uid, day):
    out = empty_day()
    conn = get_db()
    try:
        rows = conn.execute(
            "SELECT id, name, qty, unit, cal, meal FROM log WHERE user_id=? AND date=? ORDER BY rowid",
            (uid, day),
        ).fetchall()
    finally:
        conn.close()
    for r in rows:
        if r["meal"] in out:
            out[r["meal"]].append(
                {"id": r["id"], "name": r["name"], "qty": r["qty"], "unit": r["unit"], "cal": r["cal"]}
            )
    return out


def add_log_entries(uid, day, meal, entries):
    conn = get_db()
    try:
        for p in entries:
            conn.execute(
                "INSERT INTO log (id, user_id, date, meal, name, qty, unit, cal, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (uuid.uuid4().hex[:8], uid, day, meal, p["name"], p["qty"], p["unit"], p["cal"], _now()),
            )
        conn.commit()
    finally:
        conn.close()


def delete_log_entry(uid, day, meal, entry_id):
    conn = get_db()
    try:
        conn.execute(
            "DELETE FROM log WHERE user_id=? AND date=? AND meal=? AND id=?",
            (uid, day, meal, entry_id),
        )
        conn.commit()
    finally:
        conn.close()


def parse_qty(segment):
    m = re.search(r"(\d+(?:[.,]\d+)?)", segment)
    if m:
        return float(m.group(1).replace(",", "."))
    for word, val in WORDS.items():
        if re.search(rf"\b{word}\b", segment):
            return float(val)
    return None


def _content_words(segment):
    return [w for w in re.findall(r"[a-z]{3,}", segment)
            if w not in UNIT_WORDS and w not in WORDS]


def match_items(segment, reference):
    matched, best_len = [], 0
    for item in reference:
        ml = max((len(a) for a in item["aliases"] if a in segment), default=0)
        if ml:
            matched.append((item, ml))
            best_len = max(best_len, ml)
    if matched:
        return [it for it, ml in matched if ml == best_len]

    words = _content_words(segment)
    return [item for item in reference
            if any(w in alias for w in words for alias in item["aliases"])]


def parse_input(raw, reference):
    text = raw.lower().replace("&", " and ")
    segments = [s.strip() for s in re.split(r",|\band\b|\bplus\b|;|\n", text) if s.strip()]
    out = []
    for seg in segments:
        items = match_items(seg, reference)
        qty = parse_qty(seg)
        if items and qty is not None:
            options = [{
                "name": it["name"],
                "unit": it["unit"],
                "qty": qty,
                "cal": round(qty * it["cal"]),
            } for it in items]
            entry = dict(options[0])
            entry["ok"] = True
            if len(options) > 1:
                entry["alts"] = options
            out.append(entry)
        else:
            out.append({"name": seg, "ok": False})
    return out


app = Flask(__name__, static_folder="static")
app.secret_key = SECRET_KEY
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=bool(os.environ.get("COOKIE_SECURE", "")),
    PERMANENT_SESSION_LIFETIME=timedelta(days=30),
)


def login_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        uid = session.get("uid")
        if not uid:
            return jsonify({"error": "auth_required"}), 401
        g.uid = uid
        return f(*args, **kwargs)
    return wrapper


@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.get("/api/config")
def config():
    return jsonify({"google_client_id": GOOGLE_CLIENT_ID})


@app.post("/api/auth/google")
def auth_google():
    body = request.get_json(force=True)
    credential = body.get("credential")
    if not credential:
        return jsonify({"error": "missing credential"}), 400
    try:
        info = id_token.verify_oauth2_token(credential, google_requests.Request(), GOOGLE_CLIENT_ID)
    except Exception as e:
        return jsonify({"error": "invalid_token", "detail": str(e)}), 401
    uid = info["sub"]
    upsert_user(uid, info.get("email"), info.get("name"))
    session.permanent = True
    session["uid"] = uid
    return jsonify({"user": {"id": uid, "email": info.get("email"), "name": info.get("name")}})


@app.get("/api/auth/me")
def auth_me():
    uid = session.get("uid")
    return jsonify({"user": get_user(uid) if uid else None})


@app.post("/api/auth/logout")
def auth_logout():
    session.clear()
    return jsonify({"ok": True})


@app.get("/api/reference")
@login_required
def get_reference():
    return jsonify(user_reference(g.uid))


@app.post("/api/reference")
@login_required
def add_reference():
    body = request.get_json(force=True)
    name = (body.get("name") or "").strip()
    cal = body.get("cal")
    unit = body.get("unit", "g")
    if not name or cal is None:
        return jsonify({"error": "name and cal are required"}), 400
    low = name.lower()
    aliases = list({low, low.split()[-1]})
    add_user_reference(g.uid, name, cal, unit, aliases)
    return jsonify(user_reference(g.uid))


@app.delete("/api/reference/<int:ref_id>")
@login_required
def delete_reference(ref_id):
    delete_user_reference(g.uid, ref_id)
    return jsonify(user_reference(g.uid))


@app.post("/api/parse")
@login_required
def parse():
    body = request.get_json(force=True)
    parsed = parse_input(body.get("text", ""), user_reference(g.uid))
    return jsonify({"parsed": parsed})


@app.get("/api/log/<day>")
@login_required
def get_day(day):
    return jsonify(user_day(g.uid, day))


@app.post("/api/log/<day>")
@login_required
def add_to_day(day):
    body = request.get_json(force=True)
    meal = body.get("meal", "Breakfast")
    if meal not in MEALS:
        return jsonify({"error": f"meal must be one of {MEALS}"}), 400

    items = body.get("items")
    if items is not None:
        parsed = items
    else:
        parsed = parse_input(body.get("text", ""), user_reference(g.uid))
    good = [p for p in parsed if p.get("ok")]

    add_log_entries(g.uid, day, meal, good)
    return jsonify({"day": user_day(g.uid, day), "added": good, "parsed": parsed})


@app.delete("/api/log/<day>/<meal>/<entry_id>")
@login_required
def delete_entry(day, meal, entry_id):
    delete_log_entry(g.uid, day, meal, entry_id)
    return jsonify(user_day(g.uid, day))


_asr = None
_asr_error = None
_asr_lock = threading.Lock()


def get_asr():
    global _asr, _asr_error
    if _asr is not None or _asr_error is not None:
        return _asr
    with _asr_lock:
        if _asr is not None or _asr_error is not None:
            return _asr
        try:
            from faster_whisper import WhisperModel
            _asr = WhisperModel(ASR_MODEL_SIZE, device="cpu", compute_type="int8")
        except Exception as e:
            _asr_error = str(e)
    return _asr


if os.environ.get("WARM_ASR"):
    threading.Thread(target=get_asr, daemon=True).start()


@app.get("/api/asr-status")
def asr_status():
    try:
        import faster_whisper
        return jsonify({"available": True, "model": ASR_MODEL_SIZE})
    except Exception as e:
        return jsonify({"available": False, "reason": str(e)})


@app.post("/api/transcribe")
@login_required
def transcribe():
    if "audio" not in request.files:
        return jsonify({"error": "no audio file"}), 400

    model = get_asr()
    if model is None:
        return jsonify({"error": "asr_unavailable", "detail": _asr_error}), 503

    upload = request.files["audio"]
    suffix = os.path.splitext(upload.filename or "")[1] or ".webm"
    tmp_path = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
            upload.save(tmp.name)
            tmp_path = tmp.name
        lang = "en" if ASR_MODEL_SIZE.endswith(".en") else None
        segments, _ = model.transcribe(tmp_path, language=lang, beam_size=1, vad_filter=True)
        text = " ".join(seg.text for seg in segments).strip()
        return jsonify({"text": text})
    except Exception as e:
        return jsonify({"error": "transcription_failed", "detail": str(e)}), 500
    finally:
        if tmp_path:
            try:
                os.remove(tmp_path)
            except OSError:
                pass


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True, threaded=True)
