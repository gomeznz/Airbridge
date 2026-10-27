"""Long RSSI / detection history at 1 s resolution (7 days), saved to disk.

Each second stores: time, RSSI min/mean/max, carrier, transmitting,
floor, effective on/off thresholds and the AF detector value. Used for
the history chart and to suggest adaptive offsets from real site data.
"""
import logging
import math
import os
import threading

import numpy as np

log = logging.getLogger("hist")

COLS = ("t", "min", "mean", "max", "carrier", "tx", "floor", "on", "off", "af")
NAN = float("nan")


class History:
    def __init__(self, path, keep_s=7 * 86400):
        self.path = path
        self.keep = keep_s
        self.lock = threading.Lock()
        self.data = np.full((keep_s, len(COLS)), np.nan, dtype=np.float32)
        self.data_t = np.full(keep_s, np.nan, dtype=np.float64)    # float32 can't hold epoch seconds
        self.n = 0                      # rows written in total (ring index = n % keep)
        self._cur = None
        self._load()

    # ---------- feeding ----------
    def add(self, t, rssi, carrier, tx, floor, on, off, af):
        sec = int(t)
        with self.lock:
            c = self._cur
            if c is None or c["sec"] != sec:
                if c is not None:
                    self._commit(c)
                c = self._cur = {"sec": sec, "vals": [], "carrier": False, "tx": False,
                                 "floor": NAN, "on": NAN, "off": NAN, "af": []}
            if rssi is not None:
                c["vals"].append(rssi)
            c["carrier"] |= bool(carrier)
            c["tx"] |= bool(tx)
            c["floor"] = NAN if floor is None else floor
            c["on"], c["off"] = on, off
            if af is not None:
                c["af"].append(af)

    def _commit(self, c):
        v = c["vals"]
        row = (c["sec"], min(v) if v else NAN, sum(v) / len(v) if v else NAN, max(v) if v else NAN,
               float(c["carrier"]), float(c["tx"]), c["floor"], c["on"], c["off"],
               sum(c["af"]) / len(c["af"]) if c["af"] else NAN)
        i = self.n % self.keep
        self.data[i] = row
        self.data_t[i] = c["sec"]
        self.n += 1

    # ---------- reading ----------
    def _rows(self, seconds, now):
        with self.lock:
            m = min(self.n, self.keep)
            if not m:
                return np.zeros((0, len(COLS)), np.float64)
            idx = (np.arange(self.n - m, self.n) % self.keep)
            d = self.data[idx].astype(np.float64)
            d[:, 0] = self.data_t[idx]
        return d[d[:, 0] >= now - seconds]

    def query(self, seconds, now, points=600):
        d = self._rows(seconds, now)
        if len(d) == 0:
            return {"cols": COLS, "rows": [], "step": 1}
        step = max(1, math.ceil(seconds / points))
        if step > 1:
            g = ((d[:, 0] - (now - seconds)) // step).astype(int)
            out = []
            cuts = np.flatnonzero(np.diff(g)) + 1          # rows are in time order
            for b in np.split(d, cuts):
                with np.errstate(all="ignore"):
                    out.append([b[0, 0], np.nanmin(b[:, 1]) if np.isfinite(b[:, 1]).any() else NAN,
                                np.nanmean(b[:, 2]) if np.isfinite(b[:, 2]).any() else NAN,
                                np.nanmax(b[:, 3]) if np.isfinite(b[:, 3]).any() else NAN,
                                b[:, 4].max(), b[:, 5].max(),
                                *(np.nanmean(b[:, j]) if np.isfinite(b[:, j]).any() else NAN for j in (6, 7, 8, 9))])
            d = np.array(out)
        rows = [[None if not np.isfinite(x) else round(float(x), 4) for x in r] for r in d]
        for r in rows:
            r[0] = int(r[0])
        return {"cols": COLS, "rows": rows, "step": step}

    def stats(self, seconds, now, invert=False):
        """Idle spread above the floor, typical carrier lift, and suggested offsets."""
        d = self._rows(seconds, now)
        ok = np.isfinite(d[:, 3]) & np.isfinite(d[:, 6]) if len(d) else np.zeros(0, bool)
        d = d[ok]
        idle = d[(d[:, 4] == 0) & (d[:, 5] == 0)]
        carr = d[(d[:, 4] == 1) & (d[:, 5] == 0)]
        sign = -1.0 if invert else 1.0
        out = {"idle_s": int(len(idle)), "carrier_s": int(len(carr)),
               "floor_min": None, "floor_max": None, "idle_p95": None, "carrier_lift": None,
               "suggest_on": None, "suggest_off": None}
        if len(idle) >= 60:
            dev = sign * ((idle[:, 1] if invert else idle[:, 3]) - idle[:, 6])   # worst-case excursion above floor
            out["idle_p95"] = round(float(np.percentile(dev, 95)), 4)
            out["floor_min"] = round(float(np.min(idle[:, 6])), 4)
            out["floor_max"] = round(float(np.max(idle[:, 6])), 4)
        if len(carr) >= 3:
            lift = sign * ((carr[:, 1] if invert else carr[:, 3]) - carr[:, 6])
            out["carrier_lift"] = round(float(np.median(lift)), 4)
        if out["idle_p95"] is not None:
            on = max(out["idle_p95"] * 2.5, 0.03)
            if out["carrier_lift"]:
                on = min(on, out["carrier_lift"] * 0.5)
            off = max(on * 0.5, out["idle_p95"] * 1.2)
            if off >= on:
                off = on * 0.8
            out["suggest_on"], out["suggest_off"] = round(on, 3), round(off, 3)
        return out

    # ---------- persistence ----------
    def save(self):
        try:
            with self.lock:
                tmp = self.path + ".tmp.npz"
                np.savez_compressed(tmp, data=self.data, t=self.data_t, n=np.array([self.n]))
            os.replace(tmp, self.path)
        except Exception as e:
            log.warning("history save: %s", e)

    def _load(self):
        if not os.path.exists(self.path):
            return
        try:
            z = np.load(self.path)
            data, tt, n = z["data"], z["t"], int(z["n"][0])
            if data.shape == self.data.shape:
                self.data[:], self.data_t[:], self.n = data, tt, n
            elif data.shape[1] == self.data.shape[1]:
                # saved with a different length: replay the newest rows in time order
                old = data.shape[0]
                m = min(n, old, self.keep)
                idx = np.arange(n - m, n) % old
                self.data[:m], self.data_t[:m], self.n = data[idx], tt[idx], m
        except Exception as e:
            log.warning("history load: %s", e)
