"""Access-control tests against the real app (simulation mode, SIP off)."""
import json
import os
import re
import tempfile

import pytest

DATA = tempfile.mkdtemp()
os.environ["AIRBRIDGE_DATA"] = DATA
with open(os.path.join(DATA, "config.json"), "w") as f:
    json.dump({"general": {"simulate": True}, "sip": {"enabled": False},
               "web": {"lockout_attempts": 3}}, f)

from fastapi.testclient import TestClient  # noqa: E402

import airbridge.config as cfgmod  # noqa: E402
cfgmod.DATA_DIR = DATA
from airbridge import web  # noqa: E402

H = {"X-AirBridge": "1"}


@pytest.fixture(scope="module")
def client():
    with TestClient(web.app, base_url="http://testserver") as c:
        yield c


def login(c, user, pw):
    c.cookies.clear()
    return c.post("/login", data={"username": user, "password": pw}, follow_redirects=False)


def test_first_run_requires_setup_code(client):
    assert client.get("/", follow_redirects=False).headers["location"] == "/setup"
    assert client.get("/api/status").status_code == 401
    r = client.post("/setup", data={"code": "WRONG", "username": "alex", "password": "correct horse 1",
                                    "password2": "correct horse 1"})
    assert r.status_code == 400 and "wrong setup code" in r.text
    code = open(os.path.join(DATA, "setup-code.txt")).read().strip()
    r = client.post("/setup", data={"code": code, "username": "alex", "password": "correct horse 1",
                                    "password2": "correct horse 1"}, follow_redirects=False)
    assert r.status_code == 303
    assert not os.path.exists(os.path.join(DATA, "setup-code.txt"))
    # setup can't be repeated
    assert client.get("/setup", follow_redirects=False).headers["location"] == "/login"


def test_login_and_admin_creates_users(client):
    assert login(client, "alex", "nope-nope-nope").status_code == 401
    r = login(client, "alex", "correct horse 1")
    assert r.status_code == 303 and "ab_session" in r.headers["set-cookie"]
    assert "HttpOnly" in r.headers["set-cookie"] and "SameSite=strict" in r.headers["set-cookie"]
    assert client.get("/api/me").json() == {"username": "alex", "role": "admin"}
    for name, role in (("vic", "viewer"), ("olly", "operator")):
        r = client.post("/api/users", json={"username": name, "password": f"{name}-password-1", "role": role}, headers=H)
        assert r.status_code == 200, r.text
    # weak password rejected
    assert client.post("/api/users", json={"username": "x", "password": "short", "role": "viewer"},
                       headers=H).status_code == 400


MATRIX = [
    # (method, path, minimum role)
    ("GET", "/api/status", "viewer"), ("GET", "/api/history", "viewer"), ("GET", "/api/recordings", "viewer"),
    ("POST", "/api/relay/auto", "operator"), ("POST", "/api/ptt/release", "operator"),
    ("POST", "/api/sim/off", "operator"), ("POST", "/api/test/hangup", "operator"),
    ("GET", "/api/config", "admin"), ("GET", "/api/users", "admin"), ("GET", "/api/audit", "admin"),
    ("GET", "/api/sessions", "admin"),
]
RANK = {"viewer": 0, "operator": 1, "admin": 2}


@pytest.mark.parametrize("user,pw,role", [("vic", "vic-password-1", "viewer"),
                                          ("olly", "olly-password-1", "operator"),
                                          ("alex", "correct horse 1", "admin")])
def test_permission_matrix(client, user, pw, role):
    assert login(client, user, pw).status_code == 303
    for method, path, need in MATRIX:
        r = client.request(method, path, headers=H)
        allowed = RANK[role] >= RANK[need]
        assert (r.status_code != 403) == allowed, (user, method, path, r.status_code)


def test_csrf_header_and_origin_required(client):
    login(client, "olly", "olly-password-1")
    assert client.post("/api/relay/auto").status_code == 403                    # no header
    assert client.post("/api/relay/auto", headers={**H, "Origin": "http://evil.example"}).status_code == 403
    assert client.post("/api/relay/auto", headers={**H, "Origin": "http://testserver"}).status_code == 200


def test_lockout_after_repeated_failures(client):
    for _ in range(3):
        assert login(client, "vic", "wrong-password-x").status_code == 401
    r = login(client, "vic", "vic-password-1")                                  # right password, but locked
    assert r.status_code == 401 and "too many" in r.text
    web.auth._fails.clear()
    assert login(client, "vic", "vic-password-1").status_code == 303


def test_disable_revokes_sessions_and_last_admin_protected(client):
    login(client, "vic", "vic-password-1")
    vic_cookie = client.cookies.get("ab_session")
    login(client, "alex", "correct horse 1")
    users = {u["username"]: u for u in client.get("/api/users").json()}
    assert client.patch(f"/api/users/{users['vic']['id']}", json={"disabled": True}, headers=H).status_code == 200
    client.cookies.set("ab_session", vic_cookie)
    assert client.get("/api/status").status_code == 401                         # vic's session is gone
    login(client, "alex", "correct horse 1")
    # can't demote or delete the only admin
    r = client.patch(f"/api/users/{users['alex']['id']}", json={"role": "viewer"}, headers=H)
    assert r.status_code == 400 and "at least one active admin" in r.json()["detail"]


def test_audit_records_actions(client):
    login(client, "alex", "correct horse 1")
    actions = [a["action"] for a in client.get("/api/audit").json()]
    for a in ("setup_failed", "user_create", "login", "login_failed", "login_locked", "relay", "user_update"):
        assert a in actions, a


def test_ip_allowlist(client):
    login(client, "alex", "correct horse 1")
    web.config.data["web"]["allow_cidrs"] = "10.0.0.0/8"
    try:
        assert client.get("/api/status").status_code == 403                     # testclient is not in 10/8
    finally:
        web.config.data["web"]["allow_cidrs"] = ""
    assert client.get("/api/status").status_code == 200


def test_passwords_are_hashed():
    import sqlite3
    rows = sqlite3.connect(os.path.join(DATA, "auth.db")).execute("SELECT pw_hash FROM users").fetchall()
    assert rows and all(re.match(r"^scrypt\$", r[0]) for r in rows)
    assert all("correct horse" not in r[0] for r in rows)


def test_branding_upload_reset_and_svg_sandbox(client):
    import io
    # login page and the default images are public
    client.cookies.clear()
    assert client.get("/branding/logo").headers["content-type"].startswith("image/svg")
    assert "sandbox" in client.get("/branding/wallpaper").headers["content-security-policy"]
    login(client, "olly", "olly-password-1")                               # operator can't change it
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    assert client.post("/api/branding/logo", files={"file": ("l.png", png)}, headers=H).status_code == 403
    login(client, "alex", "correct horse 1")
    assert client.post("/api/branding/logo", files={"file": ("x.txt", b"hello")}, headers=H).status_code == 400
    assert client.post("/api/branding/logo", files={"file": ("l.png", png)}, headers=H).status_code == 200
    r = client.get("/branding/logo")
    assert r.headers["content-type"] == "image/png" and r.content == png
    assert client.get("/api/branding").json()["logo"]["custom"] is True
    assert client.delete("/api/branding/logo", headers=H).status_code == 200
    assert client.get("/branding/logo").headers["content-type"].startswith("image/svg")
    assert client.get("/api/branding").json()["logo"]["custom"] is False
