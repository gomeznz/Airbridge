"""Delay measurement helpers.

The echo round trip uses a 1 kHz tone burst, not a chirp: digital voice
codecs (NXDN's AMBE+2) pass a steady tone well but smear sweeps, so the
onset of a tone survives the trip far more reliably. Onset detection is
per 20 ms frame (Goertzel at the tone frequency), so resolution is +/- 20 ms.
"""
import math

import numpy as np

RATE = 8000
FRAME = 160


def tone_burst_frames(ms=400, freq=1000.0, level_dbfs=-10.0, lead_ms=300, tail_ms=300):
    amp = 32767 * 10 ** (level_dbfs / 20)
    n = int(ms * RATE / 1000)
    t = np.arange(n) / RATE
    ramp = np.minimum(1, np.minimum(np.arange(n), n - np.arange(n)) / (0.005 * RATE))   # 5 ms ramps
    burst = (amp * ramp * np.sin(2 * math.pi * freq * t)).astype(np.int16)
    lead = np.zeros(int(lead_ms * RATE / 1000), np.int16)
    tail = np.zeros(int(tail_ms * RATE / 1000), np.int16)

    def frames(x):
        x = np.concatenate([x, np.zeros((-len(x)) % FRAME, np.int16)])
        return [x[i:i + FRAME] for i in range(0, len(x), FRAME)]
    return frames(lead), frames(burst), frames(tail)


def _goertzel_db(f, freq):
    x = f.astype(np.float64) / 32768.0
    n = len(x)
    k = int(0.5 + n * freq / RATE)
    w = 2 * math.pi * k / n
    c = 2 * math.cos(w)
    s1 = s2 = 0.0
    for v in x:
        s0 = v + c * s1 - s2
        s2, s1 = s1, s0
    p = s1 * s1 + s2 * s2 - c * s1 * s2
    tone_rms = math.sqrt(max(p, 0) * 2) / n
    total_rms = math.sqrt(float(np.mean(x * x))) or 1e-9
    return 20 * math.log10(max(tone_rms, 1e-9)), tone_rms / total_rms


def find_tone_onset(rx_log, after_t, freq=1000.0, min_dbfs=-35.0, purity=0.6, frames_needed=3):
    """rx_log: list of (arrival_time, int16 frame). Returns the arrival time of the
    first sample of the tone (corrected for its position in the frame), or None."""
    run = 0
    first = None
    for t, f in rx_log:
        if t < after_t:
            continue
        lvl, pur = _goertzel_db(f, freq)
        if lvl > min_dbfs and pur > purity:
            if run == 0:
                first = (t, f)
            run += 1
            if run >= frames_needed:
                t0, f0 = first
                # within the first tone frame, find where the tone actually starts
                env = np.abs(f0.astype(np.int32))
                above = np.flatnonzero(env > 32767 * 10 ** ((min_dbfs - 6) / 20))
                lead = (above[0] if len(above) else 0)
                return t0 - (len(f0) - lead) / RATE
        else:
            run = 0
    return None
