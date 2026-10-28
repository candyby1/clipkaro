"""
ClipKaro - Video Downloader & Uploader
Paste a video link -> download it without watermark.
Login with Facebook -> upload videos to your Page.

Features:
  - Account system (signup/login) — SQLite + hashed passwords
  - Single video download (best quality, no watermark)
  - Link Grabber — Page / playlist / channel ki saari videos ki list
  - Bulk download — selected videos ek ek kar ke, progress ke sath
  - Clipper — video ka hissa kaat kar clip banao
  - Subtitles — auto captions (faster-whisper, agar available ho)
               + apni .srt file video par burn karna (style options ke sath)
  - Facebook Page upload (browser mein, Facebook Login se)
  - TikTok Upload — TikTok account connect kar ke video post (SELF_ONLY)
    ya inbox draft (Content Posting API, OAuth server-side)
  - Profiles — Pages/channels save karo, CSV import/export
  - Output format — downloads ko 9:16 / 16:9 mein convert + pehle N sec trim
  - Video Effects — brightness/contrast/saturation, flip, rotate, crop
  - Brand Overlay — text / PNG logo watermark
  - Auto Clips — lambi video se khud chhote clips (watermark ke sath)
  - Dashboard — usage stats (downloads, size, top sources, job types)
  - AI Voiceover — text se awaz (edge-tts, fallback gTTS) + video par lagana
  - Editor Tools — title text, transitions/joiner, enhance/upscale,
    audio fix (denoise/normalize), silence cut, green screen (chromakey),
    GIF / MP3 / compress / thumbnail
  - Effects upgrades — filter presets, speed control, reverse

Run:  ./venv/bin/python app.py
"""
import os
import re
import csv
import io
import json
import time
import uuid
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from urllib.parse import urlparse, urlencode

from flask import (Flask, request, jsonify, send_file, send_from_directory,
                   session, g, redirect)
from werkzeug.security import generate_password_hash, check_password_hash

import yt_dlp

try:
    import requests
except Exception:  # requests na ho to TikTok features band, app phir bhi chalegi
    requests = None

app = Flask(__name__, static_folder="static", static_url_path="/static")

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DATA_DIR = os.environ.get("CLIPKARO_DATA", os.path.join(BASE_DIR, "data"))
JOBS_DIR = os.path.join(DATA_DIR, "jobs")
COOKIES_DIR = os.path.join(DATA_DIR, "cookies")
DB_PATH = os.path.join(DATA_DIR, "clipkaro.db")
for _d in (DATA_DIR, JOBS_DIR, COOKIES_DIR):
    os.makedirs(_d, exist_ok=True)

DOWNLOAD_DIR = os.path.join(tempfile.gettempdir(), "vidfetch")
os.makedirs(DOWNLOAD_DIR, exist_ok=True)


def _load_secret():
    env = os.environ.get("CLIPKARO_SECRET")
    if env:
        return env
    path = os.path.join(DATA_DIR, ".secret")
    if os.path.exists(path):
        with open(path) as f:
            return f.read().strip()
    secret = uuid.uuid4().hex + uuid.uuid4().hex
    with open(path, "w") as f:
        f.write(secret)
    return secret


app.secret_key = _load_secret()
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    PERMANENT_SESSION_LIFETIME=60 * 60 * 24 * 30,
)

URL_RE = re.compile(r"^https?://", re.IGNORECASE)
SAFE_NAME_RE = re.compile(r'[\\/*?:"<>|]')

# ---------------------------------------------------------------- TikTok config
# Client Key deploy ke waqt yahan bhara jata hai (placeholder khali hai);
# Secret hamesha env se aata hai — code mein kabhi hardcode nahi hota.
TIKTOK_CLIENT_KEY = os.environ.get("TIKTOK_CLIENT_KEY", "awuo8ngjthqd25g2")
TIKTOK_CLIENT_SECRET = os.environ.get("TIKTOK_CLIENT_SECRET", "")
TIKTOK_REDIRECT_URI = "https://clipkaro-yfbq.onrender.com/tiktok/callback"
TIKTOK_SCOPES = "user.info.basic,video.upload,video.publish"
TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"
TIKTOK_MAX_UPLOAD = 64 * 1024 * 1024  # single-chunk upload ki had: 64 MB

# ---------------------------------------------------------------- DB / auth

def db():
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def _close_db(exc):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.executescript("""
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            pass_hash TEXT NOT NULL,
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS jobs (
            id TEXT PRIMARY KEY,
            user_id INTEGER NOT NULL,
            type TEXT NOT NULL,
            status TEXT NOT NULL DEFAULT 'running',
            data TEXT NOT NULL DEFAULT '{}',
            created_at TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS profiles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            platform TEXT NOT NULL DEFAULT 'Other',
            url TEXT NOT NULL,
            captions_on INTEGER NOT NULL DEFAULT 0,
            brand_on INTEGER NOT NULL DEFAULT 0,
            brand_text TEXT NOT NULL DEFAULT '',
            clips_on INTEGER NOT NULL DEFAULT 0,
            clip_count INTEGER NOT NULL DEFAULT 0,
            created TEXT DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS stats (
            user_id INTEGER PRIMARY KEY,
            downloads INTEGER NOT NULL DEFAULT 0,
            bytes INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS tiktok_tokens (
            user_id INTEGER PRIMARY KEY,
            open_id TEXT,
            access_token TEXT,
            refresh_token TEXT,
            expires_at INTEGER,
            scope TEXT
        );
        CREATE TABLE IF NOT EXISTS payments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            plan TEXT,
            amount INTEGER,
            method TEXT,
            txn_ref TEXT,
            sender_name TEXT,
            sender_phone TEXT,
            status TEXT DEFAULT 'pending',
            created_at INTEGER,
            decided_at INTEGER
        );
    """)
    # purani stats table mein tiktok_uploads column na ho to add kar do
    _cols = [r[1] for r in conn.execute("PRAGMA table_info(stats)").fetchall()]
    if "tiktok_uploads" not in _cols:
        conn.execute("ALTER TABLE stats ADD COLUMN tiktok_uploads "
                     "INTEGER NOT NULL DEFAULT 0")
    # plans system: users table mein plan wale columns na hon to add kar do
    _ucols = [r[1] for r in conn.execute("PRAGMA table_info(users)").fetchall()]
    if "plan" not in _ucols:
        conn.execute("ALTER TABLE users ADD COLUMN plan TEXT DEFAULT 'none'")
    if "plan_expiry" not in _ucols:
        conn.execute("ALTER TABLE users ADD COLUMN plan_expiry INTEGER")
    if "trial_used" not in _ucols:
        conn.execute("ALTER TABLE users ADD COLUMN trial_used INTEGER DEFAULT 0")
    conn.commit()
    conn.close()


init_db()


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    row = db().execute("SELECT id, username FROM users WHERE id = ?", (uid,)).fetchone()
    return dict(row) if row else None


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if not current_user():
            return jsonify({"ok": False, "error": "Pehle login karo.", "need_login": True}), 401
        return fn(*args, **kwargs)
    return wrapper

# ---------------------------------------------------------------- plans / trial

TRIAL_CODE = "CandyBoy"
PRICES = {"monthly": 2500, "lifetime": 8000}
MONTHLY_DAYS = 30
PLAN_REQUIRED_MSG = "Pehle Trial ya Plan active karein"


def _full_user(user_id):
    if not user_id:
        return None
    row = db().execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def is_admin(user):
    return bool(user) and user.get("username") == "admin"


def subscription_active(user):
    """Admin hamesha active; lifetime hamesha; trial/monthly expiry tak."""
    if not user:
        return False
    if is_admin(user):
        return True
    plan = user.get("plan") or "none"
    if plan == "lifetime":
        return True
    if plan in ("trial", "monthly"):
        exp = user.get("plan_expiry")
        return bool(exp) and exp > int(time.time())
    return False


