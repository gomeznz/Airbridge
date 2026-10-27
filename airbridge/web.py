"""FastAPI web front end: live status, recordings, setup and test tools.

Every route is behind a login; each one declares the minimum role it needs
(viewer < operator < admin). See auth.py for accounts, sessions and audit.
"""
import asyncio
import html
import logging
import os
import subprocess
import time
from urllib.parse import urlparse

from fastapi import Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import hardware
from .auth import RANK, ROLES, Auth, AuthError, client_ip, ip_allowed
from .config import DATA_DIR, Config
from .engine import Engine

log = logging.getLogger("web")
HERE = os.path.dirname(__file__)

config = Config()
engine = Engine(config, DATA_DIR)
auth = Auth(DATA_DIR, config["web"])
app = FastAPI(title="AirBridge", docs_url=None, redoc_url=None, openapi_url=None)

COOKIE = "ab_session"
PUBLIC = {"/login", "/setup", "/favicon.svg"}


# ---------- helpers ----------
def _ip(req):
    return client_ip(req.client, req.headers, bool(config["web"].get("trust_proxy")))


def _https(req):
    if req.url.scheme == "https":
        return True
    return bool(config["web"].get("trust_proxy")) and req.headers.get("x-forwarded-proto") == "https"


def _same_origin(req):
    """Form posts can't carry our custom header: check Origin/Referer instead."""
    src = req.headers.get("origin") or req.headers.get("referer")
    if not src:
        return True                         # non-browser client
    host = req.headers.get("x-forwarded-host") if config["web"].get("trust_proxy") else None
    return urlparse(src).netloc == (host or req.headers.get("host"))


def role(minimum):
    def dep(request: Request):
        u = request.state.user
        if RANK[u["role"]] < RANK[minimum]:
            raise HTTPException(403, f"{minimum} role required")
        return u
    return Depends(dep)


def audit(request, action, detail=None):
    u = getattr(request.state, "user", None)
    auth.audit(u["username"] if u else None, _ip(request), action, detail)


# ---------- middleware ----------
@app.middleware("http")
async def access_mw(request: Request, call_next):
    path = request.url.path
    ip = _ip(request)
    if not ip_allowed(ip, config["web"].get("allow_cidrs")):
        return JSONResponse({"detail": "forbidden from this address"}, status_code=403)
    request.state.user = None
    if path not in PUBLIC:
        if not auth.user_count():
            return RedirectResponse("/setup") if not path.startswith("/api/") else \
                JSONResponse({"detail": "setup required"}, status_code=401)
        u = auth.session(request.cookies.get(COOKIE), ip)
        if not u:
            if path.startswith("/api/"):
                return JSONResponse({"detail": "login required"}, status_code=401)
            return RedirectResponse("/login")
        request.state.user = u
        # CSRF: state-changing API calls must come from our own page (custom header + same origin)
        if path.startswith("/api/") and request.method not in ("GET", "HEAD"):
            if request.headers.get("x-airbridge") != "1" or not _same_origin(request):
                return JSONResponse({"detail": "bad request origin"}, status_code=403)
    resp = await call_next(request)
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["Referrer-Policy"] = "same-origin"
    resp.headers["Content-Security-Policy"] = ("default-src 'self'; script-src 'self' 'unsafe-inline'; "
                                               "style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
                                               "media-src 'self'; connect-src 'self' ws: wss:; frame-ancestors 'none'")
    if path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    return resp


# ---------- login / setup / logout ----------
def _page(title, body, err=None):
    e = f"<p class=err>{html.escape(err)}</p>" if err else ""
    return AUTH_HTML.replace("{title}", title).replace("{body}", body).replace("{err}", e)


LOGIN_FORM = """<form method=post action=/login><h1>AirBridge</h1>
<input name=username placeholder=Username autocomplete=username autofocus required>
<input type=password name=password placeholder=Password autocomplete=current-password required>
{err}<button>Sign in</button></form>"""

SETUP_FORM = """<form method=post action=/setup><h1>AirBridge setup</h1>
<p class=muted>Create the first administrator. The setup code is printed in the service log
(<code>journalctl -u airbridge</code>) and saved in <code>/var/lib/airbridge/setup-code.txt</code>.</p>
<input name=code placeholder="Setup code" autocomplete=off required>
<input name=username placeholder="Admin username" autocomplete=username required>
<input type=password name=password placeholder="Password (10+ characters)" autocomplete=new-password required>
<input type=password name=password2 placeholder="Repeat password" autocomplete=new-password required>
{err}<button>Create admin</button></form>"""


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    if not auth.user_count():
        return RedirectResponse("/setup")
    return _page("Sign in", LOGIN_FORM)


