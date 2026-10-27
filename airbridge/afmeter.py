"""AF analysis: calibrated level meters, alarms, tone analysis and spectrum.

All analysis runs on the 8 kHz int16 frames used everywhere else, so it
measures exactly what is recorded and what goes to/from the NXR.
"""
import math
import threading
import time
from collections import deque

import numpy as np

RATE = 8000
FULL = 32768.0
CLIP = 32000


def dbfs_rms(x):
    if x is None or len(x) == 0:
        return -120.0
    r = math.sqrt(float(np.mean(x.astype(np.float64) ** 2))) / FULL
    return 20 * math.log10(max(r, 1e-6))


def dbfs_peak(x):
    if x is None or len(x) == 0:
        return -120.0
    p = float(np.max(np.abs(x.astype(np.int32)))) / FULL
    return 20 * math.log10(max(p, 1e-6))


def dbm_to_mv(dbm, ohms=600.0):
    return math.sqrt(10 ** (dbm / 10) / 1000 * ohms) * 1000


class LevelMeter:
    """Per-direction meter. feed() per frame from the audio thread."""

    def __init__(self, cal_dbm_at_0dbfs=0.0, hist_s=3.0):
        self.cal = float(cal_dbm_at_0dbfs)
        self.lock = threading.Lock()
        self.rms = -120.0          # last frame
        self.display = -120.0      # fast attack, slow release
        self.peak_hold = -120.0
        self._peak_t = 0.0
        self.clips = deque()       # timestamps of clipping frames
        self.buf = deque(maxlen=int(hist_s * RATE / 160))   # recent frames for analysis
        self.active = False        # whether this direction currently carries audio

    def feed(self, frame, now=None):
        now = now or time.time()
        r = dbfs_rms(frame)
        p = dbfs_peak(frame)
        with self.lock:
            self.rms = r
            self.display = r if r > self.display else max(r, self.display - 1.5)   # ~75 dB/s release
            if p >= self.peak_hold or now - self._peak_t > 1.5:
                self.peak_hold, self._peak_t = p, now
            if np.max(np.abs(frame.astype(np.int32))) >= CLIP:
                self.clips.append(now)
            while self.clips and now - self.clips[0] > 1.0:
                self.clips.popleft()
            self.buf.append(frame)

    def snapshot(self):
        with self.lock:
            d, pk, n = self.display, self.peak_hold, len(self.clips)
        dbm = d + self.cal
        return {"dbfs": round(d, 1), "peak_dbfs": round(pk, 1),
                "dbm": round(dbm, 1), "mv": round(dbm_to_mv(dbm), 1) if d > -100 else 0.0,
                "clips_1s": n, "active": self.active}

    def samples(self, n):
        with self.lock:
            frames = list(self.buf)
        if not frames:
            return np.zeros(0, dtype=np.int16)
        x = np.concatenate(frames)
        return x[-n:]


class OverStats:
    """Accumulates level stats for one over / one TX burst."""

    def __init__(self):
        self.sum_sq = 0.0
        self.n = 0
        self.peak = 0
        self.clip_frames = 0
        self.voiced_sum_sq = 0.0
        self.voiced_n = 0

    def add(self, frame):
        f = frame.astype(np.float64)
        ss = float(np.sum(f * f))
        self.sum_sq += ss
        self.n += len(f)
        pk = int(np.max(np.abs(frame.astype(np.int32)))) if len(frame) else 0
        self.peak = max(self.peak, pk)
        if pk >= CLIP:
            self.clip_frames += 1
        # 'voiced' frames: above -45 dBFS, so silence gaps don't drag the average down
        if len(f) and 10 * math.log10(max(ss / len(f), 1e-9) / FULL ** 2) > -45:
            self.voiced_sum_sq += ss
            self.voiced_n += len(f)

    def result(self):
        def db(ss, n):
            return round(10 * math.log10(max(ss / n, 1e-12) / FULL ** 2), 1) if n else None
        return {"avg_dbfs": db(self.voiced_sum_sq, self.voiced_n) if self.voiced_n else db(self.sum_sq, self.n),
                "peak_dbfs": round(20 * math.log10(max(self.peak, 1) / FULL), 1) if self.n else None,
                "clips": self.clip_frames}