def plan_required(fn):
    """Work/action endpoints ka gate — plan na ho to 403 plan_required."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        user = _full_user(session.get("user_id"))
        if not subscription_active(user):
            return jsonify({"ok": False, "error": "plan_required",
                            "message": PLAN_REQUIRED_MSG}), 403
        return fn(*args, **kwargs)
    return wrapper


@app.route("/api/signup", methods=["POST"])
def signup():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    if len(username) < 3:
        return jsonify({"ok": False, "error": "Username kam az kam 3 characters ka ho."}), 400
    if len(password) < 6:
        return jsonify({"ok": False, "error": "Password kam az kam 6 characters ka ho."}), 400
    try:
        cur = db().execute(
            "INSERT INTO users (username, pass_hash) VALUES (?, ?)",
            (username, generate_password_hash(password)),
        )
        db().commit()
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "error": "Ye username pehle se maujood hai, doosra chuno."}), 409
    session.permanent = True
    session["user_id"] = cur.lastrowid
    return jsonify({"ok": True, "username": username})


@app.route("/api/login", methods=["POST"])
def login():
    data = request.get_json(force=True, silent=True) or {}
    username = (data.get("username") or "").strip()
    password = data.get("password") or ""
    row = db().execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
    if not row or not check_password_hash(row["pass_hash"], password):
        return jsonify({"ok": False, "error": "Username ya password ghalat hai."}), 401
    session.permanent = True
    session["user_id"] = row["id"]
    return jsonify({"ok": True, "username": row["username"]})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/change-password", methods=["POST"])
@login_required
def change_password():
    data = request.get_json(force=True, silent=True) or {}
    old = data.get("old_password") or ""
    new = data.get("new_password") or ""
    if len(new) < 6:
        return jsonify({"ok": False, "error": "Naya password kam az kam 6 characters ka ho."}), 400
    row = db().execute("SELECT * FROM users WHERE id = ?", (session["user_id"],)).fetchone()
    if not row or not check_password_hash(row["pass_hash"], old):
        return jsonify({"ok": False, "error": "Purana password ghalat hai."}), 401
    db().execute("UPDATE users SET pass_hash = ? WHERE id = ?",
                 (generate_password_hash(new), session["user_id"]))
    db().commit()
    return jsonify({"ok": True})



# ---------------------------------------------------------------- plans API

def _plan_status_dict(user):
    return {
        "ok": True,
        "plan": (user.get("plan") or "none"),
        "plan_expiry": user.get("plan_expiry"),
        "active": subscription_active(user),
        "trial_used": int(user.get("trial_used") or 0),
        "is_admin": is_admin(user),
        "prices": dict(PRICES),
    }


@app.route("/api/plans/status")
@login_required
def plans_status():
    return jsonify(_plan_status_dict(_full_user(session["user_id"])))


@app.route("/api/plans/trial", methods=["POST"])
@login_required
def plans_trial():
    data = request.get_json(force=True, silent=True) or {}
    code = (data.get("code") or "").strip()
    user = _full_user(session["user_id"])
    if code.lower() != TRIAL_CODE.lower():
        return jsonify({"ok": False, "error": "Trial code ghalat hai."}), 400
    if int(user.get("trial_used") or 0):
        return jsonify({"ok": False,
                        "error": "Aap ka FREE trial pehle use ho chuka hai — "
                                 "ab Monthly ya Lifetime plan lein."}), 400
    now = int(time.time())
    plan = user.get("plan") or "none"
    if plan == "lifetime" or (plan == "monthly" and user.get("plan_expiry")
                              and user["plan_expiry"] > now):
        return jsonify({"ok": False,
                        "error": "Aap ka paid plan pehle se active hai."}), 400
    expiry = now + 86400
    db().execute("UPDATE users SET plan = 'trial', plan_expiry = ?, "
                 "trial_used = 1 WHERE id = ?", (expiry, user["id"]))
    db().commit()
    return jsonify({"ok": True, "plan": "trial", "plan_expiry": expiry,
                    "message": "🎉 1 Day FREE Trial active ho gaya hai — "
                               "24 ghante ke liye saare tools khul gaye hain."})


@app.route("/api/plans/pay-request", methods=["POST"])
@login_required
def plans_pay_request():
    data = request.get_json(force=True, silent=True) or {}
    plan = (data.get("plan") or "").strip().lower()
    method = (data.get("method") or "").strip()
    txn_ref = (data.get("txn_ref") or "").strip()
    sender_name = (data.get("sender_name") or "").strip()
    sender_phone = (data.get("sender_phone") or "").strip()
    if plan not in PRICES:
        return jsonify({"ok": False,
                        "error": "Plan ghalat hai — Monthly ya Lifetime chunein."}), 400
    if method not in ("EasyPaisa", "Meezan Bank"):
        return jsonify({"ok": False,
                        "error": "Payment method ghalat hai — EasyPaisa ya Meezan Bank chunein."}), 400
    if not txn_ref or not sender_name or not sender_phone:
        return jsonify({"ok": False,
                        "error": "Sender name, phone number aur transaction ID — teeno zaroori hain."}), 400
    cur = db().execute(
        "INSERT INTO payments (user_id, plan, amount, method, txn_ref, "
        "sender_name, sender_phone, status, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'pending', ?)",
        (session["user_id"], plan, PRICES[plan], method, txn_ref,
         sender_name, sender_phone, int(time.time())))
    db().commit()
    return jsonify({"ok": True, "payment_id": cur.lastrowid,
                    "amount": PRICES[plan],
                    "message": "✅ Payment request bhej di gayi hai — verify "
                               "karke aap ka plan active kar diya jayega."})


@app.route("/api/plans/my-payments")
@login_required
def plans_my_payments():
    rows = db().execute(
        "SELECT id, plan, amount, method, txn_ref, sender_name, sender_phone, "
        "status, created_at, decided_at FROM payments WHERE user_id = ? "
        "ORDER BY id DESC LIMIT 50", (session["user_id"],)).fetchall()
    return jsonify({"ok": True, "payments": [dict(r) for r in rows]})


def _require_admin():
    """(user, None) admin ho to, warna (None, 403 response)."""
    user = _full_user(session.get("user_id"))
    if not is_admin(user):
        return None, (jsonify({"ok": False,
                               "error": "Ye sirf admin ke liye hai."}), 403)
    return user, None


@app.route("/api/admin/payments")
@login_required
def admin_payments():
    _, err = _require_admin()
    if err:
        return err
    rows = db().execute(
        "SELECT p.*, u.username FROM payments p "
        "JOIN users u ON u.id = p.user_id "
        "ORDER BY CASE p.status WHEN 'pending' THEN 0 ELSE 1 END, p.id DESC "
        "LIMIT 100").fetchall()
    return jsonify({"ok": True, "payments": [dict(r) for r in rows]})


@app.route("/api/admin/users")
@login_required
def admin_users():
    _, err = _require_admin()
    if err:
        return err
    rows = db().execute(
        "SELECT id, username, plan, plan_expiry, trial_used, created_at "
        "FROM users ORDER BY id").fetchall()
    return jsonify({"ok": True, "users": [dict(r) for r in rows]})


@app.route("/api/admin/payments/<int:pid>/approve", methods=["POST"])
@login_required
def admin_payment_approve(pid):
    _, err = _require_admin()
    if err:
        return err
    pay = db().execute("SELECT * FROM payments WHERE id = ?", (pid,)).fetchone()
    if not pay:
        return jsonify({"ok": False, "error": "Payment nahi mili."}), 404
    if pay["status"] != "pending":
        return jsonify({"ok": False,
                        "error": "Ye payment pehle hi decide ho chuki hai."}), 400
    now = int(time.time())
    if pay["plan"] == "lifetime":
        db().execute("UPDATE users SET plan = 'lifetime', plan_expiry = NULL "
                     "WHERE id = ?", (pay["user_id"],))
    else:
        user = _full_user(pay["user_id"])
        base = now
        if (user and user.get("plan") == "monthly"
                and user.get("plan_expiry") and user["plan_expiry"] > now):
            base = user["plan_expiry"]
        db().execute("UPDATE users SET plan = 'monthly', plan_expiry = ? "
                     "WHERE id = ?", (base + MONTHLY_DAYS * 86400, pay["user_id"]))
    db().execute("UPDATE payments SET status = 'approved', decided_at = ? "
                 "WHERE id = ?", (now, pid))
    db().commit()
    return jsonify({"ok": True, "message": "✅ Payment approve ho gayi — plan active kar diya gaya hai."})


@app.route("/api/admin/payments/<int:pid>/reject", methods=["POST"])
@login_required
def admin_payment_reject(pid):
    _, err = _require_admin()
    if err:
        return err
    pay = db().execute("SELECT * FROM payments WHERE id = ?", (pid,)).fetchone()
    if not pay:
        return jsonify({"ok": False, "error": "Payment nahi mili."}), 404
    if pay["status"] != "pending":
        return jsonify({"ok": False,
                        "error": "Ye payment pehle hi decide ho chuki hai."}), 400
    db().execute("UPDATE payments SET status = 'rejected', decided_at = ? "
                 "WHERE id = ?", (int(time.time()), pid))
    db().commit()
    return jsonify({"ok": True, "message": "Payment reject kar di gayi hai."})


@app.route("/payments/<name>")
def payment_qr(name):
    if name not in ("easypaisa-qr.png", "meezan-qr.png"):
        return jsonify({"ok": False, "error": "File nahi mili."}), 404
    return send_from_directory(os.path.join(BASE_DIR, "payments"), name)


@app.route("/api/me")
def me():
    user = current_user()
    if not user:
        return jsonify({"ok": True, "logged_in": False})
    return jsonify({"ok": True, "logged_in": True, "username": user["username"]})

# ---------------------------------------------------------------- helpers

def ydl_opts(extra=None):
    opts = {
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
        # clean original file, no watermark added
        "format": "bv*+ba/best",
        "merge_output_format": "mp4",
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
    }
    if extra:
        opts.update(extra)
    return opts


def quality_label(height):
    """Sirf asal height ke mutabiq label — 4K tabhi jab height >= 2160 ho."""
    if not height:
        return None
    if height >= 2160:
        return "4K"
    if height >= 1440:
        return "2K (1440p)"
    return f"{height}p"


def meta_height(meta):
    """Video ki asal height — top-level na ho to formats se sab se bari."""
    h = meta.get("height")
    if h:
        return h
    heights = [f.get("height") for f in (meta.get("formats") or [])
               if f.get("height") and f.get("vcodec") not in (None, "none")]
    return max(heights) if heights else None


def probe_height(path):
    """Downloaded file ki asal height ffprobe se (yt-dlp direct files ki
    height nahi batata — ye file ki haqeeqi quality hai)."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=height", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30)
        h = int(r.stdout.strip())
        return h or None
    except Exception:
        return None


def clean_title(title, maxlen=80):
    return SAFE_NAME_RE.sub("", (title or "video")).strip()[:maxlen] or "video"


def user_cookiefile(user_id):
    path = os.path.join(COOKIES_DIR, f"{user_id}.txt")
    return path if os.path.exists(path) else None


def save_job(job_id, user_id, jtype, data, status="running"):
    db().execute(
        "INSERT OR REPLACE INTO jobs (id, user_id, type, status, data) VALUES (?, ?, ?, ?, ?)",
        (job_id, user_id, jtype, status, json.dumps(data)),
    )
    db().commit()


def update_job(job_id, data, status=None):
    conn = sqlite3.connect(DB_PATH)
    if status is None:
        conn.execute("UPDATE jobs SET data = ? WHERE id = ?", (json.dumps(data), job_id))
    else:
        conn.execute("UPDATE jobs SET data = ?, status = ? WHERE id = ?",
                     (json.dumps(data), status, job_id))
    conn.commit()
    conn.close()


def get_job(job_id, user_id):
    row = db().execute("SELECT * FROM jobs WHERE id = ? AND user_id = ?",
                       (job_id, user_id)).fetchone()
    if not row:
        return None
    return {"id": row["id"], "type": row["type"], "status": row["status"],
            "created_at": row["created_at"], "data": json.loads(row["data"] or "{}")}

# ---------------------------------------------------------------- media helpers

FONT_FILE = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
if not os.path.isfile(FONT_FILE):
    FONT_FILE = None

# drawtext positions (x, y) — tw/th = text width/height
TEXT_XY = {
    "br": ("w-tw-20", "h-th-20"),
    "bl": ("20", "h-th-20"),
    "tr": ("w-tw-20", "20"),
    "tl": ("20", "20"),
}
# overlay filter positions — W/H = video, w/h = logo
LOGO_XY = {
    "br": ("W-w-20", "H-h-20"),
    "bl": ("20", "H-h-20"),
    "tr": ("W-w-20", "20"),
    "tl": ("20", "20"),
}

ENC_ARGS = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart"]


def probe_dims(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "json", path],
            capture_output=True, text=True, timeout=30)
        st = (json.loads(r.stdout or "{}").get("streams") or [{}])[0]
        return st.get("width"), st.get("height")
    except Exception:
        return None, None


def probe_duration(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30)
        return float(r.stdout.strip())
    except Exception:
        return None


def probe_has_audio(path):
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "a",
             "-show_entries", "stream=index", "-of", "csv=p=0", path],
            capture_output=True, text=True, timeout=30)
        return bool(r.stdout.strip())
    except Exception:
        return False


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def bump_stats(user_id, downloads=0, nbytes=0, tiktok_uploads=0):
    """Lifetime counters (thread-safe — apna connection)."""
    if not downloads and not nbytes and not tiktok_uploads:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO stats (user_id, downloads, bytes, tiktok_uploads) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "downloads = downloads + excluded.downloads, bytes = bytes + excluded.bytes, "
        "tiktok_uploads = tiktok_uploads + excluded.tiktok_uploads",
        (user_id, downloads, nbytes, tiktok_uploads))
    conn.commit()
    conn.close()


