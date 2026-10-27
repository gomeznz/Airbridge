"""Unit tests for the detector and G.711 codecs: python3 -m pytest tests"""
import numpy as np

from airbridge.detector import CarrierDetector, volts_to_dbm
from airbridge.g711 import CODECS

DET = {"source": "rssi", "on_threshold_v": 1.2, "off_threshold_v": 1.0, "attack_ms": 300, "hang_ms": 2000}
RSSI = {"smoothing": 1, "invert": False}


def feed(det, seq, dt=0.05, t0=0.0):
    events, t = [], t0
    for v in seq:
        e = det.update(v, None, t)
        if e:
            events.append((e, round(t, 2)))
        t += dt
    return events, t


def test_blip_is_ignored():
    d = CarrierDetector(DET, RSSI)
    ev, _ = feed(d, [0.5] * 5 + [2.0] * 4 + [0.5] * 50)      # 200 ms blip < 300 ms attack
    assert ev == []


def test_over_with_hang_and_hysteresis():
    d = CarrierDetector(DET, RSSI)
    seq = [0.5] * 5 + [2.0] * 40 + [1.1] * 10 + [2.0] * 10 + [0.5] * 60
    ev, _ = feed(d, seq)
    # one start (time = when carrier first appeared), one end after the hang
    assert [e for e, _ in ev] == ["start", "end"]
    assert d.over_start == 0.25


def test_dropout_shorter_than_hang_keeps_over():
    d = CarrierDetector(DET, RSSI)
    ev, _ = feed(d, [2.0] * 20 + [0.5] * 20 + [2.0] * 20 + [0.5] * 50)   # 1 s gap < 2 s hang
    assert [e for e, _ in ev] == ["start", "end"]


def test_inverted_rssi():
    d = CarrierDetector({**DET, "on_threshold_v": 1.0, "off_threshold_v": 1.2}, {"smoothing": 1, "invert": True})
    ev, _ = feed(d, [2.0] * 5 + [0.5] * 20 + [2.0] * 60)
    assert [e for e, _ in ev] == ["start", "end"]


def test_cor_source():
    d = CarrierDetector({**DET, "source": "cor"}, RSSI)
    t, out = 0.0, []
    for c in [False] * 3 + [True] * 10 + [False] * 50:
        e = d.update(None, c, t)
        if e:
            out.append(e)
        t += 0.05
    assert out == ["start", "end"]


def test_dbm_calibration():
    cfg = {"cal_v1": 0.5, "cal_dbm1": -120, "cal_v2": 2.5, "cal_dbm2": -60}
    assert volts_to_dbm(1.5, cfg) == -90
    assert volts_to_dbm(1.5, {"cal_v1": None}) is None


def test_g711_roundtrip():
    x = (np.sin(np.arange(8000) / 8000 * 2 * np.pi * 1000) * 16000).astype(np.int16)
    for _, enc, dec in CODECS.values():
        y = dec(enc(x).tobytes()).astype(float)
        snr = 10 * np.log10(np.mean(x.astype(float) ** 2) / np.mean((y - x) ** 2))
        assert snr > 30


# ---------- TX controller ----------
from airbridge.tx import TxController  # noqa: E402

TXCFG = {"enabled": True, "vox_dbfs": -40.0, "vox_hang_ms": 200, "ptt_lead_ms": 40, "ptt_tail_ms": 40,
         "jitter_ms": 40, "tot_s": 60, "rx_guard_ms": 300, "record": False}


class _Hooks:
    def __init__(self):
        self.ends = []
    def burst_start(self, kind): pass
    def burst_frame(self, f): pass
    def burst_end(self, result, detail, res): self.ends.append(result)
    def event(self, k, t): pass


class _Ptt:
    def __init__(self): self.log = []
    def set_ptt(self, on): self.log.append(on)


def _run_tx(busy, n_voice=50, n_silence=60, monkey=None):
    import airbridge.tx as txm
    clock = [1000.0]
    txm.time.time = lambda: clock[0]
    tone = (np.sin(np.arange(160) / 8000 * 2 * np.pi * 800) * 10000).astype(np.int16)
    feed = [tone] * n_voice + [np.zeros(160, np.int16)] * n_silence   # far end keeps sending silence
    src = iter(feed)
    hooks, ptt = _Hooks(), _Ptt()
    tx = TxController(TXCFG, ptt, lambda: [next(src)] if True else [], lambda: busy, hooks)
    played = 0
    for _ in range(len(feed) - 1):
        out = tx.pull()
        played += int(np.any(out))
        clock[0] += 0.02
    return tx, hooks, ptt, played


def test_tx_keys_and_releases_despite_streamed_silence():
    import time as _t
    real = _t.time
    try:
        tx, hooks, ptt, played = _run_tx(busy=False)
    finally:
        import airbridge.tx as txm
        txm.time.time = real
    assert ptt.log == [True, False]
    assert hooks.ends == ["transmitted"]
    assert played >= 45


