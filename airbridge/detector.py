"""Carrier detector: smoothing, hysteresis, attack and hang timing.

Pure logic with an injectable clock so it can be unit-tested.
"""
from collections import deque

IDLE, ATTACK, ACTIVE, HANG = "idle", "attack", "active", "hang"


def volts_to_dbm(v, cfg):
    """Two-point linear calibration; returns None if not calibrated."""
    try:
        v1, d1 = float(cfg["cal_v1"]), float(cfg["cal_dbm1"])
        v2, d2 = float(cfg["cal_v2"]), float(cfg["cal_dbm2"])
    except (TypeError, ValueError, KeyError):
        return None
    if v is None or v1 == v2:
        return None
    return d1 + (v - v1) * (d2 - d1) / (v2 - v1)


class CarrierDetector:
    def __init__(self, detect_cfg, rssi_cfg):
        self.d = detect_cfg
        self.r = rssi_cfg
        self.win = deque(maxlen=max(1, int(rssi_cfg["smoothing"])))
        self.rssi_latched = False
        self.state = IDLE
        self.rssi = None            # smoothed volts
        self.cor = None
        self._since = 0.0           # time the current raw condition began
        self.over_start = None      # time the raw carrier first appeared

    def _rssi_active(self, v):
        if v is None:
            return False
        on, off = float(self.d["on_threshold_v"]), float(self.d["off_threshold_v"])
        if self.r.get("invert"):
            # signal makes the voltage fall: mirror the comparison
            if v <= on:
                self.rssi_latched = True
            elif v >= off:
                self.rssi_latched = False
        else:
            if v >= on:
                self.rssi_latched = True
            elif v <= off:
                self.rssi_latched = False
        return self.rssi_latched

    def raw_active(self):
        src = self.d["source"]
        r = self._rssi_active(self.rssi)
        c = bool(self.cor)
        if src == "rssi":
            return r
        if src == "cor":
            return c
        if src == "both":
            return r and c
        return r or c                      # either

    def update(self, rssi_v, cor, now):
        """Feed one sample. Returns 'start', 'end' or None."""
        if rssi_v is not None:
            self.win.append(rssi_v)
            self.rssi = sum(self.win) / len(self.win)
        self.cor = cor
        active = self.raw_active()
        attack = float(self.d["attack_ms"]) / 1000
        hang = float(self.d["hang_ms"]) / 1000

        if self.state == IDLE:
            if active:
                self.state, self._since = ATTACK, now
                if attack <= 0:
                    return self._start(now)
        elif self.state == ATTACK:
            if not active:
                self.state = IDLE
            elif now - self._since >= attack:
                return self._start(self._since)
        elif self.state == ACTIVE:
            if not active:
                self.state, self._since = HANG, now
        elif self.state == HANG:
            if active:
                self.state = ACTIVE
            elif now - self._since >= hang:
                self.state = IDLE
                return "end"
        return None

    def _start(self, t):
        self.state = ACTIVE
        self.over_start = t
        return "start"

    @property
    def carrier(self):
        return self.state in (ACTIVE, HANG)