def _ratio_vf(fmt):
    """9:16 / 16:9 / 1:1 ke liye scale+pad filter (aspect barqarar, setsar=1)."""
    if fmt == "vertical":
        return ("scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if fmt == "horizontal":
        return ("scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if fmt == "square":
        return ("scale=1080:1080:force_original_aspect_ratio=decrease,"
                "pad=1080:1080:(ow-iw)/2:(oh-ih)/2,setsar=1")
    return None


def process_format(path, fmt="original", secs=None, out_path=None):
    """Download ke baad format convert (9:16 / 16:9) aur/ya pehle N seconds trim.
    Koi change na ho to asal path hi wapas; warna nayi file ka path."""
    vf = _ratio_vf(fmt)
    try:
        secs = float(secs) if secs else None
    except (TypeError, ValueError):
        secs = None
    if not vf and not secs:
        return path
    out_path = out_path or (os.path.splitext(path)[0] + ".fmt.mp4")
    cmd = ["ffmpeg", "-y", "-i", path]
    if secs:
        cmd += ["-t", str(secs)]
    if vf:
        cmd += ["-vf", vf]
    cmd += ENC_ARGS + [out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not os.path.isfile(out_path):
        raise RuntimeError("Format convert nahi ho saka: " + (r.stderr or "")[-200:])
    return out_path


def _drawtext(textfile, pos="br", size=36, opacity=1.0):
    """drawtext filter string — text aik file se (escaping ke jhanjhat se bachne ke liye)."""
    x, y = TEXT_XY.get(pos, TEXT_XY["br"])
    alpha = min(max(float(opacity), 0.0), 1.0)
    esc = textfile.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    parts = []
    if FONT_FILE:
        parts.append(f"fontfile='{FONT_FILE}'")
    parts += [f"textfile='{esc}'", f"fontcolor=white@{alpha:.2f}",
              "shadowcolor=black@0.6", "shadowx=2", "shadowy=2",
              f"fontsize={int(size)}", f"x={x}", f"y={y}"]
    return "drawtext=" + ":".join(parts)


def _write_textfile(text):
    fd, path = tempfile.mkstemp(dir=DOWNLOAD_DIR, suffix=".txt")
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
    return path


def _resolve_source(file_field="video"):
    """Upload ki hui file ya bulk-download job file se source path.
    (path, cleanup?) return karta hai — path None ho to source nahi mila."""
    f = request.files.get(file_field)
    if f and f.filename:
        return _temp_upload(f, suffix=os.path.splitext(f.filename)[1] or ".mp4"), True
    job_id = (request.form.get("job_id") or "").strip()
    fname = os.path.basename(request.form.get("filename") or "")
    if job_id and fname and get_job(job_id, session["user_id"]):
        cand = os.path.join(JOBS_DIR, job_id, fname)
        if os.path.isfile(cand):
            return cand, False
    return None, False


def _source_title(fallback="Video"):
    """Source ka title — job file ho to item ka title, warna upload ka file name."""
    job_id = (request.form.get("job_id") or "").strip()
    fname = os.path.basename(request.form.get("filename") or "")
    if job_id and fname:
        job = get_job(job_id, session["user_id"])
        if job:
            for item in job["data"].get("items", []):
                if item.get("file") == fname and item.get("title"):
                    return item["title"]
    f = request.files.get("video")
    if f and f.filename:
        return os.path.splitext(f.filename)[0]
    return fallback


def _register_file_job(user_id, jtype, title, job_dir, out_path):
    """Job dir mein tayyar file ko finished job bana kar result dict do."""
    job_id = os.path.basename(job_dir)
    fname = os.path.basename(out_path)
    h = probe_height(out_path)
    item = {"url": None, "title": title, "file": fname, "status": "done",
            "height": h, "quality": quality_label(h),
            "size": os.path.getsize(out_path), "error": None, "progress": 100}
    save_job(job_id, user_id, jtype, {"items": [item], "done": 1, "total": 1},
             status="finished")
    return {"job_id": job_id, "download_url": f"/api/files/{job_id}/{fname}",
            "title": title, "quality": item["quality"], "size": item["size"]}


def _run_ffmpeg_job(user_id, jtype, title, cmd_for_out, ext="mp4"):
    """ffmpeg chala kar output ko aik finished job bana do.
    cmd_for_out(out_path) -> puri ffmpeg command list.
    Success par (result_dict, None), fail par (None, error_str)."""
    job_id = uuid.uuid4().hex
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    out_path = os.path.join(job_dir, f"000.{ext}")
    cmd = cmd_for_out(out_path)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        return None, (r.stderr or "ffmpeg fail ho gaya")[-250:]
    return _register_file_job(user_id, jtype, title, job_dir, out_path), None

# ---------------------------------------------------------------- pages

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")


_PAGE_STYLE = ("<style>body{font-family:sans-serif;background:#0f1220;color:#eef1ff;"
               "max-width:760px;margin:0 auto;padding:32px 20px;line-height:1.65}"
               "h1{font-size:24px}h2{font-size:17px;margin-top:26px}a{color:#6c8cff}</style>")


@app.route("/tiktok7gWHUCwo11eUVlbu4rpVkUGLPChof2Ra.txt")
def tiktok_site_verification():
    return send_from_directory(BASE_DIR, "tiktok7gWHUCwo11eUVlbu4rpVkUGLPChof2Ra.txt")


@app.route("/privacy")
def privacy():
    return (_PAGE_STYLE +
            "<h1>ClipKaro — Privacy Policy</h1>"
            "<p>ClipKaro is an online tool to download, edit and upload videos. "
            "This page explains what information the service handles.</p>"
            "<h2>Accounts</h2><p>To use the tools you create an account with a username and a "
            "password. Passwords are stored only as a secure hash — never in plain text.</p>"
            "<h2>Your files</h2><p>Videos and files you process are stored temporarily so you can "
            "download the results of your jobs. Each account can only see its own jobs and files. "
            "Files may be removed when the service restarts or storage is cleared.</p>"
            "<h2>Connected accounts (Facebook / TikTok)</h2><p>If you connect a Facebook or TikTok "
            "account, ClipKaro uses the permission you approve only to list your Pages/accounts and "
            "to upload the videos you choose. ClipKaro never asks for or stores your Facebook or "
            "TikTok password.</p>"
            "<h2>What we do not do</h2><p>We do not sell your personal information. We do not post "
            "anything to your accounts without your action in the tool.</p>"
            "<h2>Contact</h2><p>For any privacy question, contact the site owner through the "
            "ClipKaro website.</p><p><a href='/'>Back to ClipKaro</a></p>")


@app.route("/terms")
def terms():
    return (_PAGE_STYLE +
            "<h1>ClipKaro — Terms of Service</h1>"
            "<h2>Using the tool</h2><p>ClipKaro provides video downloading, editing and uploading "
            "tools. You are responsible for the videos you download, edit and upload, and for having "
            "the rights or permission to use them. Do not use the tool for content you do not own or "
            "have permission for.</p>"
            "<h2>Accounts</h2><p>Keep your account password safe. Accounts are personal; the site "
            "owner may suspend accounts that misuse the service.</p>"
            "<h2>Availability</h2><p>The service is provided as-is, without a guarantee of uptime. "
            "Features may change or be removed at any time.</p>"
            "<h2>Uploads to other platforms</h2><p>When you upload through ClipKaro to Facebook or "
            "TikTok, those platforms' own terms also apply to your content.</p>"
            "<p><a href='/'>Back to ClipKaro</a></p>")

# ---------------------------------------------------------------- single video

@app.route("/api/info", methods=["POST"])
@login_required
@plan_required
def info():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not URL_RE.match(url):
        return jsonify({"ok": False, "error": "Sahi link paste karo (http/https)."}), 400
    extra = {}
    cf = user_cookiefile(session["user_id"])
    if data.get("use_cookies") and cf:
        extra["cookiefile"] = cf
    try:
        with yt_dlp.YoutubeDL(ydl_opts(extra)) as ydl:
            meta = ydl.extract_info(url, download=False)
        formats = []
        seen = set()
        for f in (meta.get("formats") or []):
            if f.get("vcodec") == "none":
                continue
            h = f.get("height")
            if not h or h in seen:
                continue
            seen.add(h)
            formats.append({
                "format_id": f.get("format_id"),
                "height": h,
                "ext": f.get("ext"),
                "filesize": f.get("filesize") or f.get("filesize_approx"),
            })
        formats.sort(key=lambda x: x["height"], reverse=True)
        return jsonify({
            "ok": True,
            "title": meta.get("title"),
            "thumbnail": meta.get("thumbnail"),
            "duration": meta.get("duration"),
            "uploader": meta.get("uploader"),
            "formats": formats[:6],
        })
    except Exception as e:
        return jsonify({"ok": False, "error": f"Video ki info nahi mil saki: {str(e)[:200]}"}), 422


@app.route("/api/download", methods=["POST"])
@login_required
@plan_required
def download():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    format_id = (data.get("format_id") or "best").strip()
    if not URL_RE.match(url):
        return jsonify({"ok": False, "error": "Sahi link paste karo (http/https)."}), 400

    job = uuid.uuid4().hex
    outtmpl = os.path.join(DOWNLOAD_DIR, f"{job}.%(ext)s")
    extra = {
        "format": f"{format_id}+bestaudio/best" if format_id != "best" else "bv*+ba/best",
        "outtmpl": outtmpl,
    }
    cf = user_cookiefile(session["user_id"])
    if data.get("use_cookies") and cf:
        extra["cookiefile"] = cf
    fmt = (data.get("fmt") or "original").strip()
    if fmt not in ("original", "vertical", "horizontal", "square"):
        fmt = "original"
    secs = data.get("secs")
    opts = ydl_opts(extra)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(url, download=True)
        matches = [p for p in os.listdir(DOWNLOAD_DIR) if p.startswith(job)]
        if not matches:
            return jsonify({"ok": False, "error": "Download ho gaya lekin file nahi mili."}), 500
        path = os.path.join(DOWNLOAD_DIR, matches[0])
        serve_path = path
        if fmt != "original" or secs:
            serve_path = process_format(
                path, fmt, secs,
                out_path=os.path.join(DOWNLOAD_DIR, f"{job}.fmt.mp4"))
        title = clean_title(meta.get("title"))
        dl_name = f"{title}.mp4"
        bump_stats(session["user_id"], downloads=1,
                   nbytes=os.path.getsize(serve_path))
        resp = send_file(serve_path, as_attachment=True, download_name=dl_name, mimetype="video/mp4")
        # asal quality browser ko bhi bata do taake UI sahi label dikhaye
        # (convert hui ho to file ki asal height hi sahi hai)
        height = probe_height(serve_path) if serve_path != path else None
        height = height or meta_height(meta) or probe_height(serve_path)
        if height:
            resp.headers["X-Video-Height"] = str(height)
            resp.headers["X-Video-Quality"] = quality_label(height) or ""
            resp.headers["Access-Control-Expose-Headers"] = "X-Video-Height, X-Video-Quality"

        @resp.call_on_close
        def _cleanup():
            for p in {path, serve_path}:
                try:
                    os.remove(p)
                except OSError:
                    pass
        return resp
    except Exception as e:
        for p in os.listdir(DOWNLOAD_DIR):
            if p.startswith(job):
                try:
                    os.remove(os.path.join(DOWNLOAD_DIR, p))
                except OSError:
                    pass
        return jsonify({"ok": False, "error": f"Download fail ho gaya: {str(e)[:200]}"}), 422

# ---------------------------------------------------------------- cookies (FB login wali pages ke liye)

@app.route("/api/cookies", methods=["POST"])
@login_required
@plan_required
def upload_cookies():
    f = request.files.get("cookies")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "cookies.txt file select karo."}), 400
    path = os.path.join(COOKIES_DIR, f"{session['user_id']}.txt")
    f.save(path)
    return jsonify({"ok": True, "message": "Cookies save ho gayi — ab Grab/Download mein 'cookies use karo' kaam karega."})


@app.route("/api/cookies/status")
@login_required
def cookies_status():
    return jsonify({"ok": True, "has_cookies": bool(user_cookiefile(session["user_id"]))})

# ---------------------------------------------------------------- link grabber

@app.route("/api/grab", methods=["POST"])
@login_required
@plan_required
def grab():
    data = request.get_json(force=True, silent=True) or {}
    url = (data.get("url") or "").strip()
    if not URL_RE.match(url):
        return jsonify({"ok": False, "error": "Sahi link paste karo (http/https)."}), 400
    extra = {
        "extract_flat": "in_playlist",
        "noplaylist": False,
        "playlistend": int(data.get("limit") or 200),
    }
    cf = user_cookiefile(session["user_id"])
    if data.get("use_cookies") and cf:
        extra["cookiefile"] = cf
    try:
        with yt_dlp.YoutubeDL(ydl_opts(extra)) as ydl:
            meta = ydl.extract_info(url, download=False)
    except Exception as e:
        return jsonify({"ok": False,
                        "error": f"List nahi mil saki: {str(e)[:250]}. "
                                 "Facebook ki kuch Pages ke liye login cookies zaroori hoti hain — "
                                 "neeche cookies.txt upload kar ke dobara try karo."}), 422
    entries = []
    raw_entries = meta.get("entries") if isinstance(meta, dict) else None
    if raw_entries:
        for e in raw_entries:
            if not e:
                continue
            vurl = e.get("url") or e.get("webpage_url")
            if not vurl and e.get("id") and e.get("ie_key") == "Youtube":
                vurl = f"https://www.youtube.com/watch?v={e['id']}"
            if not vurl:
                continue
            thumbs = e.get("thumbnails") or []
            thumb = e.get("thumbnail") or (thumbs[-1].get("url") if thumbs else None)
            entries.append({
                "title": e.get("title") or "Video",
                "url": vurl,
                "duration": e.get("duration"),
                "thumbnail": thumb,
            })
    else:
        # single video link diya gaya tha — usi ko aik entry bana do
        entries.append({
            "title": meta.get("title") or "Video",
            "url": meta.get("webpage_url") or url,
            "duration": meta.get("duration"),
            "thumbnail": meta.get("thumbnail"),
        })
    return jsonify({
        "ok": True,
        "title": meta.get("title"),
        "uploader": meta.get("uploader") or meta.get("channel"),
        "count": len(entries),
        "entries": entries,
    })

# ---------------------------------------------------------------- bulk download jobs

def _completed_url_map(user_id, exclude_job_id):
    """User ki pichli completed downloads: url -> (job_id, file, title).
    Duplicate skip ke liye — sirf wo files jo disk par abhi maujood hain."""
    out = {}
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, data FROM jobs WHERE user_id = ? AND id != ?",
                        (user_id, exclude_job_id)).fetchall()
    conn.close()
    for r in rows:
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        for it in d.get("items", []):
            if it.get("status") == "done" and it.get("url") and it.get("file"):
                fjob = it.get("file_job") or r["id"]
                if os.path.isfile(os.path.join(JOBS_DIR, fjob, it["file"])):
                    out.setdefault(it["url"], (fjob, it["file"], it.get("title")))
    return out


def _run_bulk_job(job_id, user_id, urls, cookiefile, fmt="original", secs=None,
                  concurrency=1, skip_dup=False):
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    items = []
    for u in urls:
        items.append({"url": u, "status": "pending", "title": None,
                      "file": None, "height": None, "quality": None,
                      "size": None, "error": None, "progress": 0, "note": None})
    lock = threading.Lock()

    def persist(status=None):
        with lock:
            data = {"items": items,
                    "done": sum(1 for i in items if i["status"] in ("done", "error")),
                    "total": len(items)}
            update_job(job_id, data, status=status)

    persist()
    dup_map = _completed_url_map(user_id, job_id) if skip_dup else {}

    def one(idx):
        item = items[idx]
        if item["url"] in dup_map:
            fjob, ffile, ftitle = dup_map[item["url"]]
            item.update({"status": "done", "title": ftitle, "file": ffile,
                         "file_job": fjob, "progress": 100,
                         "note": "duplicate — pehle se downloaded"})
            persist()
            return
        item["status"] = "downloading"

        def hook(d):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                item["progress"] = int(got * 100 / total) if total else 0
                persist()

        prefix = f"{idx:03d}"
        extra = {
            "outtmpl": os.path.join(job_dir, prefix + ".%(ext)s"),
            "progress_hooks": [hook],
        }
        if cookiefile:
            extra["cookiefile"] = cookiefile
        try:
            with yt_dlp.YoutubeDL(ydl_opts(extra)) as ydl:
                meta = ydl.extract_info(item["url"], download=True)
            matches = [p for p in os.listdir(job_dir)
                       if p.startswith(prefix + ".") and not p.endswith(".fmt.mp4")]
            if not matches:
                raise RuntimeError("File nahi mili")
            fname = matches[0]
            fpath = os.path.join(job_dir, fname)
            if fmt != "original" or secs:
                fpath = process_format(fpath, fmt, secs,
                                       out_path=os.path.join(job_dir, prefix + ".fmt.mp4"))
                try:
                    os.remove(os.path.join(job_dir, fname))
                except OSError:
                    pass
                fname = os.path.basename(fpath)
            item["file"] = fname
            item["title"] = meta.get("title")
            item["height"] = meta_height(meta) or probe_height(fpath)
            item["quality"] = quality_label(item["height"])
            item["size"] = os.path.getsize(fpath)
            item["progress"] = 100
            item["status"] = "done"
            bump_stats(user_id, downloads=1, nbytes=item["size"])
        except Exception as e:
            item["status"] = "error"
            item["error"] = str(e)[:250]
        persist()

    workers = max(1, min(4, int(concurrency or 1)))
    if workers == 1:
        for i in range(len(items)):
            one(i)
    else:
        with ThreadPoolExecutor(max_workers=workers) as ex:
            list(ex.map(one, range(len(items))))
    persist(status="finished")