def test_tx_dropped_when_channel_busy():
    import time as _t
    real = _t.time
    try:
        tx, hooks, ptt, played = _run_tx(busy=True)
    finally:
        import airbridge.tx as txm
        txm.time.time = real
    assert ptt.log == []
    assert hooks.ends == ["dropped"]
    assert played == 0


# ---------- AF carrier detection ----------
from airbridge.detector import AfDetector, sources_of  # noqa: E402

AFCFG = {"mode": "noise", "smoothing": 1, "level_on_dbfs": -35.0, "level_off_dbfs": -42.0,
         "noise_lo_hz": 4000, "noise_hi_hz": 7000, "noise_on_dbfs": -60.0, "noise_off_dbfs": -52.0}


def test_af_noise_mode_quieting_and_hysteresis():
    a = AfDetector(AFCFG)
    seq = [-45, -45, -70, -70, -56, -56, -50, -45]       # idle, carrier, in-between holds, clear
    out = [a.update(-30, v) for v in seq]
    assert out == [False, False, True, True, True, True, False, False]


def test_af_level_mode():
    a = AfDetector({**AFCFG, "mode": "level"})
    out = [a.update(v, None) for v in (-60, -30, -38, -45)]
    assert out == [False, True, True, False]


def test_af_noise_without_hf_reports_error():
    a = AfDetector(AFCFG)
    a.update(-30, None)
    assert a.error


def test_af_source_any_and_all():
    det = {"sources": ["rssi", "af"], "combine": "any", "on_threshold_v": 1.2, "off_threshold_v": 1.0,
           "attack_ms": 0, "hang_ms": 0}
    d = CarrierDetector(det, RSSI)
    assert d.update(0.5, None, 0.0, af=True) == "start"
    d2 = CarrierDetector({**det, "combine": "all"}, RSSI)
    assert d2.update(0.5, None, 0.0, af=True) is None
    assert d2.update(2.0, None, 0.1, af=True) == "start"


def test_legacy_source_mapping():
    assert sources_of({"source": "either"}) == ["rssi", "cor"]
    assert sources_of({"sources": ["af"]}) == ["af"]


# ---------- adaptive RSSI floor ----------
def test_adaptive_floor_follows_drift_and_ignores_carriers():
    det = {"sources": ["rssi"], "combine": "any", "on_threshold_v": 5.0, "off_threshold_v": 4.0,
           "attack_ms": 0, "hang_ms": 200}
    rcfg = {"smoothing": 1, "invert": False, "threshold_mode": "adaptive", "sample_hz": 20,
            "floor_window_s": 30, "floor_percentile": 20, "on_offset_v": 0.15, "off_offset_v": 0.08}
    d = CarrierDetector(det, rcfg)
    t, events = 0.0, []
    # 2 min of floor drifting 0.40 -> 0.70 V with a +0.5 V carrier every 15 s for 1 s.
    # A fixed threshold either misses carriers early or false-triggers late; adaptive must do neither.
    rng = np.random.default_rng(1)
    for i in range(2400):
        floor = 0.40 + 0.30 * i / 2400
        car = (i % 300) >= 280
        v = floor + (0.5 if car else 0.0) + rng.normal(0, 0.01)
        e = d.update(v, None, t)
        if e:
            events.append(e)
        t += 0.05
    assert d.adaptive
    assert events.count("start") == 8 and events.count("end") >= 7     # last carrier still up at the end
    assert abs(d.floor.floor - 0.70) < 0.08                              # percentile lags a steady ramp slightly


def test_adaptive_recovers_from_floor_step():
    """A step up in the floor (e.g. new interference) must not latch the detector on forever."""
    det = {"sources": ["rssi"], "on_threshold_v": 5.0, "off_threshold_v": 4.0, "attack_ms": 0, "hang_ms": 0}
    rcfg = {"smoothing": 1, "threshold_mode": "adaptive", "sample_hz": 20, "floor_window_s": 30,
            "floor_percentile": 20, "on_offset_v": 0.15, "off_offset_v": 0.08, "max_carrier_s": 10}
    d = CarrierDetector(det, rcfg)
    t = 0.0
    for i in range(600):                       # 30 s at 0.5 V, then the floor steps to 1.0 V
        d.update(0.5 if i < 300 else 1.0, None, t)
        t += 0.05
    for i in range(300):                       # 15 more seconds at the new floor
        d.update(1.0, None, t)
        t += 0.05
    assert d.relearned == 1 and d.state == "idle" and abs(d.floor.floor - 1.0) < 0.01


def test_adaptive_falls_back_to_fixed_until_learned():
    det = {"sources": ["rssi"], "on_threshold_v": 1.2, "off_threshold_v": 1.0, "attack_ms": 0, "hang_ms": 0}
    d = CarrierDetector(det, {"smoothing": 1, "threshold_mode": "adaptive", "sample_hz": 20,
                              "on_offset_v": 0.15, "off_offset_v": 0.08})
    assert not d.adaptive and d.thresholds() == (1.2, 1.0)


# ---------- LCD ----------
from airbridge import lcd as lcdmod  # noqa: E402


