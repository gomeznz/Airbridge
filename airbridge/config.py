"""Web-editable JSON configuration with defaults.

Everything is set from the Setup page; the file lives in the data
directory so it survives reinstalls.
"""
import copy
import json
import os
import threading

DATA_DIR = os.environ.get("AIRBRIDGE_DATA", "/var/lib/airbridge")

DEFAULTS = {
    "general": {
        "site_name": "IC-A210 Airband Bridge",
        "simulate": False,          # no hardware: synthetic RSSI + audio
    },
    "rssi": {
        "enabled": True,
        "i2c_bus": 1,
        "i2c_address": 0x48,        # ADS1115 ADDR pin to GND
        "channel": 0,               # AIN0
        "gain_fsr_v": 4.096,        # ADC full-scale range
        "divider_ratio": 1.0,       # Vin = Vadc * ratio (e.g. 2.0 for 10k/10k)
        "sample_hz": 20,
        "smoothing": 4,             # moving-average length (samples)
        "invert": False,            # True if RSSI voltage FALLS with signal
        # optional two-point calibration to dBm (blank = volts only)
        "cal_v1": None, "cal_dbm1": None,
        "cal_v2": None, "cal_dbm2": None,
    },
    "cor": {
        "enabled": False,           # squelch/COR line from the radio
        "gpio": 17,
        "active_low": True,
        "pull_up": True,
    },
    "detect": {
        "source": "rssi",           # rssi | cor | either | both
        "on_threshold_v": 1.20,
        "off_threshold_v": 1.00,
        "attack_ms": 300,           # must stay above on-threshold this long (filters blips)
        "hang_ms": 2500,            # carrier must stay off this long to end an over
    },
    "relay": {
        "enabled": True,
        "gpio": 27,
        "active_high": True,
        "mode": "carrier",          # carrier | call | either
        "hold_ms": 0,               # extra on-time after the mode condition ends
    },
    "audio": {
        "device": "",               # blank = auto-detect C-Media/K5TRA
        "capture_rate": 48000,
        "gain_db": 0.0,
        "preroll_ms": 2000,         # audio kept from before carrier detect
    },
    "record": {
        "enabled": True,
        "format": "opus",           # opus | mp3 | wav
        "keep_days": 30,
    },
    "sip": {
        "enabled": True,
        "target_host": "192.168.1.50",   # NXR-1800 IP
        "target_port": 5060,
        "target_user": "",               # extension / number to dial (request-URI user)
        "local_port": 5062,
        "local_user": "airbridge",
        "display_name": "Airband",
        "auth_user": "",
        "auth_password": "",
        "register": False,               # REGISTER to target before calling
        "register_expires": 300,
        "codec": "PCMU",                 # PCMU (mu-law) | PCMA (A-law)
        "ptime_ms": 20,
        "dtmf_digits": "",               # sent after answer (e.g. talkgroup ID)
        "dtmf_mode": "rfc2833",          # rfc2833 | info
        "dtmf_delay_ms": 500,
        "post_answer_delay_ms": 300,     # wait before streaming audio
        "forward_preroll_ms": 600,       # pre-roll sent on the call (recording keeps full pre-roll)
        "max_call_s": 180,
        "cooldown_s": 0,                 # minimum gap between calls
        "keep_call_while_hang": True,    # new carrier during hang reuses call
        "tx_gain_db": 0.0,
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
        "password": "",                  # blank = no login
    },
}


def _merge(base, over):
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    def __init__(self, path=None):
        self.path = path or os.path.join(DATA_DIR, "config.json")
        self._lock = threading.Lock()
        self.data = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self):
        if os.path.exists(self.path):
            with open(self.path) as f:
                self.data = _merge(DEFAULTS, json.load(f))
        return self.data

    def save(self, new):
        with self._lock:
            self.data = _merge(DEFAULTS, new)
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        return self.data

    def __getitem__(self, k):
        return self.data[k]