@app.route("/api/bulk-download", methods=["POST"])
@login_required
@plan_required
def bulk_download():
    data = request.get_json(force=True, silent=True) or {}
    urls = [u.strip() for u in (data.get("urls") or []) if isinstance(u, str) and URL_RE.match(u.strip())]
    if not urls:
        return jsonify({"ok": False, "error": "Koi valid video select nahi hui."}), 400
    if len(urls) > 100:
        return jsonify({"ok": False, "error": "Aik dafa mein zyada se zyada 100 videos select karo."}), 400
    fmt = (data.get("fmt") or "original").strip()
    if fmt not in ("original", "vertical", "horizontal", "square"):
        fmt = "original"
    try:
        secs = float(data["secs"]) if data.get("secs") else None
    except (TypeError, ValueError):
        secs = None
    try:
        concurrency = max(1, min(4, int(data.get("concurrency") or 1)))
    except (TypeError, ValueError):
        concurrency = 1
    skip_dup = bool(data.get("skip_dup"))
    job_id = uuid.uuid4().hex
    cf = user_cookiefile(session["user_id"]) if data.get("use_cookies") else None
    save_job(job_id, session["user_id"], "bulk", {"items": [], "done": 0, "total": len(urls)})
    t = threading.Thread(target=_run_bulk_job,
                         args=(job_id, session["user_id"], urls, cf),
                         kwargs={"fmt": fmt, "secs": secs,
                                 "concurrency": concurrency, "skip_dup": skip_dup},
                         daemon=True)
    t.start()
    return jsonify({"ok": True, "job_id": job_id})


@app.route("/api/job/<job_id>")
@login_required
def job_status(job_id):
    job = get_job(job_id, session["user_id"])
    if not job:
        return jsonify({"ok": False, "error": "Job nahi mili."}), 404
    data = dict(job["data"])
    for item in data.get("items", []):
        if item.get("file"):
            item["download_url"] = f"/api/files/{item.get('file_job') or job_id}/{item['file']}"
    return jsonify({"ok": True, "job_id": job_id, "status": job["status"],
                    "type": job["type"], "created_at": job["created_at"], **data})


@app.route("/api/jobs")
@login_required
def my_jobs():
    rows = db().execute(
        "SELECT id, type, status, data, created_at FROM jobs WHERE user_id = ? ORDER BY created_at DESC LIMIT 30",
        (session["user_id"],)).fetchall()
    out = []
    for r in rows:
        d = json.loads(r["data"] or "{}")
        out.append({"id": r["id"], "type": r["type"], "status": r["status"],
                    "created_at": r["created_at"],
                    "total": d.get("total", 0), "done": d.get("done", 0),
                    "items": [{k: i.get(k) for k in ("title", "file", "quality", "status", "error", "note")}
                              | ({"download_url": f"/api/files/{i.get('file_job') or r['id']}/{i['file']}"} if i.get("file") else {})
                              for i in d.get("items", [])]})
    return jsonify({"ok": True, "jobs": out})


@app.route("/api/files/<job_id>/<path:filename>")
@login_required
def job_file(job_id, filename):
    job = get_job(job_id, session["user_id"])
    if not job:
        return jsonify({"ok": False, "error": "File nahi mili."}), 404
    job_dir = os.path.join(JOBS_DIR, job_id)
    safe = os.path.basename(filename)
    path = os.path.join(job_dir, safe)
    if not os.path.isfile(path):
        return jsonify({"ok": False, "error": "File nahi mili."}), 404
    # download name mein asal title rakho agar item maloom ho
    dl = safe
    for item in job["data"].get("items", []):
        if item.get("file") == safe and item.get("title"):
            dl = clean_title(item["title"]) + os.path.splitext(safe)[1]
            break
    return send_file(path, as_attachment=True, download_name=dl)

# ---------------------------------------------------------------- clipper

def _temp_upload(f, suffix=""):
    fd, path = tempfile.mkstemp(dir=DOWNLOAD_DIR, suffix=suffix)
    os.close(fd)
    f.save(path)
    return path


@app.route("/api/clip", methods=["POST"])
@login_required
@plan_required
def clip():
    try:
        start = float(request.form.get("start") or 0)
        end = float(request.form.get("end") or 0)
    except ValueError:
        return jsonify({"ok": False, "error": "Start/end seconds sahi number mein do."}), 400
    if end <= start:
        return jsonify({"ok": False, "error": "End time, start time se zyada hona chahiye."}), 400
    if end - start > 3600:
        return jsonify({"ok": False, "error": "Clip zyada se zyada 1 ghante ka ho sakta hai."}), 400

    src = None
    cleanup_src = False
    f = request.files.get("video")
    if f and f.filename:
        src = _temp_upload(f, suffix=os.path.splitext(f.filename)[1] or ".mp4")
        cleanup_src = True
    else:
        job_id = (request.form.get("job_id") or "").strip()
        fname = os.path.basename(request.form.get("filename") or "")
        if job_id and fname and get_job(job_id, session["user_id"]):
            cand = os.path.join(JOBS_DIR, job_id, fname)
            if os.path.isfile(cand):
                src = cand
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400

    out_path = os.path.join(DOWNLOAD_DIR, f"clip-{uuid.uuid4().hex}.mp4")
    cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", src,
           "-t", str(end - start),
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if cleanup_src:
        try:
            os.remove(src)
        except OSError:
            pass
    if r.returncode != 0 or not os.path.isfile(out_path):
        return jsonify({"ok": False, "error": f"Clip nahi ban saka: {r.stderr[-200:]}"}), 422
    resp = send_file(out_path, as_attachment=True,
                     download_name=f"clip-{int(start)}-{int(end)}.mp4", mimetype="video/mp4")

    @resp.call_on_close
    def _cleanup():
        try:
            os.remove(out_path)
        except OSError:
            pass
    return resp

# ---------------------------------------------------------------- subtitles

_whisper_model = None
_whisper_lock = threading.Lock()


def _get_whisper():
    """faster-whisper load karo (aik dafa). Available na ho to None."""
    global _whisper_model
    if _whisper_model is not None:
        return _whisper_model
    with _whisper_lock:
        if _whisper_model is not None:
            return _whisper_model
        try:
            from faster_whisper import WhisperModel
            size = os.environ.get("CLIPKARO_WHISPER_MODEL", "base")
            _whisper_model = WhisperModel(size, device="cpu", compute_type="int8")
        except Exception:
            _whisper_model = False
    return _whisper_model or None


def _load_wav_array(path):
    """16kHz mono WAV ko numpy float32 array mein parho.
    (faster-whisper ko array dene se uska PyAV wala decode step skip ho
    jata hai, jo naye av versions se incompatible hai.)"""
    import wave
    import numpy as np
    with wave.open(path, "rb") as w:
        frames = w.readframes(w.getnframes())
    return np.frombuffer(frames, dtype=np.int16).astype(np.float32) / 32768.0