@app.post("/login")
async def login(request: Request):
    if not _same_origin(request):
        raise HTTPException(403)
    form = await request.form()
    try:
        token = auth.login(str(form.get("username", "")).strip(), str(form.get("password", "")),
                           _ip(request), request.headers.get("user-agent"))
    except AuthError as e:
        return HTMLResponse(_page("Sign in", LOGIN_FORM, str(e)), status_code=401)
    r = RedirectResponse("/", status_code=303)
    r.set_cookie(COOKIE, token, httponly=True, samesite="strict", secure=_https(request),
                 max_age=int(float(config["web"].get("session_max_days", 30)) * 86400))
    return r


@app.get("/setup", response_class=HTMLResponse)
async def setup_page():
    if auth.user_count():
        return RedirectResponse("/login")
    return _page("Setup", SETUP_FORM)


@app.post("/setup")
async def setup(request: Request):
    if not _same_origin(request) or auth.user_count():
        return RedirectResponse("/login", status_code=303)
    f = await request.form()
    if f.get("password") != f.get("password2"):
        return HTMLResponse(_page("Setup", SETUP_FORM, "passwords do not match"), status_code=400)
    try:
        auth.first_admin(str(f.get("code", "")), str(f.get("username", "")).strip(), str(f.get("password", "")),
                         _ip(request))
    except AuthError as e:
        return HTMLResponse(_page("Setup", SETUP_FORM, str(e)), status_code=400)
    engine.event("info", f"first admin '{f.get('username')}' created")
    return RedirectResponse("/login", status_code=303)


@app.post("/api/logout")
async def logout(request: Request):
    auth.logout(request.cookies.get(COOKIE), _ip(request))
    r = JSONResponse({"ok": True})
    r.delete_cookie(COOKIE)
    return r


@app.get("/api/me")
async def me(user=role("viewer")):
    return {"username": user["username"], "role": user["role"]}


@app.post("/api/me/password")
async def me_password(request: Request, user=role("viewer")):
    b = await request.json()
    try:
        auth.change_own_password(user, str(b.get("current", "")), str(b.get("new", "")), _ip(request))
    except AuthError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


# ---------- user / session / audit admin ----------
@app.get("/api/users")
async def users(user=role("admin")):
    return auth.list_users()


@app.post("/api/users")
async def user_create(request: Request, user=role("admin")):
    b = await request.json()
    try:
        auth.create_user(str(b.get("username", "")).strip(), str(b.get("password", "")),
                         str(b.get("role", "viewer")), by=user["username"], ip=_ip(request))
    except AuthError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.patch("/api/users/{uid}")
async def user_update(uid: int, request: Request, user=role("admin")):
    b = await request.json()
    try:
        auth.update_user(uid, user["username"], _ip(request), role=b.get("role"),
                         password=b.get("password") or None, disabled=b.get("disabled"))
    except AuthError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.delete("/api/users/{uid}")
async def user_delete(uid: int, request: Request, user=role("admin")):
    if uid == user["id"]:
        raise HTTPException(400, "you can't delete your own account")
    try:
        auth.delete_user(uid, user["username"], _ip(request))
    except AuthError as e:
        raise HTTPException(400, str(e))
    return {"ok": True}


@app.get("/api/sessions")
async def sessions(user=role("admin")):
    out = auth.list_sessions()
    for s_ in out:
        s_["current"] = s_["id"] == user["session_id"]
    return out


@app.delete("/api/sessions/{sid}")
async def session_revoke(sid: int, request: Request, user=role("admin")):
    auth.revoke_session(sid, user["username"], _ip(request))
    return {"ok": True}


@app.get("/api/audit")
async def audit_log(limit: int = 200, offset: int = 0, user=role("admin")):
    return auth.audit_log(min(limit, 1000), offset)


# ---------- lifecycle ----------
@app.on_event("startup")
async def _startup():
    note = auth.bootstrap(config["web"].get("password", ""))
    if config["web"].get("password"):
        c = dict(config.data)
        c["web"] = {**c["web"], "password": ""}            # never keep the plain-text password
        config.save(c)
    await engine.start()
    if note:
        log.warning(note)                     # the setup code goes to the journal only, never the web UI
        engine.event("info", "no users yet: create the first admin at /setup" if auth.setup_code else note)


@app.on_event("shutdown")
async def _shutdown():
    await engine.stop()


