"""Per-over recordings: WAV while live, then compressed with ffmpeg.

Metadata (time, duration, RSSI, forward result) lives in SQLite.
"""
import logging
import os
import shutil
import sqlite3
import subprocess
import threading
import time
import wave
from datetime import datetime

log = logging.getLogger("rec")

SCHEMA = """
CREATE TABLE IF NOT EXISTS overs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  started REAL NOT NULL,
  ended REAL,
  duration REAL,
  file TEXT,
  peak_v REAL,
  avg_v REAL,
  forward TEXT,          -- sent | failed | skipped | disabled | pending
  forward_detail TEXT
);
CREATE INDEX IF NOT EXISTS overs_started ON overs(started);
"""

# columns added after the first release; added in place on older databases
MIGRATIONS = {
    "direction": "TEXT DEFAULT 'rx'",      # rx = airband -> NXR, tx = NXR -> airband
    "avg_dbfs": "REAL",
    "peak_dbfs": "REAL",
    "clips": "INTEGER",
    "alarms": "TEXT",
    "lag_ms": "REAL",                     # how far behind live the audio was delivered
}


class Store:
    def __init__(self, data_dir):
        self.dir = os.path.join(data_dir, "recordings")
        os.makedirs(self.dir, exist_ok=True)
        self.db_path = os.path.join(data_dir, "airbridge.db")
        self._lock = threading.Lock()
        with self._conn() as c:
            c.executescript(SCHEMA)
            have = {r[1] for r in c.execute("PRAGMA table_info(overs)")}
            for col, decl in MIGRATIONS.items():
                if col not in have:
                    c.execute(f"ALTER TABLE overs ADD COLUMN {col} {decl}")

    def _conn(self):
        c = sqlite3.connect(self.db_path, timeout=10)
        c.row_factory = sqlite3.Row
        return c

    def new_over(self, started, direction="rx", forward="pending"):
        with self._lock, self._conn() as c:
            cur = c.execute("INSERT INTO overs(started, direction, forward) VALUES (?, ?, ?)",
                            (started, direction, forward))
            return cur.lastrowid

    def update(self, oid, **kw):
        if not kw:
            return
        cols = ", ".join(f"{k}=?" for k in kw)
        with self._lock, self._conn() as c:
            c.execute(f"UPDATE overs SET {cols} WHERE id=?", (*kw.values(), oid))

    def list(self, limit=100, offset=0):
        with self._conn() as c:
            rows = c.execute("SELECT * FROM overs ORDER BY started DESC LIMIT ? OFFSET ?",
                             (limit, offset)).fetchall()
            total = c.execute("SELECT COUNT(*) FROM overs").fetchone()[0]
        return [dict(r) for r in rows], total

    def get(self, oid):
        with self._conn() as c:
            r = c.execute("SELECT * FROM overs WHERE id=?", (oid,)).fetchone()
        return dict(r) if r else None

    def delete(self, oid):
        r = self.get(oid)
        if not r:
            return False
        if r["file"]:
            try:
                os.remove(os.path.join(self.dir, r["file"]))
            except FileNotFoundError:
                pass
        with self._lock, self._conn() as c:
            c.execute("DELETE FROM overs WHERE id=?", (oid,))
        return True

    def stats_today(self):
        start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        with self._conn() as c:
            r = c.execute("SELECT SUM(direction='rx'), COALESCE(SUM(CASE WHEN direction='rx' THEN duration END),0), "
                          "SUM(forward='sent'), SUM(forward='failed'), SUM(forward='transmitted'), "
                          "SUM(forward='dropped') FROM overs WHERE started>=?",
                          (start,)).fetchone()
        return {"overs": r[0] or 0, "seconds": round(r[1] or 0, 1), "sent": r[2] or 0, "failed": r[3] or 0,
                "transmitted": r[4] or 0, "dropped": r[5] or 0}

    def purge(self, keep_days):
        if not keep_days or keep_days <= 0:
            return 0
        cutoff = time.time() - keep_days * 86400
        with self._conn() as c:
            ids = [r[0] for r in c.execute("SELECT id FROM overs WHERE started<?", (cutoff,))]
        for i in ids:
            self.delete(i)
        return len(ids)


class OverRecording:
    """Collects 8 kHz int16 frames for one over."""

    def __init__(self, store: Store, oid, started, fmt):
        self.store = store
        self.oid = oid
        self.fmt = fmt if shutil.which("ffmpeg") else "wav"
        stamp = datetime.fromtimestamp(started).strftime("%Y%m%d-%H%M%S")
        day = datetime.fromtimestamp(started).strftime("%Y-%m-%d")
        os.makedirs(os.path.join(store.dir, day), exist_ok=True)
        self.rel_base = os.path.join(day, f"{stamp}-{oid}")
        self.wav_path = os.path.join(store.dir, self.rel_base + ".wav")
        self.w = wave.open(self.wav_path, "wb")
        self.w.setnchannels(1)
        self.w.setsampwidth(2)
        self.w.setframerate(8000)
        self.samples = 0

    def write(self, frame):
        self.w.writeframes(frame.tobytes())
        self.samples += len(frame)

    def close(self):
        self.w.close()
        rel = self.rel_base + ".wav"
        if self.fmt in ("opus", "mp3"):
            ext = ".ogg" if self.fmt == "opus" else ".mp3"
            out = os.path.join(self.store.dir, self.rel_base + ext)
            args = (["-c:a", "libopus", "-b:a", "16k", "-application", "voip"] if self.fmt == "opus"
                    else ["-c:a", "libmp3lame", "-b:a", "32k"])
            try:
                subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
                                "-i", self.wav_path, *args, out], check=True, timeout=120)
                os.remove(self.wav_path)
                rel = self.rel_base + ext
            except Exception as e:
                log.warning("compress failed, keeping wav: %s", e)
        self.store.update(self.oid, file=rel)
        return rel
