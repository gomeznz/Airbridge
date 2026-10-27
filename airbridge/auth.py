"""Access control: user accounts, roles, sessions, lockout and audit log.

Roles (each includes the ones below it):
  viewer   -- status, history, audio analysis, listen to recordings
  operator -- + test calls, TX test tone, PTT release, relay override,
              hang up, delete recordings, simulator controls
  admin    -- + settings, user management, sessions, audit log

Passwords are stored as scrypt hashes. Sessions are random tokens kept
server-side (only a SHA-256 of the token is stored), with idle and
absolute expiry. Repeated failed logins lock the user and the source IP.
"""
import hashlib
import hmac
import ipaddress
import logging
import os
import secrets
import sqlite3
import threading
import time

log = logging.getLogger("auth")

ROLES = ("viewer", "operator", "admin")
RANK = {r: i for i, r in enumerate(ROLES)}
MIN_PASSWORD = 10

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  username TEXT NOT NULL UNIQUE COLLATE NOCASE,
  pw_hash TEXT NOT NULL,
  role TEXT NOT NULL,
  disabled INTEGER NOT NULL DEFAULT 0,
  created REAL NOT NULL,
  last_login REAL
);
CREATE TABLE IF NOT EXISTS sessions (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  token_hash TEXT NOT NULL UNIQUE,
  user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
  created REAL NOT NULL,
  last_seen REAL NOT NULL,
  ip TEXT,
  agent TEXT
);
CREATE TABLE IF NOT EXISTS audit (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  t REAL NOT NULL,
  username TEXT,
  ip TEXT,
  action TEXT NOT NULL,
  detail TEXT
);
CREATE INDEX IF NOT EXISTS audit_t ON audit(t);
"""

_N, _R, _P = 2 ** 14, 8, 1


def hash_password(pw: str) -> str:
    salt = os.urandom(16)
    h = hashlib.scrypt(pw.encode(), salt=salt, n=_N, r=_R, p=_P, dklen=32)
    return f"scrypt${_N}${_R}${_P}${salt.hex()}${h.hex()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, h = stored.split("$")
        if algo != "scrypt":
            return False
        calc = hashlib.scrypt(pw.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(bytes.fromhex(h)))
        return hmac.compare_digest(calc.hex(), h)
    except Exception:
        return False


_DUMMY = hash_password(secrets.token_hex(8))          # equal-time check for unknown users


def _th(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class AuthError(Exception):
    pass


class Auth:
    def __init__(self, data_dir, web_cfg):
        self.cfg = web_cfg
        self.path = os.path.join(data_dir, "auth.db")
        os.makedirs(data_dir, exist_ok=True)
        self._lock = threading.Lock()
        with self._c() as c:
            c.executescript(SCHEMA)
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._fails = {}               # key -> [timestamps]
        self.setup_code = None
        self.setup_code_path = os.path.join(data_dir, "setup-code.txt")

    def _c(self):
        c = sqlite3.connect(self.path, timeout=10)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys = ON")
        return c

    # ---------- first run ----------
    def user_count(self):
        with self._c() as c:
            return c.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def bootstrap(self, legacy_password=""):
        """Called at start-up. Returns a note for the log, or None."""
        if self.user_count():
            self._clear_setup_code()
            return None
        if legacy_password:
            self.create_user("admin", legacy_password, "admin", by="system", ip=None, check_policy=False)
            return "migrated the old web password to user 'admin' (role admin) - change it on the Access tab"
        self.setup_code = "-".join(secrets.token_hex(2).upper() for _ in range(3))
        try:
            fd = os.open(self.setup_code_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(self.setup_code + "\n")
        except OSError as e:
            log.warning("could not write setup code file: %s", e)
        return (f"no users yet: open the web page and create the first admin with setup code "
                f"{self.setup_code} (also in {self.setup_code_path})")

    def _clear_setup_code(self):
        self.setup_code = None
        try:
            os.remove(self.setup_code_path)
        except FileNotFoundError:
            pass

    def first_admin(self, code, username, password, ip):
        if self.user_count():
            raise AuthError("setup already completed")
        if self.check_locked("setup", ip):
            raise AuthError("too many attempts; wait and try again")
        if not self.setup_code or not hmac.compare_digest(code.strip().upper(), self.setup_code):
            self._fail("setup", ip)
            self.audit(None, ip, "setup_failed", "wrong setup code")
            raise AuthError("wrong setup code")
        self.create_user(username, password, "admin", by=username, ip=ip)
        self._clear_setup_code()

    # ---------- users ----------
    @staticmethod
    def _policy(username, password):
        if not username or len(username) > 32 or not all(ch.isalnum() or ch in "._-" for ch in username):
            raise AuthError("username: 1-32 letters, digits, . _ -")
        if len(password) < MIN_PASSWORD:
            raise AuthError(f"password must be at least {MIN_PASSWORD} characters")
        if password.lower() == username.lower():
            raise AuthError("password must not be the username")

    def create_user(self, username, password, role, by, ip, check_policy=True):
        if role not in ROLES:
            raise AuthError("bad role")
        if check_policy:
            self._policy(username, password)
        with self._lock, self._c() as c:
            try:
                c.execute("INSERT INTO users(username, pw_hash, role, created) VALUES (?,?,?,?)",
                          (username, hash_password(password), role, time.time()))
            except sqlite3.IntegrityError:
                raise AuthError("username already exists")
        self.audit(by, ip, "user_create", f"{username} ({role})")

    def list_users(self):
        with self._c() as c:
            rows = c.execute("SELECT u.id, u.username, u.role, u.disabled, u.created, u.last_login, "
                             "(SELECT COUNT(*) FROM sessions s WHERE s.user_id=u.id) AS sessions "
                             "FROM users u ORDER BY u.username").fetchall()
        return [dict(r) for r in rows]

    def get_user(self, uid):
        with self._c() as c:
            r = c.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()
        return dict(r) if r else None

    def _admins_left(self, c, excluding):
        return c.execute("SELECT COUNT(*) FROM users WHERE role='admin' AND disabled=0 AND id<>?",
                         (excluding,)).fetchone()[0]

    def update_user(self, uid, by, ip, role=None, password=None, disabled=None):
        u = self.get_user(uid)
        if not u:
            raise AuthError("no such user")
        with self._lock, self._c() as c:
            losing_admin = u["role"] == "admin" and not u["disabled"] and (
                (role is not None and role != "admin") or disabled)
            if losing_admin and not self._admins_left(c, uid):
                raise AuthError("there must be at least one active admin")
            changes = []
            if role is not None:
                if role not in ROLES:
                    raise AuthError("bad role")
                c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
                changes.append(f"role={role}")
            if password is not None:
                self._policy(u["username"], password)
                c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(password), uid))
                changes.append("password reset")
            if disabled is not None:
                c.execute("UPDATE users SET disabled=? WHERE id=?", (1 if disabled else 0, uid))
                changes.append("disabled" if disabled else "enabled")
            if password is not None or disabled or role is not None:
                c.execute("DELETE FROM sessions WHERE user_id=?", (uid,))      # force re-login
        self.audit(by, ip, "user_update", f"{u['username']}: {', '.join(changes)}")

    def delete_user(self, uid, by, ip):
        u = self.get_user(uid)
        if not u:
            raise AuthError("no such user")
        with self._lock, self._c() as c:
            if u["role"] == "admin" and not self._admins_left(c, uid):
                raise AuthError("there must be at least one active admin")
            c.execute("DELETE FROM users WHERE id=?", (uid,))
        self.audit(by, ip, "user_delete", u["username"])

    def change_own_password(self, user, current, new, ip):
        u = self.get_user(user["id"])
        if not verify_password(current, u["pw_hash"]):
            self.audit(user["username"], ip, "password_change_failed", "wrong current password")
            raise AuthError("current password is wrong")
        self._policy(u["username"], new)
        with self._lock, self._c() as c:
            c.execute("UPDATE users SET pw_hash=? WHERE id=?", (hash_password(new), u["id"]))
            # keep this session, drop the others
            c.execute("DELETE FROM sessions WHERE user_id=? AND id<>?", (u["id"], user["session_id"]))
        self.audit(u["username"], ip, "password_change", None)

    # ---------- login / lockout ----------
    def _fail(self, *keys):
        now = time.time()
        for k in keys:
            self._fails.setdefault(k, []).append(now)

    def check_locked(self, *keys):
        now = time.time()
        win = float(self.cfg.get("lockout_minutes", 15)) * 60
        lim = int(self.cfg.get("lockout_attempts", 5))
        for k in keys:
            v = [t for t in self._fails.get(k, []) if now - t < win]
            self._fails[k] = v
            if len(v) >= lim:
                return True
        return False

    def login(self, username, password, ip, agent):
        ukey, ikey = f"u:{username.lower()}", f"ip:{ip}"
        if self.check_locked(ukey, ikey):
            self.audit(username, ip, "login_locked", None)
            raise AuthError("too many failed attempts; try again later")
        with self._c() as c:
            u = c.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
        ok = verify_password(password, u["pw_hash"] if u else _DUMMY)
        if not u or not ok or u["disabled"]:
            self._fail(ukey, ikey)
            self.audit(username, ip, "login_failed", "disabled" if (u and ok) else None)
            raise AuthError("wrong username or password")
        self._fails.pop(ukey, None)
        token = secrets.token_urlsafe(32)
        now = time.time()
        with self._lock, self._c() as c:
            c.execute("INSERT INTO sessions(token_hash, user_id, created, last_seen, ip, agent) "
                      "VALUES (?,?,?,?,?,?)", (_th(token), u["id"], now, now, ip, (agent or "")[:200]))
            c.execute("UPDATE users SET last_login=? WHERE id=?", (now, u["id"]))
        self.audit(u["username"], ip, "login", None)
        return token

    def session(self, token, ip=None, touch=True):
        """Return the user dict for a valid session token, else None."""
        if not token:
            return None
        now = time.time()
        idle = float(self.cfg.get("session_idle_hours", 12)) * 3600
        life = float(self.cfg.get("session_max_days", 30)) * 86400
        with self._c() as c:
            r = c.execute("SELECT s.id AS session_id, s.created, s.last_seen, u.id, u.username, u.role, u.disabled "
                          "FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=?",
                          (_th(token),)).fetchone()
            if not r:
                return None
            if r["disabled"] or now - r["last_seen"] > idle or now - r["created"] > life:
                c.execute("DELETE FROM sessions WHERE id=?", (r["session_id"],))
                return None
            if touch and now - r["last_seen"] > 60:
                c.execute("UPDATE sessions SET last_seen=?, ip=COALESCE(?, ip) WHERE id=?",
                          (now, ip, r["session_id"]))
        return dict(r)

    def logout(self, token, ip):
        u = self.session(token, touch=False)
        with self._lock, self._c() as c:
            c.execute("DELETE FROM sessions WHERE token_hash=?", (_th(token or ""),))
        if u:
            self.audit(u["username"], ip, "logout", None)

    def list_sessions(self):
        with self._c() as c:
            rows = c.execute("SELECT s.id, u.username, s.created, s.last_seen, s.ip, s.agent FROM sessions s "
                             "JOIN users u ON u.id=s.user_id ORDER BY s.last_seen DESC").fetchall()
        return [dict(r) for r in rows]

    def revoke_session(self, sid, by, ip):
        with self._lock, self._c() as c:
            r = c.execute("SELECT u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.id=?",
                          (sid,)).fetchone()
            c.execute("DELETE FROM sessions WHERE id=?", (sid,))
        if r:
            self.audit(by, ip, "session_revoke", r["username"])

    # ---------- audit ----------
    def audit(self, username, ip, action, detail):
        try:
            with self._c() as c:
                c.execute("INSERT INTO audit(t, username, ip, action, detail) VALUES (?,?,?,?,?)",
                          (time.time(), username, ip, action, detail))
                c.execute("DELETE FROM audit WHERE t < ?", (time.time() - 365 * 86400,))
        except Exception as e:
            log.warning("audit: %s", e)

    def audit_log(self, limit=200, offset=0):
        with self._c() as c:
            rows = c.execute("SELECT * FROM audit ORDER BY t DESC LIMIT ? OFFSET ?", (limit, offset)).fetchall()
        return [dict(r) for r in rows]


# ---------- network helpers ----------
def client_ip(scope_client, headers, trust_proxy):
    if trust_proxy:
        for h in ("cf-connecting-ip", "x-real-ip"):
            v = headers.get(h)
            if v:
                return v.strip()
        xff = headers.get("x-forwarded-for")
        if xff:
            return xff.split(",")[0].strip()
    return scope_client[0] if scope_client else "?"


def ip_allowed(ip, allow):
    allow = [a.strip() for a in (allow or "").replace("\n", ",").split(",") if a.strip()]
    if not allow:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for a in allow:
        try:
            if addr in ipaddress.ip_network(a, strict=False):
                return True
        except ValueError:
            continue
    return False