# ---------- pages ----------
@app.get("/", response_class=HTMLResponse)
async def index(user=role("viewer")):
    with open(os.path.join(HERE, "static", "index.html")) as f:
        return f.read()


@app.get("/favicon.svg")
async def favicon():
    return FileResponse(os.path.join(HERE, "static", "favicon.svg"), media_type="image/svg+xml")


# ---------- live ----------
@app.websocket("/ws")
async def ws(sock: WebSocket):
    ip = client_ip(sock.client, sock.headers, bool(config["web"].get("trust_proxy")))
    token = sock.cookies.get(COOKIE)
    origin = sock.headers.get("origin")
    if (not ip_allowed(ip, config["web"].get("allow_cidrs")) or not auth.session(token, ip)
            or (origin and urlparse(origin).netloc != sock.headers.get("host")
                and not config["web"].get("trust_proxy"))):
        await sock.close(code=4401)
        return
    await sock.accept()
    q = asyncio.Queue()
    engine.listeners.add(q)
    checked = time.time()
    try:
        await sock.send_json({"type": "events", "items": list(engine.events)[-60:]})
        while True:
            if time.time() - checked > 30:                 # revoked / expired sessions get dropped
                checked = time.time()
                if not auth.session(token, ip):
                    await sock.close(code=4401)
                    return
            await sock.send_json(engine.status())
            try:
                while True:
                    item = await asyncio.wait_for(q.get(), timeout=0.2)
                    await sock.send_json(item)
            except asyncio.TimeoutError:
                pass
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        engine.listeners.discard(q)


@app.get("/api/status")
async def api_status(user=role("viewer")):
    return engine.status()


@app.get("/api/events")
async def api_events(user=role("viewer")):
    return list(engine.events)


# ---------- history ----------
@app.get("/api/history")
async def history(minutes: float = 60, user=role("viewer")):
    secs = max(60, min(int(minutes * 60), engine.history.keep))
    return await asyncio.get_running_loop().run_in_executor(None, engine.history.query, secs, time.time())


@app.get("/api/history/stats")
async def history_stats(minutes: float = 360, user=role("viewer")):
    secs = max(300, min(int(minutes * 60), engine.history.keep))
    st = await asyncio.get_running_loop().run_in_executor(
        None, engine.history.stats, secs, time.time(), bool(config["rssi"].get("invert")))
    st["window_min"] = secs // 60
    return st


# ---------- system clock ----------
SETCLOCK = "/usr/local/sbin/airbridge-setclock"


def _clock_info():
    synced = ntp = None
    try:
        out = subprocess.run(["timedatectl", "show", "-p", "NTPSynchronized", "-p", "NTP"],
                             capture_output=True, text=True, timeout=3).stdout
        kv = dict(line.split("=", 1) for line in out.splitlines() if "=" in line)
        synced, ntp = kv.get("NTPSynchronized") == "yes", kv.get("NTP") == "yes"
    except Exception:
        pass
    return {"t": time.time(), "ntp_synced": synced, "ntp_enabled": ntp, "rtc": os.path.exists("/dev/rtc0"),
            "can_set": os.path.exists(SETCLOCK)}


@app.get("/api/clock")
async def clock(user=role("viewer")):
    return await asyncio.get_running_loop().run_in_executor(None, _clock_info)


@app.post("/api/clock")
async def clock_set(request: Request, user=role("admin")):
    b = await request.json()
    try:
        t = int(float(b["t"]))
    except (KeyError, TypeError, ValueError):
        raise HTTPException(400, "need t (unix seconds)")
    before = time.time()
    try:
        r = subprocess.run(["sudo", "-n", SETCLOCK, str(t)], capture_output=True, text=True, timeout=10)
    except FileNotFoundError:
        raise HTTPException(501, "sudo not available")
    if r.returncode != 0:
        raise HTTPException(500, (r.stderr or r.stdout or "could not set the clock").strip()
                            + " (is install.sh's clock helper installed?)")
    audit(request, "clock_set", f"{time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(t))} "
                                f"(was {t - before:+.0f} s off)")
    engine.event("info", f"system clock set from browser by {user['username']} ({t - before:+.0f} s)")
    return _clock_info()


# ---------- recordings ----------
@app.get("/api/recordings")
async def recordings(limit: int = 50, offset: int = 0, user=role("viewer")):
    rows, total = engine.store.list(min(limit, 500), offset)
    return {"items": rows, "total": total}


