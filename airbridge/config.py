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
        "threshold_mode": "adaptive",   # adaptive (track the idle floor) | fixed (detect.on/off_threshold_v)
        "floor_window_s": 120,      # idle history used for the floor
        "floor_percentile": 20,     # low percentile of idle samples = floor
        "on_offset_v": 0.15,        # adaptive: carrier when RSSI >= floor + this
        "off_offset_v": 0.08,       # adaptive: clear when RSSI <= floor + this
        "max_carrier_s": 120,       # adaptive: RSSI 'carrier' longer than this = the floor moved; re-learn (0 = off)
        # optional two-point calibration to dBm (blank = volts only)
        "cal_v1": None, "cal_dbm1": None,
        "cal_v2": None, "cal_dbm2": None,
    },
    "cor": {
        "enabled": False,           # squelch/COR line from the radio
        "backend": "k5tra",         # k5tra (K5TRA COR input via CM119A HID) | gpio (Pi pin)
        "gpio": 17,
        "active_low": True,
        "pull_up": True,
    },
    "detect": {
        "sources": ["rssi"],        # any of: rssi, cor, af
        "combine": "any",           # any = OR of the sources, all = AND
        "on_threshold_v": 1.20,
        "off_threshold_v": 1.00,
        "attack_ms": 300,           # must stay above on-threshold this long (filters blips)
        "hang_ms": 2500,            # carrier must stay off this long to end an over
    },
    "afdetect": {
        "mode": "noise",            # noise (A210 squelch open) | level (A210 squelch set)
        "smoothing": 5,             # frames (20 ms each)
        "level_on_dbfs": -35.0,     # level mode: audio above this = carrier
        "level_off_dbfs": -42.0,
        "noise_lo_hz": 4000,        # noise mode: band measured on the raw capture
        "noise_hi_hz": 7000,
        "noise_on_dbfs": -60.0,     # noise mode: band noise BELOW this = carrier (quieted)
        "noise_off_dbfs": -52.0,
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
        "tx_enabled": True,         # use the K5TRA output (NXR audio -> A210)
        "out_gain_db": 0.0,         # K5TRA output level into the A210
    },
    "k5tra": {
        "hid_device": "",           # blank = first C-Media /dev/hidraw*
        "ptt_gpio": 3,              # CM119A GPIO used for PTT (AllStar convention: 3)
        "ptt_invert": False,
        "cos_bit": 1,               # HID input bit for COS (VOLDN = 1)
        "cos_invert": False,
    },
    "tx": {
        "enabled": True,            # key the A210 with audio from the NXR
        "vox_dbfs": -40.0,          # NXR audio above this keys the radio
        "vox_hang_ms": 800,         # keep keyed this long after NXR audio stops
        "ptt_lead_ms": 150,         # key-up before audio starts
        "ptt_tail_ms": 100,         # hold after the last audio
        "jitter_ms": 60,            # receive buffer before playout starts
        "tot_s": 60,                # transmit time-out
        "rx_guard_ms": 600,         # ignore RSSI/COR this long after unkeying
        "record": True,             # record NXR -> airband bursts too
    },
    "af": {
        "rx_cal_dbm": 0.0,          # dBm (600 ohm) that 0 dBFS represents on K5TRA input
        "tx_cal_dbm": 0.0,          # dBm (600 ohm) that 0 dBFS represents on K5TRA output
        "low_dbfs": -30.0,          # over average below this -> 'level low' alarm
        "high_dbfs": -6.0,          # over average above this -> 'level high' alarm
        "clip_frames": 5,           # clipped frames per over before a clipping alarm
        "no_audio_dbfs": -50.0,     # carrier present but audio below this ...
        "no_audio_s": 3.0,          # ... for this long -> 'carrier, no audio' alarm
        "tone_freq": 1000.0,        # expected test-tone frequency
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
        "max_call_s": 600,
        "cooldown_s": 0,                 # minimum gap between calls
        "send_gain_db": 0.0,             # airband audio level sent to the NXR
        "reply_hold_s": 10,              # keep an outbound call up this long for replies
        "inbound_enabled": True,         # accept calls from the NXR
        "inbound_allow": "",             # extra allowed source IPs (comma list); target_host always allowed
        "inbound_idle_s": 120,           # hang up an inbound call idle this long (0 = never)
    },
    "web": {
        "host": "0.0.0.0",
        "port": 8080,
        "password": "",                  # legacy single password: migrated to an 'admin' user, then cleared
        "allow_cidrs": "",               # comma list of allowed client networks (blank = any), e.g. 192.168.1.0/24
        "trust_proxy": False,            # behind Cloudflare Tunnel / reverse proxy: use CF-Connecting-IP / X-Forwarded-For
        "session_idle_hours": 12,
        "session_max_days": 30,
        "lockout_attempts": 5,           # failed logins (per user and per IP) ...
        "lockout_minutes": 15,           # ... within this window lock further attempts
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
                raw = json.load(f)
            det = raw.get("detect", {})
            if "source" in det and "sources" not in det:      # pre-AF-detect config
                from .detector import LEGACY_SOURCE
                det["sources"], det["combine"] = LEGACY_SOURCE.get(det.pop("source"), (["rssi"], "any"))
            det.pop("source", None)
            self.data = _merge(DEFAULTS, raw)
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
