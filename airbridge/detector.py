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


LEGACY_SOURCE = {"rssi": (["rssi"], "any"), "cor": (["cor"], "any"),
                 "either": (["rssi", "cor"], "any"), "both": (["rssi", "cor"], "all")}


def sources_of(d):
    if "sources" in d and d["sources"] is not None:
        return list(d["sources"])
    return LEGACY_SOURCE.get(d.get("source", "rssi"), (["rssi"], "any"))[0]


class AfDetector:
    """Carrier detection from the receiver audio itself.

    level -- the A210 squelch is set: any audio above the threshold means
             a carrier (simple, but misses an unmodulated carrier).
    noise -- the A210 squelch is left open: receiver noise above the voice
             band (default 4-7 kHz, measured on the raw capture) collapses
             when a carrier arrives. Classic noise squelch; it also
             catches dead carriers and is not fooled by loud speech.
    """

    def __init__(self, cfg):
        self.cfg = cfg
        self.win = deque(maxlen=max(1, int(cfg["smoothing"])))
        self.latched = False
        self.value = None            # smoothed dB used for the decision
        self.error = None

    def update(self, level_dbfs, hf_dbfs):
        mode = self.cfg["mode"]
        v = level_dbfs if mode == "level" else hf_dbfs
        if v is None:
            self.error = None if mode == "level" else "noise mode needs capture_rate 48000 (no HF band)"
            return self.latched
        self.error = None
        self.win.append(v)
        self.value = sum(self.win) / len(self.win)
        if mode == "level":
            on, off = float(self.cfg["level_on_dbfs"]), float(self.cfg["level_off_dbfs"])
            if self.value >= on:
                self.latched = True
            elif self.value <= off:
                self.latched = False
        else:
            # carrier = noise quieted BELOW the 'on' level
            on, off = float(self.cfg["noise_on_dbfs"]), float(self.cfg["noise_off_dbfs"])
            if self.value <= on:
                self.latched = True
            elif self.value >= off:
                self.latched = False
        return self.latched

    def reset(self):
        self.win.clear()
        self.latched = False
        self.value = None


class CarrierDetector:
    def __init__(self, detect_cfg, rssi_cfg):
        self.d = detect_cfg
        self.r = rssi_cfg
        self.win = deque(maxlen=max(1, int(rssi_cfg["smoothing"])))
        self.rssi_latched = False
        self.state = IDLE
        self.rssi = None            # smoothed volts
        self.cor = None
        self.af = None
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

    def sources(self):
        return sources_of(self.d)

    def raw_active(self):
        srcs = self.sources()
        vals = []
        if "rssi" in srcs:
            vals.append(self._rssi_active(self.rssi))
        if "cor" in srcs:
            vals.append(bool(self.cor))
        if "af" in srcs:
            vals.append(bool(self.af))
        if not vals:
            return False
        return all(vals) if self.d.get("combine") == "all" else any(vals)

    def update(self, rssi_v, cor, now, af=None):
        """Feed one sample. Returns 'start', 'end' or None."""
        if rssi_v is not None:
            self.win.append(rssi_v)
            self.rssi = sum(self.win) / len(self.win)
        self.cor = cor
        self.af = af
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