def _fmt_ts(sec):
    ms = int(round(sec * 1000))
    h, rem = divmod(ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


@app.route("/api/captions-status")
@login_required
def captions_status():
    try:
        import faster_whisper  # noqa: F401
        available = True
    except Exception:
        available = False
    return jsonify({"ok": True, "auto_captions": available})


@app.route("/api/subtitles", methods=["POST"])
@login_required
@plan_required
def subtitles():
    model = _get_whisper()
    if model is None:
        return jsonify({
            "ok": False, "auto_captions": False,
            "error": "Auto captions is server par available nahi hain (faster-whisper install/load nahi ho saka). "
                     "Apni .srt file se subtitles burn karna phir bhi chalega — 'Subtitles Burn' wala option use karo.",
        }), 501
    f = request.files.get("video")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "Video file upload karo."}), 400
    # translate=1 ho to captions English mein bante hain (whisper ka translate task)
    translate = _bool_int(request.form.get("translate"))
    src = _temp_upload(f, suffix=os.path.splitext(f.filename)[1] or ".mp4")
    wav = src + ".wav"
    srt_path = os.path.join(DOWNLOAD_DIR, f"subs-{uuid.uuid4().hex}.srt")
    try:
        r = subprocess.run(["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav],
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            return jsonify({"ok": False, "error": "Video se audio nahi nikal saka."}), 422
        if translate:
            segments, _info = model.transcribe(_load_wav_array(wav), task="translate")
        else:
            segments, _info = model.transcribe(_load_wav_array(wav))
        lines = []
        for i, seg in enumerate(segments, 1):
            lines.append(str(i))
            lines.append(f"{_fmt_ts(seg.start)} --> {_fmt_ts(seg.end)}")
            lines.append(seg.text.strip())
            lines.append("")
        with open(srt_path, "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines))
    except Exception as e:
        return jsonify({"ok": False, "error": f"Captions nahi ban sake: {str(e)[:200]}"}), 422
    finally:
        for p in (src, wav):
            try:
                os.remove(p)
            except OSError:
                pass
    resp = send_file(srt_path, as_attachment=True, download_name="captions.srt",
                     mimetype="text/plain")

    @resp.call_on_close
    def _cleanup():
        try:
            os.remove(srt_path)
        except OSError:
            pass
    return resp


@app.route("/api/burn-subtitles", methods=["POST"])
@login_required
@plan_required
def burn_subtitles():
    vf = request.files.get("video")
    sf = request.files.get("srt")
    if not vf or not vf.filename:
        return jsonify({"ok": False, "error": "Video file upload karo."}), 400
    if not sf or not sf.filename:
        return jsonify({"ok": False, "error": ".srt file upload karo."}), 400
    src = _temp_upload(vf, suffix=os.path.splitext(vf.filename)[1] or ".mp4")
    srt = _temp_upload(sf, suffix=".srt")
    out_path = os.path.join(DOWNLOAD_DIR, f"subbed-{uuid.uuid4().hex}.mp4")
    # caption style — #RRGGBB ko ASS ke &H00BBGGRR mein badlo
    def ass_color(val, default):
        h = (val or default).strip().lstrip("#")
        if len(h) != 6 or any(c not in "0123456789abcdefABCDEF" for c in h):
            h = default.lstrip("#")
        return "&H00" + h[4:6] + h[2:4] + h[0:2]
    primary = ass_color(request.form.get("color"), "#FFFFFF")
    outline = ass_color(request.form.get("outline"), "#000000")
    try:
        alignment = int(request.form.get("position") or 2)
    except ValueError:
        alignment = 2
    if alignment not in (2, 3, 8):
        alignment = 2
    # libass ka SRT->ASS raasta purani SSA numbering use karta hai — top-center
    # ke liye 8 ki bajaye 6 dena parta hai (empirically verify kiya gaya:
    # 2=bottom-center, 3=bottom-right, 6=top-center is filter mein).
    alignment = {2: 2, 3: 3, 8: 6}[alignment]
    try:
        fontsize = max(14, min(30, int(request.form.get("fontsize") or 18)))
    except ValueError:
        fontsize = 18
    force_style = (f"FontSize={fontsize},PrimaryColour={primary},"
                   f"OutlineColour={outline},Alignment={alignment},"
                   "BorderStyle=1,Outline=1,Shadow=0")
    # subtitles filter ko path escape karna parta hai
    esc = srt.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    cmd = ["ffmpeg", "-y", "-i", src, "-vf",
           f"subtitles='{esc}':force_style='{force_style}'",
           "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
           "-c:a", "aac", "-b:a", "160k", "-movflags", "+faststart", out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    for p in (src, srt):
        try:
            os.remove(p)
        except OSError:
            pass
    if r.returncode != 0 or not os.path.isfile(out_path):
        return jsonify({"ok": False, "error": f"Subtitles burn nahi ho sake: {r.stderr[-200:]}"}), 422
    resp = send_file(out_path, as_attachment=True, download_name="video-with-subtitles.mp4",
                     mimetype="video/mp4")

    @resp.call_on_close
    def _cleanup():
        try:
            os.remove(out_path)
        except OSError:
            pass
    return resp

# ---------------------------------------------------------------- profiles

PLATFORMS = ("Facebook", "YouTube", "TikTok", "Instagram", "Other")


def _profile_dict(row):
    return {"id": row["id"], "name": row["name"], "platform": row["platform"],
            "url": row["url"], "captions_on": bool(row["captions_on"]),
            "brand_on": bool(row["brand_on"]), "brand_text": row["brand_text"],
            "clips_on": bool(row["clips_on"]), "clip_count": row["clip_count"],
            "created": row["created"]}


def _bool_int(v):
    return 1 if v in (1, True, "1", "true", "True", "on", "yes") else 0


@app.route("/api/profiles", methods=["GET"])
@login_required
def profiles_list():
    rows = db().execute(
        "SELECT * FROM profiles WHERE user_id = ? ORDER BY id DESC",
        (session["user_id"],)).fetchall()
    return jsonify({"ok": True, "profiles": [_profile_dict(r) for r in rows]})


@app.route("/api/profiles", methods=["POST"])
@login_required
@plan_required
def profiles_add():
    data = request.get_json(force=True, silent=True) or {}
    name = (data.get("name") or "").strip()
    url = (data.get("url") or "").strip()
    platform = (data.get("platform") or "Other").strip()
    if platform not in PLATFORMS:
        platform = "Other"
    if not name:
        return jsonify({"ok": False, "error": "Profile ka naam do."}), 400
    if not URL_RE.match(url):
        return jsonify({"ok": False, "error": "Sahi URL do (http/https)."}), 400
    try:
        clip_count = max(0, min(10, int(data.get("clip_count") or 0)))
    except (TypeError, ValueError):
        clip_count = 0
    cur = db().execute(
        "INSERT INTO profiles (user_id, name, platform, url, captions_on, brand_on,"
        " brand_text, clips_on, clip_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (session["user_id"], name, platform, url,
         _bool_int(data.get("captions_on")), _bool_int(data.get("brand_on")),
         (data.get("brand_text") or "").strip(),
         _bool_int(data.get("clips_on")), clip_count))
    db().commit()
    row = db().execute("SELECT * FROM profiles WHERE id = ?", (cur.lastrowid,)).fetchone()
    return jsonify({"ok": True, "profile": _profile_dict(row)})


@app.route("/api/profiles/delete", methods=["POST"])
@login_required
@plan_required
def profiles_delete():
    data = request.get_json(force=True, silent=True) or {}
    try:
        pid = int(data.get("id"))
    except (TypeError, ValueError):
        return jsonify({"ok": False, "error": "Profile id sahi do."}), 400
    cur = db().execute("DELETE FROM profiles WHERE id = ? AND user_id = ?",
                       (pid, session["user_id"]))
    db().commit()
    if not cur.rowcount:
        return jsonify({"ok": False, "error": "Profile nahi mila."}), 404
    return jsonify({"ok": True})


@app.route("/api/profiles/import", methods=["POST"])
@login_required
@plan_required
def profiles_import():
    text = None
    f = request.files.get("csv") or request.files.get("file")
    if f and f.filename:
        text = f.read().decode("utf-8-sig", errors="replace")
    elif request.form.get("csv"):
        text = request.form["csv"]
    else:
        data = request.get_json(force=True, silent=True) or {}
        text = data.get("csv")
    if not text or not text.strip():
        return jsonify({"ok": False, "error": "CSV file ya CSV text do."}), 400
    reader = csv.DictReader(io.StringIO(text))
    if not reader.fieldnames:
        return jsonify({"ok": False, "error": "CSV samajh nahi aaya — pehli line mein name,platform,url hon."}), 400
    added, skipped = 0, 0
    for raw in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
        name, url = row.get("name", ""), row.get("url", "")
        if not name or not URL_RE.match(url):
            skipped += 1
            continue
        platform = row.get("platform") or "Other"
        if platform not in PLATFORMS:
            platform = "Other"
        try:
            clip_count = max(0, min(10, int(row.get("clip_count") or 0)))
        except ValueError:
            clip_count = 0
        db().execute(
            "INSERT INTO profiles (user_id, name, platform, url, captions_on, brand_on,"
            " brand_text, clips_on, clip_count) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (session["user_id"], name, platform, url,
             _bool_int(row.get("captions_on")), _bool_int(row.get("brand_on")),
             row.get("brand_text", ""), _bool_int(row.get("clips_on")), clip_count))
        added += 1
    db().commit()
    return jsonify({"ok": True, "added": added, "skipped": skipped})


@app.route("/api/profiles/export")
@login_required
def profiles_export():
    rows = db().execute(
        "SELECT * FROM profiles WHERE user_id = ? ORDER BY id",
        (session["user_id"],)).fetchall()
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["name", "platform", "url", "captions_on", "brand_on",
                "brand_text", "clips_on", "clip_count"])
    for r in rows:
        w.writerow([r["name"], r["platform"], r["url"], r["captions_on"],
                    r["brand_on"], r["brand_text"], r["clips_on"], r["clip_count"]])
    return send_file(io.BytesIO(buf.getvalue().encode("utf-8")),
                     as_attachment=True, download_name="clipkaro-profiles.csv",
                     mimetype="text/csv")

# ---------------------------------------------------------------- video effects

@app.route("/api/effects", methods=["POST"])
@login_required
@plan_required
def effects():
    src, cleanup = _resolve_source()
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400

    def fnum(name, default, lo, hi):
        try:
            return max(lo, min(hi, float(request.form.get(name))))
        except (TypeError, ValueError):
            return default

    brightness = fnum("brightness", 0.0, -0.3, 0.3)
    contrast = fnum("contrast", 1.0, 0.7, 1.4)
    saturation = fnum("saturation", 1.0, 0.0, 2.0)
    flip = (request.form.get("flip") or "none").strip()
    try:
        rotate = int(request.form.get("rotate") or 0)
    except ValueError:
        rotate = 0
    if rotate not in (0, 90, 180, 270):
        rotate = 0
    crop_on = _bool_int(request.form.get("crop_on"))
    crop_pct = fnum("crop_pct", 10.0, 0.0, 45.0)

    # --- Flixier-style upgrades: filter preset, speed, reverse ---
    preset = (request.form.get("preset") or "none").strip()
    PRESETS = {
        # warm/cool: colorbalance se halki si garam/thandi tone
        "warm": "colorbalance=rm=0.12:gm=0.04:bm=-0.12:rs=0.10:bs=-0.10",
        "cool": "colorbalance=rm=-0.10:bm=0.12:rs=-0.08:bs=0.10",
        "mono": "hue=s=0",
        "vintage": "curves=preset=vintage,eq=saturation=0.75",
        "vibrant": "eq=saturation=1.5:contrast=1.08",
    }
    try:
        speed = float(request.form.get("speed") or 1)
    except ValueError:
        speed = 1.0
    if speed not in (0.5, 0.75, 1.0, 1.25, 1.5, 2.0):
        speed = 1.0
    reverse = _bool_int(request.form.get("reverse"))
    if reverse:
        dur = probe_duration(src)
        if dur is not None and dur > 60:
            if cleanup:
                try:
                    os.remove(src)
                except OSError:
                    pass
            return jsonify({"ok": False, "error": "Reverse sirf 60 seconds tak ki "
                            "video par lag sakta hai — ye video lambi hai, pehle "
                            "Clipper se chhota hissa kaat lo."}), 400

    filters = []
    if crop_on and crop_pct > 0:
        frac = 1 - 2 * crop_pct / 100.0
        filters.append(f"crop=iw*{frac:.4f}:ih*{frac:.4f}")
    if preset in PRESETS:
        filters.append(PRESETS[preset])
    filters.append(f"eq=brightness={brightness}:contrast={contrast}:saturation={saturation}")
    if flip == "h":
        filters.append("hflip")
    elif flip == "v":
        filters.append("vflip")
    if rotate == 90:
        filters.append("transpose=1")
    elif rotate == 270:
        filters.append("transpose=2")
    elif rotate == 180:
        filters += ["hflip", "vflip"]
    if reverse:
        filters.append("reverse")
    if speed != 1.0:
        filters.append(f"setpts=PTS/{speed}")
    vf = ",".join(filters)

    afilters = []
    if probe_has_audio(src):
        if reverse:
            afilters.append("areverse")
        if speed != 1.0:
            # atempo aik step mein 0.5–2 hi sambhalta hai — chain banao
            v = speed
            while v > 2.0:
                afilters.append("atempo=2.0")
                v /= 2.0
            while v < 0.5:
                afilters.append("atempo=0.5")
                v /= 0.5
            afilters.append(f"atempo={v}")
    af = ",".join(afilters) if afilters else None
    title = _source_title("Video") + " (effects)"

    def build_fx(out):
        cmd = ["ffmpeg", "-y", "-i", src, "-vf", vf]
        if af:
            cmd += ["-af", af]
        return cmd + ENC_ARGS + [out]

    result, err = _run_ffmpeg_job(session["user_id"], "effect", title, build_fx)
    if cleanup:
        try:
            os.remove(src)
        except OSError:
            pass
    if err:
        return jsonify({"ok": False, "error": f"Effects nahi lag sake: {err}"}), 422
    return jsonify({"ok": True, **result})

# ---------------------------------------------------------------- brand overlay

@app.route("/api/overlay", methods=["POST"])
@login_required
@plan_required
def overlay():
    src, cleanup = _resolve_source()
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400
    text = (request.form.get("text") or "").strip()
    text_pos = (request.form.get("text_pos") or "br").strip()
    if text_pos not in TEXT_XY:
        text_pos = "br"
    logo_pos = (request.form.get("logo_pos") or "br").strip()
    if logo_pos not in LOGO_XY:
        logo_pos = "br"

    def fnum(name, default, lo, hi):
        try:
            return max(lo, min(hi, float(request.form.get(name))))
        except (TypeError, ValueError):
            return default

    text_opacity = fnum("text_opacity", 0.9, 0.1, 1.0)
    text_size = fnum("text_size", 36, 12, 120)
    logo_opacity = fnum("logo_opacity", 1.0, 0.1, 1.0)
    logo_scale = fnum("logo_scale", 20, 2, 90)

    logo_path = None
    lf = request.files.get("logo")
    if lf and lf.filename:
        logo_path = _temp_upload(lf, suffix=os.path.splitext(lf.filename)[1] or ".png")
    if not text and not logo_path:
        return jsonify({"ok": False, "error": "Text ya logo — kam az kam aik do."}), 400

    textfile = _write_textfile(text) if text else None
    vw, _vh = probe_dims(src)
    fc_parts = []
    cur = "[0:v]"
    if logo_path:
        logo_w = max(16, int((vw or 640) * logo_scale / 100.0))
        fc_parts.append(f"[1:v]scale={logo_w}:-1,format=rgba,"
                        f"colorchannelmixer=aa={logo_opacity:.2f}[logo]")
        lx, ly = LOGO_XY[logo_pos]
        fc_parts.append(f"{cur}[logo]overlay={lx}:{ly}[vlogo]")
        cur = "[vlogo]"
    if textfile:
        fc_parts.append(f"{cur}{_drawtext(textfile, text_pos, text_size, text_opacity)}[vout]")
        cur = "[vout]"
    title = _source_title("Video") + " (branded)"
    cmd_inputs = ["-i", src] + (["-i", logo_path] if logo_path else [])

    def build(out):
        return (["ffmpeg", "-y"] + cmd_inputs +
                ["-filter_complex", ";".join(fc_parts),
                 "-map", cur, "-map", "0:a?"] + ENC_ARGS + [out])

    result, err = _run_ffmpeg_job(session["user_id"], "overlay", title, build)
    for p in ([src] if cleanup else []) + [logo_path, textfile]:
        if p:
            try:
                os.remove(p)
            except OSError:
                pass
    if err:
        return jsonify({"ok": False, "error": f"Overlay nahi lag saka: {err}"}), 422
    return jsonify({"ok": True, **result})

# ---------------------------------------------------------------- auto clips

@app.route("/api/autoclips", methods=["POST"])
@login_required
@plan_required
def autoclips():
    try:
        n = int(request.form.get("count") or 1)
    except ValueError:
        n = 1
    n = max(1, min(10, n))
    try:
        dur = float(request.form.get("duration") or 60)
    except ValueError:
        dur = 60.0
    if dur <= 0:
        return jsonify({"ok": False, "error": "Clip duration 0 se zyada ho."}), 400
    ratio = (request.form.get("ratio") or "original").strip()
    vf_ratio = _ratio_vf({"portrait": "vertical", "landscape": "horizontal",
                          "vertical": "vertical", "horizontal": "horizontal"}.get(ratio, ""))
    src_job_id = (request.form.get("job_id") or "").strip()
    src_fname = os.path.basename(request.form.get("filename") or "")
    src, cleanup = _resolve_source()
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400
    user = current_user()
    wm_on = _bool_int(request.form.get("wm_on"))
    wm_text = (request.form.get("wm_text") or "").strip()
    if wm_on and not wm_text:
        wm_text = user["username"] if user else "ClipKaro"
    delete_original = _bool_int(request.form.get("delete_original"))
    base_title = _source_title("Video")

    textfile = _write_textfile(wm_text) if wm_on else None
    vf_parts = []
    if vf_ratio:
        vf_parts.append(vf_ratio)
    if textfile:
        vf_parts.append(_drawtext(textfile, "br", 36, 0.85))
    vf = ",".join(vf_parts) if vf_parts else None

    created, errors = [], []
    for i in range(n):
        start = i * dur
        title = f"{base_title} Part {i + 1}"

        def build(out, start=start, vf=vf):
            cmd = ["ffmpeg", "-y", "-ss", str(start), "-i", src, "-t", str(dur)]
            if vf:
                cmd += ["-vf", vf]
            return cmd + ENC_ARGS + [out]

        result, err = _run_ffmpeg_job(session["user_id"], "clip", title, build)
        if result:
            created.append(result)
        else:
            errors.append({"part": i + 1, "error": err})
    deleted = False
    if delete_original and not cleanup and src_job_id and src_fname:
        cand = os.path.join(JOBS_DIR, src_job_id, src_fname)
        if os.path.isfile(cand):
            try:
                os.remove(cand)
            except OSError:
                pass
        db().execute("DELETE FROM jobs WHERE id = ? AND user_id = ?",
                     (src_job_id, session["user_id"]))
        db().commit()
        deleted = True
    if cleanup:
        try:
            os.remove(src)
        except OSError:
            pass
    if textfile:
        try:
            os.remove(textfile)
        except OSError:
            pass
    if not created:
        return jsonify({"ok": False, "error": "Koi clip nahi ban saka.",
                        "errors": errors}), 422
    return jsonify({"ok": True, "created": created,
                    "job_ids": [c["job_id"] for c in created],
                    "errors": errors, "deleted_original": deleted})

# ---------------------------------------------------------------- usage dashboard

@app.route("/api/stats")
@login_required
def stats():
    uid = session["user_id"]
    total_jobs = db().execute("SELECT COUNT(*) AS c FROM jobs WHERE user_id = ?",
                              (uid,)).fetchone()["c"]
    srow = db().execute("SELECT downloads, bytes, tiktok_uploads FROM stats WHERE user_id = ?",
                        (uid,)).fetchone()
    downloads = srow["downloads"] if srow else 0
    nbytes = srow["bytes"] if srow else 0
    tiktok_uploads = srow["tiktok_uploads"] if srow else 0
    by_type = {}
    for r in db().execute("SELECT type, COUNT(*) AS c FROM jobs WHERE user_id = ? GROUP BY type",
                          (uid,)).fetchall():
        by_type[r["type"]] = r["c"]
    failed = 0
    domains = {}
    for r in db().execute("SELECT data FROM jobs WHERE user_id = ?", (uid,)).fetchall():
        try:
            d = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        for it in d.get("items", []):
            if it.get("status") == "error":
                failed += 1
            u = it.get("url")
            if u:
                net = urlparse(u).netloc.lower().removeprefix("www.") or "unknown"
                domains[net] = domains.get(net, 0) + 1
    top_sources = [{"source": k, "count": v} for k, v in
                   sorted(domains.items(), key=lambda kv: -kv[1])[:8]]
    return jsonify({"ok": True, "total_jobs": total_jobs,
                    "downloads": downloads, "tiktok_uploads": tiktok_uploads,
                    "downloaded_bytes": nbytes,
                    "downloaded_human": human_size(nbytes),
                    "failed": failed, "by_type": by_type,
                    "top_sources": top_sources})

# ---------------------------------------------------------------- AI voiceover

EDGE_VOICES = [
    "ur-PK-UzmaNeural", "ur-PK-AsadNeural",
    "hi-IN-SwaraNeural", "hi-IN-MadhurNeural",
    "en-US-AriaNeural", "en-US-GuyNeural",
    "en-GB-SoniaNeural", "en-IN-NeerjaNeural",
]
_edge_ctx_ready = False


def _edge_synthesize(text, voice, out_path):
    """edge-tts (Microsoft ki online awazein) se mp3 banao.
    Kuch networks par darmiyan mein proxy apna CA laga deta hai — certifi ke
    sath system CA bundle mila kar SSL context banao taake dono jagah chale."""
    global _edge_ctx_ready
    import asyncio
    import ssl
    import edge_tts
    if not _edge_ctx_ready:
        try:
            import certifi
            import edge_tts.communicate as _comm
            parts = []
            for p in (certifi.where(), ssl.get_default_verify_paths().cafile):
                if p and os.path.exists(p):
                    with open(p, "rb") as fh:
                        parts.append(fh.read())
            if parts:
                bundle = os.path.join(DATA_DIR, "ca-bundle.pem")
                with open(bundle, "wb") as fh:
                    fh.write(b"\n".join(parts))
                _comm._SSL_CTX = ssl.create_default_context(cafile=bundle)
        except Exception:
            pass
        _edge_ctx_ready = True
    proxy = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy") or None

    async def _main():
        comm = edge_tts.Communicate(text, voice, proxy=proxy)
        await asyncio.wait_for(comm.save(out_path), timeout=120)

    asyncio.run(_main())


def _gtts_synthesize(text, lang, out_path):
    from gtts import gTTS
    gTTS(text=text, lang=lang).save(out_path)


@app.route("/api/voiceover", methods=["POST"])
@login_required
@plan_required
def voiceover():
    data = request.get_json(force=True, silent=True) or {}
    text = (data.get("text") or "").strip()
    voice = (data.get("voice") or "ur-PK-UzmaNeural").strip()
    if not text:
        return jsonify({"ok": False, "error": "Pehle text likho jo bolna hai."}), 400
    if len(text) > 2000:
        return jsonify({"ok": False, "error": "Text zyada lamba hai — zyada se zyada 2000 characters rakho."}), 400
    if voice not in EDGE_VOICES:
        return jsonify({"ok": False, "error": "Voice sahi select karo."}), 400
    job_id = uuid.uuid4().hex
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    out_path = os.path.join(job_dir, "000.mp3")
    engine, err_edge = None, None
    try:
        _edge_synthesize(text, voice, out_path)
        if not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
            raise RuntimeError("edge-tts se khali file bani")
        engine = "Microsoft Edge (online awaz)"
    except Exception as e:
        err_edge = str(e)[:150]
        try:
            os.remove(out_path)
        except OSError:
            pass
        # Fallback: Google TTS (zuban voice ke pehle hisse se: ur/hi/en)
        lang = voice.split("-")[0].lower()
        if lang not in ("ur", "hi", "en"):
            lang = "en"
        try:
            _gtts_synthesize(text, lang, out_path)
            engine = "Google TTS (fallback)"
        except Exception as e2:
            shutil.rmtree(job_dir, ignore_errors=True)
            return jsonify({"ok": False,
                            "error": f"Voiceover nahi ban saka. Edge: {err_edge} | Google: {str(e2)[:150]}"}), 422
    result = _register_file_job(session["user_id"], "voiceover",
                                f"Voiceover ({voice})", job_dir, out_path)
    result["engine"] = engine
    return jsonify({"ok": True, **result})


def _resolve_job_file(job_id, filename):
    """User ki kisi job ki file ka path (voiceover audio / doosri video ke liye)."""
    job_id = (job_id or "").strip()
    fname = os.path.basename(filename or "")
    if job_id and fname and get_job(job_id, session["user_id"]):
        cand = os.path.join(JOBS_DIR, job_id, fname)
        if os.path.isfile(cand):
            return cand
    return None


@app.route("/api/voiceover-add", methods=["POST"])
@login_required
@plan_required
def voiceover_add():
    vsrc, vclean = _resolve_source()
    if not vsrc:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400
    asrc, aclean = None, False
    af = request.files.get("audio")
    if af and af.filename:
        asrc = _temp_upload(af, suffix=os.path.splitext(af.filename)[1] or ".mp3")
        aclean = True
    else:
        asrc = _resolve_job_file(request.form.get("audio_job_id"),
                                 request.form.get("audio_filename"))
    if not asrc:
        if vclean:
            try:
                os.remove(vsrc)
            except OSError:
                pass
        return jsonify({"ok": False, "error": "Audio file upload karo ya voiceover job ki file select karo."}), 400
    mode = (request.form.get("mode") or "replace").strip()
    if mode not in ("replace", "mix"):
        mode = "replace"
    try:
        bg_volume = max(0.0, min(1.0, float(request.form.get("bg_volume") or 0.3)))
    except ValueError:
        bg_volume = 0.3
    title = _source_title("Video") + " (voiceover)"
    has_audio = probe_has_audio(vsrc)

    def build_replace(out):
        return (["ffmpeg", "-y", "-i", vsrc, "-i", asrc,
                 "-map", "0:v:0", "-map", "1:a:0", "-shortest"] + ENC_ARGS + [out])

    def build_mix(out):
        fc = (f"[0:a]volume={bg_volume:.2f}[bgm];[1:a]volume=1.0[vo];"
              "[bgm][vo]amix=inputs=2:duration=first:dropout_transition=0[aout]")
        return (["ffmpeg", "-y", "-i", vsrc, "-i", asrc,
                 "-filter_complex", fc, "-map", "0:v:0", "-map", "[aout]"]
                + ENC_ARGS + [out])

    build = build_mix if (mode == "mix" and has_audio) else build_replace
    result, err = _run_ffmpeg_job(session["user_id"], "voiceover", title, build)
    for p, cl in ((vsrc, vclean), (asrc, aclean)):
        if cl and p:
            try:
                os.remove(p)
            except OSError:
                pass
    if err:
        return jsonify({"ok": False, "error": f"Voiceover nahi lag saka: {err}"}), 422
    return jsonify({"ok": True, **result})

# ---------------------------------------------------------------- editor tools

def _hex6(val, default):
    """'#rrggbb' ya 'rrggbb' ko saaf 6-hex mein; ghalat ho to default."""
    h = (val or "").strip().lstrip("#")
    if len(h) == 6 and all(c in "0123456789abcdefABCDEF" for c in h):
        return h.lower()
    return default


def _resolve_source2():
    """Doosri video (transition ke liye): file field 'video2' ya
    form 'job_id2' + 'filename2'."""
    f = request.files.get("video2")
    if f and f.filename:
        return _temp_upload(f, suffix=os.path.splitext(f.filename)[1] or ".mp4"), True
    path = _resolve_job_file(request.form.get("job_id2"),
                             request.form.get("filename2"))
    return path, False


@app.route("/api/title", methods=["POST"])
@login_required
@plan_required
def title_text():
    src, cleanup = _resolve_source()
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400
    text = (request.form.get("text") or "").strip()
    if not text:
        if cleanup:
            try:
                os.remove(src)
            except OSError:
                pass
        return jsonify({"ok": False, "error": "Title ka text likho."}), 400

    def fnum(name, default, lo, hi):
        try:
            return max(lo, min(hi, float(request.form.get(name))))
        except (TypeError, ValueError):
            return default

    start = fnum("start", 0.0, 0.0, 86400.0)
    end = fnum("end", 3.0, 0.0, 86400.0)
    if end <= start:
        end = start + 3.0
    size = int(fnum("size", 48, 16, 160))
    color = _hex6(request.form.get("color"), "ffffff")
    box_on = _bool_int(request.form.get("box"))
    box_color = _hex6(request.form.get("box_color"), "000000")
    pos = (request.form.get("position") or "top").strip()
    XY = {"top": ("(w-text_w)/2", "30"),
          "center": ("(w-text_w)/2", "(h-text_h)/2"),
          "bottom": ("(w-text_w)/2", "h-text_h-30")}
    x, y = XY.get(pos, XY["top"])
    textfile = _write_textfile(text)
    esc = textfile.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    parts = []
    if FONT_FILE:
        parts.append(f"fontfile='{FONT_FILE}'")
    parts += [f"textfile='{esc}'", f"fontcolor=0x{color}", f"fontsize={size}",
              f"x={x}", f"y={y}",
              f"enable='between(t,{start:.3f},{end:.3f})'"]
    if box_on:
        parts += ["box=1", f"boxcolor=0x{box_color}@0.55", "boxborderw=12"]
    vf = "drawtext=" + ":".join(parts)
    title = _source_title("Video") + " (title)"
    result, err = _run_ffmpeg_job(
        session["user_id"], "title", title,
        lambda out: ["ffmpeg", "-y", "-i", src, "-vf", vf] + ENC_ARGS + [out])
    for p in ([src] if cleanup else []) + [textfile]:
        try:
            os.remove(p)
        except OSError:
            pass
    if err:
        return jsonify({"ok": False, "error": f"Title nahi lag saka: {err}"}), 422
    return jsonify({"ok": True, **result})


@app.route("/api/transition", methods=["POST"])
@login_required
@plan_required
def transition():
    src1, clean1 = _resolve_source()
    if not src1:
        return jsonify({"ok": False, "error": "Pehli video upload karo ya job file select karo."}), 400
    src2, clean2 = _resolve_source2()
    if not src2:
        if clean1:
            try:
                os.remove(src1)
            except OSError:
                pass
        return jsonify({"ok": False, "error": "Doosri video upload karo ya job file select karo."}), 400
    name = (request.form.get("transition") or "fade").strip()
    ALLOWED = ("none", "fade", "fadeblack", "wipeleft", "slideright",
               "circleopen", "dissolve")
    if name not in ALLOWED:
        name = "fade"
    try:
        dur_t = float(request.form.get("duration") or 1)
    except ValueError:
        dur_t = 1.0
    if dur_t not in (0.5, 1.0, 2.0):
        dur_t = 1.0
    d1, d2 = probe_duration(src1), probe_duration(src2)
    w1, h1 = probe_dims(src1)
    w2, h2 = probe_dims(src2)
    err = None
    if not d1 or not d2:
        err = "Kisi video ki duration nahi parhi ja saki."
    elif name != "none" and (dur_t >= d1 or dur_t >= d2):
        err = "Transition ki duration dono videos se chhoti honi chahiye."
    if err:
        for p, cl in ((src1, clean1), (src2, clean2)):
            if cl:
                try:
                    os.remove(p)
                except OSError:
                    pass
        return jsonify({"ok": False, "error": err}), 400
    W = max(w1 or 640, w2 or 640)
    H = max(h1 or 360, h2 or 360)
    W += W % 2
    H += H % 2

    inputs = ["-i", src1, "-i", src2]
    fc = []
    for i in (0, 1):
        fc.append(f"[{i}:v]scale={W}:{H}:force_original_aspect_ratio=decrease,"
                  f"pad={W}:{H}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps=30,settb=AVTB,"
                  f"format=yuv420p[v{i}]")
    # audio normalize — jis video mein awaz nahi, uske liye khamoshi banao
    next_idx = 2
    for i, (s, d) in enumerate(((src1, d1), (src2, d2))):
        if probe_has_audio(s):
            fc.append(f"[{i}:a]aformat=sample_fmts=fltp:sample_rates=48000:"
                      f"channel_layouts=stereo,asetpts=PTS-STARTPTS[a{i}]")
        else:
            inputs += ["-f", "lavfi", "-i",
                       "anullsrc=channel_layouts=stereo:sample_rate=48000"]
            fc.append(f"[{next_idx}:a]atrim=start=0:end={d:.3f},asetpts=PTS-STARTPTS,"
                      f"aformat=sample_fmts=fltp:sample_rates=48000:"
                      f"channel_layouts=stereo[a{i}]")
            next_idx += 1
    if name == "none":
        fc.append("[v0][a0][v1][a1]concat=n=2:v=1:a=1[vout][aout]")
    else:
        off = max(0.0, d1 - dur_t)
        fc.append(f"[v0][v1]xfade=transition={name}:duration={dur_t}:"
                  f"offset={off:.3f}[vout]")
        fc.append(f"[a0][a1]acrossfade=d={dur_t}[aout]")

    def build(out):
        return (["ffmpeg", "-y"] + inputs +
                ["-filter_complex", ";".join(fc),
                 "-map", "[vout]", "-map", "[aout]"] + ENC_ARGS + [out])

    result, err = _run_ffmpeg_job(session["user_id"], "join",
                                  "Joined Video (transition)", build)
    for p, cl in ((src1, clean1), (src2, clean2)):
        if cl:
            try:
                os.remove(p)
            except OSError:
                pass
    if err:
        return jsonify({"ok": False, "error": f"Videos join nahi ho sakeen: {err}"}), 422
    return jsonify({"ok": True, **result})


@app.route("/api/enhance", methods=["POST"])
@login_required
@plan_required
def enhance():
    src, cleanup = _resolve_source()
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400
    try:
        target = int(request.form.get("target") or 1080)
    except ValueError:
        target = 1080
    if target not in (720, 1080, 2160):
        target = 1080
    sharpen = _bool_int(request.form.get("sharpen"))
    auto = _bool_int(request.form.get("auto"))
    _w, sh = probe_dims(src)
    filters = []
    if sh and sh < target:
        filters.append(f"scale=-2:{target}")
    if auto:
        filters.append("eq=contrast=1.08:saturation=1.15:brightness=0.02")
    if sharpen:
        filters.append("unsharp=5:5:1.0:5:5:0.0")
    title = _source_title("Video") + " (enhanced)"

    def build(out):
        cmd = ["ffmpeg", "-y", "-i", src]
        if filters:
            cmd += ["-vf", ",".join(filters)]
        return cmd + ENC_ARGS + [out]

    result, err = _run_ffmpeg_job(session["user_id"], "enhance", title, build)
    if cleanup:
        try:
            os.remove(src)
        except OSError:
            pass
    if err:
        return jsonify({"ok": False, "error": f"Enhance nahi ho saka: {err}"}), 422
    return jsonify({"ok": True, **result})


@app.route("/api/audiofix", methods=["POST"])
@login_required
@plan_required
def audiofix():
    src, cleanup = _resolve_source()
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400
    denoise = _bool_int(request.form.get("denoise"))
    normalize = _bool_int(request.form.get("normalize"))
    if not denoise and not normalize:
        if cleanup:
            try:
                os.remove(src)
            except OSError:
                pass
        return jsonify({"ok": False, "error": "Kam az kam aik option select karo — Denoise ya Normalize."}), 400
    if not probe_has_audio(src):
        if cleanup:
            try:
                os.remove(src)
            except OSError:
                pass
        return jsonify({"ok": False, "error": "Is video mein audio hi nahi hai."}), 400
    af = []
    if denoise:
        af.append("afftdn=nf=-25")
    if normalize:
        af.append("loudnorm=I=-16:TP=-1.5:LRA=11")
    af_str = ",".join(af)
    title = _source_title("Video") + " (audio fixed)"

    def build_copy(out):
        return ["ffmpeg", "-y", "-i", src, "-af", af_str,
                "-c:v", "copy", "-c:a", "aac", "-b:a", "160k",
                "-movflags", "+faststart", out]

    def build_enc(out):
        return ["ffmpeg", "-y", "-i", src, "-af", af_str] + ENC_ARGS + [out]

    result, err = _run_ffmpeg_job(session["user_id"], "audiofix", title, build_copy)
    if err:
        # video copy na ho sake (ajeeb codec) to re-encode kar ke dobara try
        result, err = _run_ffmpeg_job(session["user_id"], "audiofix", title, build_enc)
    if cleanup:
        try:
            os.remove(src)
        except OSError:
            pass
    if err:
        return jsonify({"ok": False, "error": f"Audio fix nahi ho saka: {err}"}), 422
    return jsonify({"ok": True, **result})


@app.route("/api/silence-cut", methods=["POST"])
@login_required
@plan_required
def silence_cut():
    src, cleanup = _resolve_source()
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400

    def _fail(msg, code=400):
        if cleanup:
            try:
                os.remove(src)
            except OSError:
                pass
        return jsonify({"ok": False, "error": msg}), code

    dur = probe_duration(src)
    if not dur:
        return _fail("Video ki duration nahi parhi ja saki.")
    if dur > 900:
        return _fail("Silence Cut sirf 15 minute tak ki video par lag sakta hai — pehle Clipper se hissa kaat lo.")
    r = subprocess.run(
        ["ffmpeg", "-i", src, "-af", "silencedetect=noise=-35dB:d=0.6",
         "-f", "null", "-"],
        capture_output=True, text=True, timeout=1800)
    silences = []
    cur_start = None
    for m in re.finditer(r"silence_(start|end):\s*([\d.]+)", r.stderr or ""):
        if m.group(1) == "start":
            cur_start = float(m.group(2))
        elif cur_start is not None:
            silences.append((cur_start, float(m.group(2))))
            cur_start = None
    if cur_start is not None:
        silences.append((cur_start, dur))
    if not silences:
        return _fail("Is video mein koi lambi khamoshi (silence) nahi mili — video pehle se hi tight hai.", 422)
    # keep segments: khamoshi ke dono taraf 0.1s ki padding rakho
    keeps = []
    cursor = 0.0
    for s, e in silences:
        kend = min(s + 0.1, dur)
        if kend - cursor > 0.05:
            keeps.append((cursor, kend))
        cursor = max(cursor, e - 0.1)
    if dur - cursor > 0.05:
        keeps.append((cursor, dur))
    keeps = [(max(0.0, s), min(dur, e)) for s, e in keeps if e - s > 0.05]
    if not keeps:
        return _fail("Poori video hi khamoshi lagti hai — kuch bacha hi nahi kaatne ko.", 422)
    if len(keeps) > 300:
        return _fail("Video mein bahut zyada chhoti khamoshiyan hain — ye tool is par kaam nahi karega.", 422)
    has_audio = probe_has_audio(src)
    fc = []
    for i, (s, e) in enumerate(keeps):
        fc.append(f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{i}]")
        if has_audio:
            fc.append(f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{i}]")
    if has_audio:
        ins = "".join(f"[v{i}][a{i}]" for i in range(len(keeps)))
        fc.append(f"{ins}concat=n={len(keeps)}:v=1:a=1[vout][aout]")
        maps = ["-map", "[vout]", "-map", "[aout]"]
    else:
        ins = "".join(f"[v{i}]" for i in range(len(keeps)))
        fc.append(f"{ins}concat=n={len(keeps)}:v=1:a=0[vout]")
        maps = ["-map", "[vout]"]

    def build(out):
        return (["ffmpeg", "-y", "-i", src, "-filter_complex", ";".join(fc)]
                + maps + ENC_ARGS + [out])

    title = _source_title("Video") + " (silence cut)"
    result, err = _run_ffmpeg_job(session["user_id"], "silencecut", title, build)
    if cleanup:
        try:
            os.remove(src)
        except OSError:
            pass
    if err:
        return jsonify({"ok": False, "error": f"Silence cut nahi ho saka: {err}"}), 422
    result["removed_segments"] = len(silences)
    return jsonify({"ok": True, **result})


@app.route("/api/chromakey", methods=["POST"])
@login_required
@plan_required
def chromakey():
    src, cleanup = _resolve_source()
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400
    key = _hex6(request.form.get("key_color"), "00ff00")
    try:
        sim = max(0.1, min(0.5, float(request.form.get("similarity") or 0.25)))
    except ValueError:
        sim = 0.25
    bg_color = _hex6(request.form.get("bg_color"), "0000ff")
    vw, vh = probe_dims(src)
    vw, vh = vw or 640, vh or 360
    bg_path = None
    bf = request.files.get("bg")
    if bf and bf.filename:
        bg_path = _temp_upload(bf, suffix=os.path.splitext(bf.filename)[1] or ".png")
    if bg_path:
        inputs = ["-i", src, "-loop", "1", "-framerate", "30", "-i", bg_path]
        bg_chain = f"[1:v]scale={vw}:{vh}[bgs];"
    else:
        inputs = ["-i", src, "-f", "lavfi", "-i",
                  f"color=c=0x{bg_color}:s={vw}x{vh}:r=30"]
        bg_chain = ""
    fc = (bg_chain +
          f"[0:v]chromakey=0x{key}:{sim:.2f}:0.08[fg];"
          f"[1:v][fg]overlay=0:0:shortest=1:format=auto,format=yuv420p[vout]")

    def build(out):
        return (["ffmpeg", "-y"] + inputs +
                ["-filter_complex", fc, "-map", "[vout]", "-map", "0:a?",
                 "-shortest"] + ENC_ARGS + [out])

    title = _source_title("Video") + " (green screen)"
    result, err = _run_ffmpeg_job(session["user_id"], "chromakey", title, build)
    for p in ([src] if cleanup else []) + ([bg_path] if bg_path else []):
        try:
            os.remove(p)
        except OSError:
            pass
    if err:
        return jsonify({"ok": False, "error": f"Green screen nahi lag saka: {err}"}), 422
    return jsonify({"ok": True, **result})

# ---------------------------------------------------------------- chhote tools

@app.route("/api/gif", methods=["POST"])
@login_required
@plan_required
def make_gif():
    src, cleanup = _resolve_source()
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400

    def fnum(name, default, lo, hi):
        try:
            return max(lo, min(hi, float(request.form.get(name))))
        except (TypeError, ValueError):
            return default

    start = fnum("start", 0.0, 0.0, 86400.0)
    dur = fnum("duration", 3.0, 0.1, 10.0)
    try:
        width = int(request.form.get("width") or 480)
    except ValueError:
        width = 480
    if width not in (320, 480):
        width = 480
    fc = (f"[0:v]fps=12,scale={width}:-1:flags=lanczos,split[a][b];"
          "[a]palettegen[p];[b][p]paletteuse")

    def build(out):
        return ["ffmpeg", "-y", "-ss", str(start), "-i", src, "-t", str(dur),
                "-filter_complex", fc, "-loop", "0", out]

    result, err = _run_ffmpeg_job(session["user_id"], "gif",
                                  _source_title("Video") + " (GIF)", build, ext="gif")
    if cleanup:
        try:
            os.remove(src)
        except OSError:
            pass
    if err:
        return jsonify({"ok": False, "error": f"GIF nahi ban saka: {err}"}), 422
    return jsonify({"ok": True, **result})


@app.route("/api/mp3", methods=["POST"])
@login_required
@plan_required
def extract_mp3():
    src, cleanup = _resolve_source()
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400
    if not probe_has_audio(src):
        if cleanup:
            try:
                os.remove(src)
            except OSError:
                pass
        return jsonify({"ok": False, "error": "Is video mein audio hi nahi hai."}), 400

    def build(out):
        return ["ffmpeg", "-y", "-i", src, "-vn", "-c:a", "libmp3lame",
                "-q:a", "2", out]

    result, err = _run_ffmpeg_job(session["user_id"], "mp3",
                                  _source_title("Video") + " (MP3)", build, ext="mp3")
    if cleanup:
        try:
            os.remove(src)
        except OSError:
            pass
    if err:
        return jsonify({"ok": False, "error": f"MP3 nahi ban saka: {err}"}), 422
    return jsonify({"ok": True, **result})


@app.route("/api/compress", methods=["POST"])
@login_required
@plan_required
def compress():
    src, cleanup = _resolve_source()
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400
    q = (request.form.get("quality") or "medium").strip()
    crf = {"high": "24", "medium": "28", "small": "33"}.get(q, "28")

    def build(out):
        return ["ffmpeg", "-y", "-i", src, "-c:v", "libx264", "-preset",
                "veryfast", "-crf", crf, "-c:a", "aac", "-b:a", "128k",
                "-movflags", "+faststart", out]

    result, err = _run_ffmpeg_job(session["user_id"], "compress",
                                  _source_title("Video") + " (compressed)", build)
    if cleanup:
        try:
            os.remove(src)
        except OSError:
            pass
    if err:
        return jsonify({"ok": False, "error": f"Compress nahi ho saka: {err}"}), 422
    return jsonify({"ok": True, **result})


@app.route("/api/thumbnail", methods=["POST"])
@login_required
@plan_required
def thumbnail():
    src, cleanup = _resolve_source()
    if not src:
        return jsonify({"ok": False, "error": "Video file upload karo ya downloaded file select karo."}), 400
    try:
        t = max(0.0, float(request.form.get("time") or 1))
    except ValueError:
        t = 1.0

    def build(out):
        return ["ffmpeg", "-y", "-ss", str(t), "-i", src,
                "-frames:v", "1", "-q:v", "3", out]

    result, err = _run_ffmpeg_job(session["user_id"], "thumbnail",
                                  _source_title("Video") + " (thumbnail)",
                                  build, ext="jpg")
    if cleanup:
        try:
            os.remove(src)
        except OSError:
            pass
    if err:
        return jsonify({"ok": False, "error": f"Thumbnail nahi ban saka: {err}"}), 422
    return jsonify({"ok": True, **result})

# ---------------------------------------------------------------- tiktok upload

def tiktok_tokens_for(user_id):
    """User ka TikTok token row (dict) ya None. Expired ho to refresh token se
    naya access token le kar save kar do; refresh fail ho to None
    (disconnect samjho)."""
    row = db().execute("SELECT * FROM tiktok_tokens WHERE user_id = ?",
                       (user_id,)).fetchone()
    if not row:
        return None
    tok = dict(row)
    exp = tok.get("expires_at") or 0
    if exp and exp <= int(time.time()) + 60 and tok.get("refresh_token"):
        if requests is None or not TIKTOK_CLIENT_KEY or not TIKTOK_CLIENT_SECRET:
            return None
        try:
            r = requests.post(f"{TIKTOK_API_BASE}/oauth/token/", data={
                "client_key": TIKTOK_CLIENT_KEY,
                "client_secret": TIKTOK_CLIENT_SECRET,
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
            }, timeout=30)
            d = r.json()
        except Exception:
            return None
        if not isinstance(d, dict) or not d.get("access_token"):
            return None
        tok.update({
            "access_token": d["access_token"],
            "refresh_token": d.get("refresh_token") or tok["refresh_token"],
            "open_id": d.get("open_id") or tok.get("open_id"),
            "scope": d.get("scope") or tok.get("scope"),
            "expires_at": int(time.time()) + int(d.get("expires_in") or 0),
        })
        db().execute(
            "UPDATE tiktok_tokens SET open_id = ?, access_token = ?, "
            "refresh_token = ?, expires_at = ?, scope = ? WHERE user_id = ?",
            (tok["open_id"], tok["access_token"], tok["refresh_token"],
             tok["expires_at"], tok["scope"], user_id))
        db().commit()
    return tok


def _tiktok_error_text(resp):
    """TikTok API ke jawab se error ka text — error.message / code, ya
    OAuth wale error_description, warna HTTP status."""
    try:
        d = resp.json()
    except Exception:
        return f"TikTok se jawab nahi aaya (HTTP {resp.status_code})."
    if isinstance(d, dict):
        err = d.get("error")
        if isinstance(err, dict):
            return (err.get("message") or err.get("code")
                    or f"TikTok error (HTTP {resp.status_code}).")
        if isinstance(err, str) and err:
            return d.get("error_description") or err
        if d.get("error_description"):
            return d["error_description"]
    return f"TikTok error (HTTP {resp.status_code})."


def _tiktok_ok(resp):
    """Content Posting API ke jawab mein error.code == 'ok' matlab success.
    (ok, parsed_json) return karta hai."""
    try:
        d = resp.json()
    except Exception:
        return False, {}
    if not isinstance(d, dict):
        return False, {}
    return (d.get("error") or {}).get("code") == "ok", d


@app.route("/api/tiktok/auth-url")
@login_required
@plan_required
def tiktok_auth_url():
    if not TIKTOK_CLIENT_KEY:
        return jsonify({"ok": False,
                        "error": "TikTok abhi configure nahi hua — server par "
                                 "Client Key set nahi hai."}), 503
    state = uuid.uuid4().hex
    session["tiktok_state"] = state
    q = urlencode({
        "client_key": TIKTOK_CLIENT_KEY,
        "scope": TIKTOK_SCOPES,
        "response_type": "code",
        "redirect_uri": TIKTOK_REDIRECT_URI,
        "state": state,
    })
    return jsonify({"ok": True,
                    "url": f"https://www.tiktok.com/v2/auth/authorize/?{q}"})


@app.route("/tiktok/callback")
def tiktok_callback():
    state = request.args.get("state") or ""
    saved = session.pop("tiktok_state", None)
    if not saved or state != saved:
        return redirect("/?tiktok=error&reason=state")
    user = current_user()
    if not user:
        return redirect("/")
    code = request.args.get("code") or ""
    if not code or requests is None:
        return redirect("/?tiktok=error&reason=code")
    try:
        r = requests.post(f"{TIKTOK_API_BASE}/oauth/token/", data={
            "client_key": TIKTOK_CLIENT_KEY,
            "client_secret": TIKTOK_CLIENT_SECRET,
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": TIKTOK_REDIRECT_URI,
        }, timeout=30)
        d = r.json()
    except Exception:
        return redirect("/?tiktok=error&reason=token")
    if not isinstance(d, dict) or not d.get("access_token"):
        return redirect("/?tiktok=error&reason=token")
    db().execute(
        "INSERT OR REPLACE INTO tiktok_tokens "
        "(user_id, open_id, access_token, refresh_token, expires_at, scope) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], d.get("open_id"), d["access_token"], d.get("refresh_token"),
         int(time.time()) + int(d.get("expires_in") or 0), d.get("scope")))
    db().commit()
    return redirect("/#upload")


