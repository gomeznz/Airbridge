"""FastAPI web front end: live status, recordings, setup and test tools."""
import asyncio
import hashlib
import hmac
import logging
import os
import secrets
import time

from fastapi import FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse

from . import hardware
from .config import DATA_DIR, Config
from .engine import Engine

log = logging.getLogger("web")
HERE = os.path.dirname(__file__)

config = Config()
engine = Engine(config, DATA_DIR)
app = FastAPI(title="AirBridge", docs_url=None, redoc_url=None)
_SECRET = secrets.token_bytes(32)


# ---------- auth (optional single password) ----------
def _token():
    return hmac.new(_SECRET, config["web"]["password"].encode(), hashlib.sha256).hexdigest()


def _authed(req_or_ws):
    pw = config["web"]["password"]
    if not pw:
        return True
    return hmac.compare_digest(req_or_ws.cookies.get("ab_auth", ""), _token())


@app.middleware("http")
async def auth_mw(request: Request, call_next):
    path = request.url.path
    if path in ("/login", "/favicon.svg") or _authed(request):
        return await call_next(request)
    if path.startswith("/api/"):
        return JSONResponse({"detail": "login required"}, status_code=401)
    return RedirectResponse("/login")


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    return LOGIN_HTML


@app.post("/login")
async def login(request: Request):
    form = await request.form()
    if hmac.compare_digest(str(form.get("password", "")), config["web"]["password"]):
        r = RedirectResponse("/", status_code=303)
        r.set_cookie("ab_auth", _token(), httponly=True, samesite="lax", max_age=30 * 86400)
        return r
    return HTMLResponse(LOGIN_HTML.replace("<!--err-->", "<p class=err>Wrong password</p>"), status_code=401)


# ---------- lifecycle ----------
@app.on_event("startup")
async def _startup():
    await engine.start()


@app.on_event("shutdown")
async def _shutdown():
    await engine.stop()


# ---------- pages ----------
@app.get("/", response_class=HTMLResponse)
async def index():
    with open(os.path.join(HERE, "static", "index.html")) as f:
        return f.read()


@app.get("/favicon.svg")
async def favicon():
    return FileResponse(os.path.join(HERE, "static", "favicon.svg"), media_type="image/svg+xml")


# ---------- live ----------
@app.websocket("/ws")
async def ws(sock: WebSocket):
    if not _authed(sock):
        await sock.close(code=4401)
        return
    await sock.accept()
    q = asyncio.Queue()
    engine.listeners.add(q)
    try:
        await sock.send_json({"type": "events", "items": list(engine.events)[-60:]})
        while True:
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
async def api_status():
    return engine.status()


@app.get("/api/events")
async def api_events():
    return list(engine.events)


# ---------- history ----------
@app.get("/api/history")
async def history(minutes: float = 60):
    secs = max(60, min(int(minutes * 60), 86400))
    return engine.history.query(secs, time.time())


@app.get("/api/history/stats")
async def history_stats(minutes: float = 360):
    secs = max(300, min(int(minutes * 60), 86400))
    st = engine.history.stats(secs, time.time(), bool(config["rssi"].get("invert")))
    st["window_min"] = secs // 60
    return st


# ---------- recordings ----------
@app.get("/api/recordings")
async def recordings(limit: int = 50, offset: int = 0):
    rows, total = engine.store.list(min(limit, 500), offset)
    return {"items": rows, "total": total}


@app.get("/api/recordings/{oid}/audio")
async def recording_audio(oid: int):
    r = engine.store.get(oid)
    if not r or not r["file"]:
        raise HTTPException(404)
    path = os.path.realpath(os.path.join(engine.store.dir, r["file"]))
    if not path.startswith(os.path.realpath(engine.store.dir)) or not os.path.exists(path):
        raise HTTPException(404)
    mt = {".ogg": "audio/ogg", ".mp3": "audio/mpeg", ".wav": "audio/wav"}[os.path.splitext(path)[1]]
    return FileResponse(path, media_type=mt, filename=os.path.basename(path))


@app.delete("/api/recordings/{oid}")
async def recording_delete(oid: int):
    if not engine.store.delete(oid):
        raise HTTPException(404)
    return {"ok": True}


# ---------- setup ----------
@app.get("/api/config")
async def get_config():
    return config.data


@app.post("/api/config")
async def set_config(request: Request):
    new = await request.json()
    old_web = dict(config["web"])
    config.save(new)
    engine.event("info", "settings saved; restarting engine")
    await engine.restart()
    note = None
    if (config["web"]["host"], config["web"]["port"]) != (old_web["host"], old_web["port"]):
        note = "Web host/port change takes effect after the service restarts."
    return {"ok": True, "note": note, "config": config.data}


@app.get("/api/audio-devices")
async def audio_devices():
    try:
        import sounddevice as sd
        return [{"index": i, "name": d["name"], "inputs": d["max_input_channels"]}
                for i, d in enumerate(sd.query_devices()) if d["max_input_channels"] > 0]
    except Exception as e:
        return [{"index": -1, "name": f"unavailable: {e}", "inputs": 0}]


# ---------- test tools ----------
@app.post("/api/test/call")
async def test_call(seconds: float = 5.0):
    try:
        await engine.test_call(max(1.0, min(seconds, 30.0)))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/test/hangup")
async def test_hangup():
    await engine.hangup()
    return {"ok": True}


@app.post("/api/test/tx")
async def test_tx(seconds: float = 5.0, freq: float = 1000.0, level: float = -10.0):
    try:
        engine.test_tx(max(1.0, min(seconds, 30.0)), max(100.0, min(freq, 3500.0)), max(-60.0, min(level, 0.0)))
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    return {"ok": True}


@app.post("/api/ptt/release")
async def ptt_release():
    engine.release_ptt()
    return {"ok": True}


@app.post("/api/relay/{mode}")
async def relay(mode: str):
    if mode not in ("on", "off", "auto"):
        raise HTTPException(400)
    engine.set_relay_override(None if mode == "auto" else mode == "on")
    return {"ok": True}


@app.post("/api/sim/{mode}")
async def sim(mode: str):
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


LOGIN_HTML = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>AirBridge</title><link rel=icon href=/favicon.svg>
<style>body{margin:0;min-height:100vh;display:grid;place-items:center;background:#0d1117;color:#e6edf3;
font:15px system-ui,sans-serif}form{background:#161b22;border:1px solid #30363d;border-radius:12px;padding:28px;
width:min(320px,86vw)}h1{font-size:18px;margin:0 0 16px}input{width:100%;box-sizing:border-box;padding:10px;
border-radius:8px;border:1px solid #30363d;background:#0d1117;color:inherit;font:inherit}button{margin-top:12px;
width:100%;padding:10px;border:0;border-radius:8px;background:#e3a008;color:#111;font-weight:600;font:inherit}
.err{color:#f85149;margin:8px 0 0}</style>
<form method=post action=/login><h1>AirBridge</h1><input type=password name=password placeholder=Password autofocus>
<!--err--><button>Sign in</button></form>"""
