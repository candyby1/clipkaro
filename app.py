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
               + apni .srt file video par burn karna
  - Facebook Page upload (browser mein, Facebook Login se)

Run:  ./venv/bin/python app.py
"""
import os
import re
import json
import uuid
import sqlite3
import subprocess
import tempfile
import threading
from functools import wraps

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
    opts = ydl_opts(extra)
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            meta = ydl.extract_info(url, download=True)
        matches = [p for p in os.listdir(DOWNLOAD_DIR) if p.startswith(job)]
        if not matches:
            return jsonify({"ok": False, "error": "Download ho gaya lekin file nahi mili."}), 500
        path = os.path.join(DOWNLOAD_DIR, matches[0])
        title = clean_title(meta.get("title"))
        dl_name = f"{title}.mp4"
        resp = send_file(path, as_attachment=True, download_name=dl_name, mimetype="video/mp4")
        # asal quality browser ko bhi bata do taake UI sahi label dikhaye
        height = meta_height(meta) or probe_height(path)
        if height:
            resp.headers["X-Video-Height"] = str(height)
            resp.headers["X-Video-Quality"] = quality_label(height) or ""
            resp.headers["Access-Control-Expose-Headers"] = "X-Video-Height, X-Video-Quality"

        @resp.call_on_close
        def _cleanup():
            try:
                os.remove(path)
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

def _run_bulk_job(job_id, user_id, urls, cookiefile):
    job_dir = os.path.join(JOBS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    items = []
    for u in urls:
        items.append({"url": u, "status": "pending", "title": None,
                      "file": None, "height": None, "quality": None,
                      "size": None, "error": None, "progress": 0})
    data = {"items": items, "done": 0, "total": len(items)}
    conn = sqlite3.connect(DB_PATH)
    conn.execute("UPDATE jobs SET data = ? WHERE id = ?", (json.dumps(data), job_id))
    conn.commit()
    conn.close()

    for idx, item in enumerate(items):
        item["status"] = "downloading"

        def hook(d, idx=idx):
            if d.get("status") == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                got = d.get("downloaded_bytes") or 0
                items[idx]["progress"] = int(got * 100 / total) if total else 0
                update_job(job_id, {"items": items, "done": sum(1 for i in items if i['status'] in ('done', 'error')), "total": len(items)})

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
            matches = [p for p in os.listdir(job_dir) if p.startswith(prefix + ".")]
            if not matches:
                raise RuntimeError("File nahi mili")
            fname = matches[0]
            fpath = os.path.join(job_dir, fname)
            item["file"] = fname
            item["title"] = meta.get("title")
            item["height"] = meta_height(meta) or probe_height(fpath)
            item["quality"] = quality_label(item["height"])
            item["size"] = os.path.getsize(fpath)
            item["progress"] = 100
            item["status"] = "done"
        except Exception as e:
            item["status"] = "error"
            item["error"] = str(e)[:250]
        update_job(job_id, {"items": items,
                            "done": sum(1 for i in items if i["status"] in ("done", "error")),
                            "total": len(items)})
    update_job(job_id, {"items": items,
                        "done": sum(1 for i in items if i["status"] in ("done", "error")),
                        "total": len(items)}, status="finished")


@app.route("/api/bulk-download", methods=["POST"])
@login_required
def bulk_download():
    data = request.get_json(force=True, silent=True) or {}
    urls = [u.strip() for u in (data.get("urls") or []) if isinstance(u, str) and URL_RE.match(u.strip())]
    if not urls:
        return jsonify({"ok": False, "error": "Koi valid video select nahi hui."}), 400
    if len(urls) > 100:
        return jsonify({"ok": False, "error": "Aik dafa mein zyada se zyada 100 videos select karo."}), 400
    job_id = uuid.uuid4().hex
    cf = user_cookiefile(session["user_id"]) if data.get("use_cookies") else None
    save_job(job_id, session["user_id"], "bulk", {"items": [], "done": 0, "total": len(urls)})
    t = threading.Thread(target=_run_bulk_job,
                         args=(job_id, session["user_id"], urls, cf), daemon=True)
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
            item["download_url"] = f"/api/files/{job_id}/{item['file']}"
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
                    "items": [{k: i.get(k) for k in ("title", "file", "quality", "status", "error")}
                              | ({"download_url": f"/api/files/{r['id']}/{i['file']}"} if i.get("file") else {})
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
    # subtitles filter ko path escape karna parta hai
    esc = srt.replace("\\", "\\\\").replace(":", "\\:").replace("'", "\\'")
    cmd = ["ffmpeg", "-y", "-i", src, "-vf", f"subtitles='{esc}'",
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

# ---------------------------------------------------------------- misc

@app.route("/api/health")
def health():
    return jsonify({"ok": True})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, threaded=True)