@app.route("/api/tiktok/status")
@login_required
def tiktok_status():
    tok = tiktok_tokens_for(session["user_id"])
    name = None
    if tok and requests is not None:
        try:
            r = requests.post(
                f"{TIKTOK_API_BASE}/post/publish/creator_info/query/",
                headers={"Authorization": f"Bearer {tok['access_token']}"},
                timeout=15)
            ok, d = _tiktok_ok(r)
            if ok:
                u = ((d.get("data") or {}).get("user") or {})
                name = u.get("nickname") or u.get("username")
        except Exception:
            name = None
    return jsonify({"ok": True, "configured": bool(TIKTOK_CLIENT_KEY),
                    "connected": bool(tok), "name": name})


@app.route("/api/tiktok/disconnect", methods=["POST"])
@login_required
def tiktok_disconnect():
    db().execute("DELETE FROM tiktok_tokens WHERE user_id = ?",
                 (session["user_id"],))
    db().commit()
    return jsonify({"ok": True})


@app.route("/api/tiktok/upload", methods=["POST"])
@login_required
@plan_required
def tiktok_upload():
    uid = session["user_id"]
    tok = tiktok_tokens_for(uid)
    if not tok:
        return jsonify({"ok": False,
                        "error": "Pehle TikTok connect karo — upar wale button "
                                 "se apna TikTok account joro."}), 400
    if requests is None:
        return jsonify({"ok": False,
                        "error": "Server par requests library available nahi hai."}), 501
    f = request.files.get("file")
    if not f or not f.filename:
        return jsonify({"ok": False, "error": "Video file select karo."}), 400
    data_bytes = f.read()
    size = len(data_bytes)
    if size == 0:
        return jsonify({"ok": False, "error": "Video file khali hai."}), 400
    if size > TIKTOK_MAX_UPLOAD:
        return jsonify({"ok": False,
                        "error": f"Video bahut bari hai ({human_size(size)}) — "
                                 "TikTok upload ki had 64 MB hai, pehle Compress "
                                 "tool se chhoti kar lo."}), 400
    title = (request.form.get("title") or "").strip() \
        or os.path.splitext(f.filename)[0]
    headers = {"Authorization": f"Bearer {tok['access_token']}",
               "Content-Type": "application/json; charset=UTF-8"}
    source_info = {"source": "FILE_UPLOAD", "video_size": size,
                   "chunk_size": size, "total_chunk_count": 1}

    def _put(upload_url):
        return requests.put(upload_url, data=data_bytes, headers={
            "Content-Type": "video/mp4",
            "Content-Length": str(size),
            "Content-Range": f"bytes 0-{size - 1}/{size}",
        }, timeout=300)

    errors = []
    # Flow A — direct post (SELF_ONLY: audit se pehle sirf khud ko dikhega)
    try:
        r = requests.post(f"{TIKTOK_API_BASE}/post/publish/video/init/",
                          headers=headers, json={
                              "post_info": {"title": title,
                                            "privacy_level": "SELF_ONLY",
                                            "disable_comment": False},
                              "source_info": source_info,
                          }, timeout=30)
        ok, d = _tiktok_ok(r)
        if ok and (d.get("data") or {}).get("upload_url"):
            pr = _put(d["data"]["upload_url"])
            if pr.status_code in (200, 201):
                bump_stats(uid, tiktok_uploads=1)
                return jsonify({"ok": True, "mode": "direct",
                                "publish_id": d["data"].get("publish_id")})
            errors.append(f"Direct post ki file upload fail (HTTP {pr.status_code}).")
        else:
            errors.append("Direct post: " + _tiktok_error_text(r))
    except Exception as e:
        errors.append(f"Direct post: {str(e)[:150]}")
    # Flow B — inbox draft (video TikTok inbox mein draft ban kar jayegi)
    try:
        r = requests.post(f"{TIKTOK_API_BASE}/post/publish/inbox/video/init/",
                          headers=headers, json={"source_info": source_info},
                          timeout=30)
        ok, d = _tiktok_ok(r)
        if ok and (d.get("data") or {}).get("upload_url"):
            pr = _put(d["data"]["upload_url"])
            if pr.status_code in (200, 201):
                bump_stats(uid, tiktok_uploads=1)
                return jsonify({"ok": True, "mode": "inbox",
                                "publish_id": d["data"].get("publish_id")})
            errors.append(f"Inbox ki file upload fail (HTTP {pr.status_code}).")
        else:
            errors.append("Inbox draft: " + _tiktok_error_text(r))
    except Exception as e:
        errors.append(f"Inbox draft: {str(e)[:150]}")
    return jsonify({"ok": False,
                    "error": "TikTok upload fail ho gaya — "
                             + " | ".join(errors)}), 422

# ---------------------------------------------------------------- misc

@app.route("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
