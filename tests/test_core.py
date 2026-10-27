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