def _snap(**kw):
    base = {"state": "idle", "rssi_v": 0.5, "on_v": 0.65, "af_value": None, "sip": "idle", "alarms": [],
            "today": {"overs": 3, "sent": 2, "transmitted": 1}, "ip": "192.168.1.60"}
    base.update(kw)
    return base


def test_lcd_lines_are_always_20x4():
    for kw in ({}, {"state": "active", "over_s": 4.2}, {"state": "tx", "tx_s": 12.3}, {"state": "drop"},
               {"sip": "connected", "sip_s": 75, "sip_in": True}, {"sip": "failed", "sip_err": "486 Busy Here, very long"},
               {"alarms": ["carrier no audio", "LCD: something extremely long that must be cut"]}, {"rssi_v": None}):
        lines = lcdmod.render(_snap(**kw), 1793100000.0, 0)
        assert len(lines) == 4 and all(len(x) == 20 for x in lines), (kw, lines)


def test_lcd_content():
    l = lcdmod.render(_snap(state="active", over_s=4.2, sip="connected", sip_s=12), 1793100000.0, 1)
    assert l[0].startswith("CARRIER 4.2s") and l[2].startswith("SIP connected 12s")
    assert l[3].startswith("Ovr  3 NXR  2 TX  1")
    assert lcdmod.render(_snap(), 1793100000.0, 0)[3].startswith("192.168.1.60")


def test_lcd_bar_glyphs():
    assert lcdmod.bar(0, 4) == "\x00" * 4
    assert lcdmod.bar(1, 4) == "\x05" * 4
    assert lcdmod.bar(0.5, 4) == "\x05\x05\x00\x00"
    assert len(lcdmod.bar(0.37, 10)) == 10


class _FakeBus:
    def __init__(self):
        self.writes = []
    def write_byte(self, addr, b):
        self.writes.append(b)
    def close(self):
        pass


def test_hd44780_4bit_protocol():
    d = lcdmod.Hd44780.__new__(lcdmod.Hd44780)
    d.bus, d.addr, d.backlight = _FakeBus(), 0x27, True
    d.write_at(1, 0, "A")
    w = d.bus.writes
    # set DDRAM 0x80|0x40 = 0xC0 as two nibbles, then 'A' (0x41) with RS; each nibble strobes EN high then low
    assert w[0] == 0xC0 | lcdmod.EN | lcdmod.BL and w[1] == 0xC0 | lcdmod.BL
    assert w[2] == 0x00 | lcdmod.EN | lcdmod.BL and w[3] == 0x00 | lcdmod.BL
    assert w[4] == 0x40 | lcdmod.RS | lcdmod.EN | lcdmod.BL and w[6] == 0x10 | lcdmod.RS | lcdmod.EN | lcdmod.BL


# ---------- version ----------
def test_version_is_semver_and_matches_changelog():
    import os
    import re
    from airbridge import __version__
    assert re.fullmatch(r"\d+\.\d+\.\d+", __version__)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    with open(os.path.join(root, "CHANGELOG.md")) as f:
        assert f"## [{__version__}]" in f.read(), "add a CHANGELOG entry for the new version"


def test_version_history_records_only_changes(tmp_path):
    from airbridge.version import VersionHistory
    h = VersionHistory(str(tmp_path))
    a = {"version": "0.6.0", "commit": "aaa", "describe": "v0.6.0"}
    b = {"version": "0.7.0", "commit": "bbb", "describe": "v0.7.0"}
    assert h.record(a, 1) == (None, True)
    assert h.record(a, 2)[1] is False                 # restart of the same build: not logged again
    prev, changed = h.record(b, 3)
    assert changed and prev["version"] == "0.6.0"
    assert [x["version"] for x in h.load()] == ["0.6.0", "0.7.0"]


# ---------- delay measurement ----------
def test_tone_onset_detection_timing():
    from airbridge.latency import find_tone_onset, tone_burst_frames
    lead, burst, tail = tone_burst_frames()
    rng = np.random.default_rng(3)
    t0, log = 100.0, []
    frames = [np.zeros(160, np.int16)] * 30 + burst + tail          # tone starts 600 ms in
    for i, f in enumerate(frames):
        noisy = np.clip(f.astype(np.int32) + rng.normal(0, 100, 160), -32768, 32767).astype(np.int16)
        log.append((t0 + (i + 1) * 0.02, noisy))                     # arrival = end of frame
    onset = find_tone_onset(log, t0)
    assert onset is not None and abs(onset - (t0 + 0.600)) < 0.025


def test_tone_onset_ignores_noise_and_speechlike():
    from airbridge.latency import find_tone_onset
    rng = np.random.default_rng(4)
    t = np.arange(160) / 8000
    log = [(i * 0.02, (rng.normal(0, 3000, 160)).astype(np.int16)) for i in range(50)]
    log += [(1 + i * 0.02, (8000 * (np.sin(2 * np.pi * 440 * t) + np.sin(2 * np.pi * 1300 * t))).astype(np.int16))
            for i in range(50)]
    assert find_tone_onset(log, 0) is None
