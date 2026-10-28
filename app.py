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
  - Profiles — Pages/channels save karo, CSV import/export
  - Output format — downloads ko 9:16 / 16:9 mein convert + pehle N sec trim
  - Video Effects — brightness/contrast/saturation, flip, rotate, crop
  - Brand Overlay — text / PNG logo watermark
  - Auto Clips — lambi video se khud chhote clips (watermark ke sath)
  - Dashboard — usage stats (downloads, size, top sources, job types)

Run:  ./venv/bin/python app.py
"""
import os
import re
import csv
import io
import json
import uuid
import shutil
import sqlite3
import subprocess
import tempfile
import threading
from concurrent.futures import ThreadPoolExecutor
from functools import wraps
from urllib.parse import urlparse

from flask import (Flask, request, jsonify, send_file, send_from_directory,
                   session, g)
from werkzeug.security import generate_password_hash, check_password_hash

import yt_dlp

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
    """)
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


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024.0


def bump_stats(user_id, downloads=0, nbytes=0):
    """Lifetime download counters (thread-safe — apna connection)."""
    if not downloads and not nbytes:
        return
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        "INSERT INTO stats (user_id, downloads, bytes) VALUES (?, ?, ?) "
        "ON CONFLICT(user_id) DO UPDATE SET "
        "downloads = downloads + excluded.downloads, bytes = bytes + excluded.bytes",
        (user_id, downloads, nbytes))
    conn.commit()
    conn.close()


def _ratio_vf(fmt):
    """9:16 / 16:9 ke liye scale+pad filter (aspect barqarar, setsar=1)."""
    if fmt == "vertical":
        return ("scale=1080:1920:force_original_aspect_ratio=decrease,"
                "pad=1080:1920:(ow-iw)/2:(oh-ih)/2,setsar=1")
    if fmt == "horizontal":
        return ("scale=1920:1080:force_original_aspect_ratio=decrease,"
                "pad=1920:1080:(ow-iw)/2:(oh-ih)/2,setsar=1")
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


def _run_ffmpeg_job(user_id, jtype, title, cmd_for_out):
    """ffmpeg chala kar output ko aik finished job bana do.
    cmd_for_out(out_path) -> puri ffmpeg command list.
    Success par (result_dict, None), fail par (None, error_str)."""
    job_id = uuid.uuid4().hex
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    out_path = os.path.join(job_dir, "000.mp4")
    cmd = cmd_for_out(out_path)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
    if r.returncode != 0 or not os.path.isfile(out_path) or os.path.getsize(out_path) == 0:
        shutil.rmtree(job_dir, ignore_errors=True)
        return None, (r.stderr or "ffmpeg fail ho gaya")[-250:]
    h = probe_height(out_path)
    item = {"url": None, "title": title, "file": "000.mp4", "status": "done",
            "height": h, "quality": quality_label(h),
            "size": os.path.getsize(out_path), "error": None, "progress": 100}
    save_job(job_id, user_id, jtype, {"items": [item], "done": 1, "total": 1},
             status="finished")
    return {"job_id": job_id, "download_url": f"/api/files/{job_id}/000.mp4",
            "title": title, "quality": item["quality"], "size": item["size"]}, None

# ---------------------------------------------------------------- pages

@app.route("/")
def index():
    return send_from_directory(BASE_DIR, "index.html")

# ---------------------------------------------------------------- single video

@app.route("/api/info", methods=["POST"])
@login_required
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
    if fmt not in ("original", "vertical", "horizontal"):
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
def bulk_download():
    data = request.get_json(force=True, silent=True) or {}
    urls = [u.strip() for u in (data.get("urls") or []) if isinstance(u, str) and URL_RE.match(u.strip())]
    if not urls:
        return jsonify({"ok": False, "error": "Koi valid video select nahi hui."}), 400
    if len(urls) > 100:
        return jsonify({"ok": False, "error": "Aik dafa mein zyada se zyada 100 videos select karo."}), 400
    fmt = (data.get("fmt") or "original").strip()
    if fmt not in ("original", "vertical", "horizontal"):
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
    src = _temp_upload(f, suffix=os.path.splitext(f.filename)[1] or ".mp4")
    wav = src + ".wav"
    srt_path = os.path.join(DOWNLOAD_DIR, f"subs-{uuid.uuid4().hex}.srt")
    try:
        r = subprocess.run(["ffmpeg", "-y", "-i", src, "-vn", "-ac", "1", "-ar", "16000", wav],
                           capture_output=True, text=True, timeout=900)
        if r.returncode != 0:
            return jsonify({"ok": False, "error": "Video se audio nahi nikal saka."}), 422
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

    filters = []
    if crop_on and crop_pct > 0:
        frac = 1 - 2 * crop_pct / 100.0
        filters.append(f"crop=iw*{frac:.4f}:ih*{frac:.4f}")
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
    vf = ",".join(filters)
    title = _source_title("Video") + " (effects)"
    result, err = _run_ffmpeg_job(
        session["user_id"], "effect", title,
        lambda out: ["ffmpeg", "-y", "-i", src, "-vf", vf] + ENC_ARGS + [out])
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
    srow = db().execute("SELECT downloads, bytes FROM stats WHERE user_id = ?",
                        (uid,)).fetchone()
    downloads = srow["downloads"] if srow else 0
    nbytes = srow["bytes"] if srow else 0
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
                    "downloads": downloads, "downloaded_bytes": nbytes,
                    "downloaded_human": human_size(nbytes),
                    "failed": failed, "by_type": by_type,
                    "top_sources": top_sources})

# ---------------------------------------------------------------- misc

@app.route("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