@app.get("/api/recordings/{oid}/audio")
async def recording_audio(oid: int, user=role("viewer")):
    r = engine.store.get(oid)
    if not r or not r["file"]:
        raise HTTPException(404)
    path = os.path.realpath(os.path.join(engine.store.dir, r["file"]))
    if not path.startswith(os.path.realpath(engine.store.dir)) or not os.path.exists(path):
        raise HTTPException(404)
    mt = {".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav"}[os.path.splitext(path)[1]]
    return FileResponse(path, media_type=mt, filename=os.path.basename(path))


@app.delete("/api/recordings/{oid}")
async def recording_delete(oid: int, request: Request, user=role("operator")):
    if not engine.store.delete(oid):
        raise HTTPException(404)
    audit(request, "recording_delete", f"#{oid}")
    return {"ok": True}


# ---------- setup ----------
@app.get("/api/config")
async def get_config(user=role("admin")):
    return config.data


@app.post("/api/config")
async def set_config(request: Request, user=role("admin")):
    new = await request.json()
    old_web = dict(config["web"])
    changed = sorted(k for k in new if new.get(k) != config.data.get(k))
    new.setdefault("web", {})["password"] = ""
    config.save(new)
    auth.cfg = config["web"]
    audit(request, "settings_save", ", ".join(changed) or "no changes")
    engine.event("info", "settings saved; restarting engine")
    await engine.restart()
    note = None
    if (config["web"]["host"], config["web"]["port"]) != (old_web["host"], old_web["port"]):
        note = "Web host/port change takes effect after the service restarts."
    return {"ok": True, "note": note, "config": config.data}


@app.get("/api/audio-devices")
async def audio_devices(user=role("admin")):
    try:
        import sounddevice as sd
        return [{"index": i, "name": d["name"], "inputs": d["max_input_channels"]}
                for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    except Exception as e:
        return [{"index": -1, "name": f"unavailable: {e}", "inputs": 0}]


# ---------- test tools ----------
@app.post("/api/test/call")
async def test_call(request: Request, seconds: float = 5.0, user=role("operator")):
    try:
        await engine.test_call(max(1.0, min(seconds, 30.0)))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    audit(request, "test_call", f"{seconds:.0f} s")
    return {"ok": True}


@app.post("/api/test/hangup")
async def test_hangup(request: Request, user=role("operator")):
    await engine.hangup()
    audit(request, "hangup")
    return {"ok": True}


@app.post("/api/test/tx")
async def test_tx(request: Request, seconds: float = 5.0, freq: float = 1000.0, level: float = -10.0,
                  user=role("operator")):
    try:
        engine.test_tx(max(1.0, min(seconds, 30.0)), max(100.0, min(freq, 3500.0)), max(-60.0, min(level, 0.0)))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    audit(request, "tx_test_tone", f"{seconds:.0f} s, {freq:.0f} Hz, {level:.0f} dBFS")
    return {"ok": True}


@app.post("/api/ptt/release")
async def ptt_release(request: Request, user=role("operator")):
    engine.release_ptt()
    audit(request, "ptt_release")
    return {"ok": True}


@app.post("/api/relay/{mode}")
async def relay(mode: str, request: Request, user=role("operator")):
    if mode not in ("on", "off", "auto"):
        raise HTTPException(400)
    engine.set_relay_override(None if mode == "auto" else mode == "on")
    audit(request, "relay", mode)
    return {"ok": True}


@app.post("/api/sim/{mode}")
async def sim(mode: str, user=role("operator")):
    if not engine.sim:
        raise HTTPException(409, "not in simulation mode")
    s = hardware.SIM
    if mode == "on":
        s.forced = True
    elif mode == "off":
        s.forced = False
    elif mode == "release":
        s.forced = None
    elif mode == "auto":
        s.forced, s.auto = None, not s.auto
    elif mode == "tone":
        s.tone = not s.tone
    else:
        raise HTTPException(400)
    return {"ok": True, "forced": s.forced, "auto": s.auto, "tone": s.tone}


AUTH_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>AirBridge · {title}</title><link rel=icon href=/favicon.svg>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;
font:15px system-ui,sans-serif}form{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:28px;
width:min(340px,86vw)}h1{font-size:18px;margin:0 0 16px}input{width:100%;box-sizing:border-box;padding:10px;
border-radius:8px;border:1px solid #30363d;background:#0d1117;color:inherit;font:inherit;margin-bottom:10px}
button{margin-top:4px;width:100%;padding:10px;border:0;border-radius:8px;background:#e3a008;color:#111;
font-weight:600;font:inherit;cursor:pointer}.err{color:#f85149;margin:0 0 10px}.muted{color:#8b949e;font-size:13px}
code{font-size:12px;color:#e6edf3}</style>
{body}"""