def check_over_alarms(res, cfg, label):
    """Alarms for a finished over/burst. Returns a list of strings."""
    out = []
    avg = res.get("avg_dbfs")
    if avg is not None:
        if avg < float(cfg["low_dbfs"]):
            out.append(f"{label} level low ({avg:.1f} dBFS)")
        elif avg > float(cfg["high_dbfs"]):
            out.append(f"{label} level high ({avg:.1f} dBFS)")
    if res.get("clips", 0) >= int(cfg["clip_frames"]):
        out.append(f"{label} clipping ({res['clips']} frames)")
    return out


_WIN_CACHE = {}


def _bh_window(n):
    w = _WIN_CACHE.get(n)
    if w is None:
        k = np.arange(n)
        a = (0.35875, 0.48829, 0.14128, 0.01168)
        w = (a[0] - a[1] * np.cos(2 * np.pi * k / (n - 1)) + a[2] * np.cos(4 * np.pi * k / (n - 1))
             - a[3] * np.cos(6 * np.pi * k / (n - 1)))
        _WIN_CACHE[n] = w
    return w


def analyze_tone(x, cal_dbm=0.0, band=(300.0, 3400.0)):
    """Single-tone analysis: frequency, level, SINAD and THD (unweighted, in band).

    Returns None when there isn't enough audio.
    """
    n = 4096
    if len(x) < n:
        return None
    x = x[-n:].astype(np.float64) / FULL
    x = x - np.mean(x)
    w = _bh_window(n)
    spec = np.abs(np.fft.rfft(x * w)) ** 2
    freqs = np.fft.rfftfreq(n, 1 / RATE)
    lo, hi = np.searchsorted(freqs, band[0]), np.searchsorted(freqs, band[1])
    inband = spec[lo:hi]
    total = float(np.sum(inband))
    if total <= 0:
        return None
    k = lo + int(np.argmax(inband))
    # parabolic interpolation on log magnitude for the frequency
    if 1 <= k < len(spec) - 1:
        a, b, c = np.log(spec[k - 1] + 1e-30), np.log(spec[k] + 1e-30), np.log(spec[k + 1] + 1e-30)
        d = 0.5 * (a - c) / (a - 2 * b + c) if (a - 2 * b + c) != 0 else 0.0
    else:
        d = 0.0
    f0 = (k + d) * RATE / n
    half = 4                                     # BH main lobe is +/- 4 bins
    fund = float(np.sum(spec[max(k - half, 0):k + half + 1]))
    noise_dist = max(total - fund, 1e-30)
    sinad = 10 * math.log10(fund / noise_dist + 1)
    harm = 0.0
    for h in range(2, 6):
        fh = f0 * h
        if fh >= band[1]:
            break
        kh = int(round(fh * n / RATE))
        harm += float(np.sum(spec[max(kh - half, 0):kh + half + 1]))
    thd = 100 * math.sqrt(harm / fund) if fund > 0 else None
    level = 20 * math.log10(max(math.sqrt(np.mean(x ** 2)), 1e-9))
    tone_present = fund / total > 0.6
    return {"freq": round(float(f0), 1), "level_dbfs": round(level, 1), "level_dbm": round(level + cal_dbm, 1),
            "sinad_db": round(sinad, 1), "thd_pct": round(thd, 2) if thd is not None else None,
            "tone": bool(tone_present)}


def spectrum(x, bins=128, floor=-100.0):
    """Hann-windowed 1024-pt spectrum, 0-4 kHz, as `bins` dBFS values."""
    n = 1024
    if len(x) < n:
        return [floor] * bins
    x = x[-n:].astype(np.float64) / FULL
    w = np.hanning(n)
    mag = np.abs(np.fft.rfft((x - x.mean()) * w))[:-1] * 2 / np.sum(w)
    db = 20 * np.log10(np.maximum(mag, 1e-9))
    db = db.reshape(bins, -1).max(axis=1)
    return [int(max(floor, v)) for v in db]
