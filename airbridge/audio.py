"""Audio capture from the K5TRA (CM119A) and 8 kHz framing.

Captures at the card's native rate, low-pass filters and decimates to
8 kHz mono int16, and hands out 20 ms frames (160 samples). Everything
downstream (pre-roll, recorder, SIP) works on those frames.
"""
import logging
import math
import threading
import time

import numpy as np

from .hardware import SIM

log = logging.getLogger("audio")

RATE = 8000
FRAME = 160                     # 20 ms @ 8 kHz


def _lowpass(num_taps, cutoff, fs):
    n = np.arange(num_taps) - (num_taps - 1) / 2
    h = np.sinc(2 * cutoff / fs * n) * np.hamming(num_taps)
    return (h / h.sum()).astype(np.float32)


def dbfs(frame):
    if frame is None or len(frame) == 0:
        return -120.0
    rms = math.sqrt(float(np.mean(frame.astype(np.float32) ** 2))) / 32768.0
    return 20 * math.log10(max(rms, 1e-6))


class AudioSource:
    def __init__(self, cfg, simulate, on_frame):
        self.cfg = cfg
        self.sim = simulate
        self.on_frame = on_frame
        self.level = -120.0
        self.error = None
        self.device_name = None
        self._stream = None
        self._thread = None
        self._stop = threading.Event()
        self._pending = np.zeros(0, dtype=np.float32)
        self._hist = None
        self._gain = 10 ** (float(cfg["gain_db"]) / 20)

    # ---------- real capture ----------
    def _pick_device(self, sd):
        want = (self.cfg["device"] or "").strip()
        devs = sd.query_devices()
        for i, d in enumerate(devs):
            if d["max_input_channels"] < 1:
                continue
            name = d["name"]
            if want:
                if want == str(i) or want.lower() in name.lower():
                    return i, name
            elif any(k in name.lower() for k in ("c-media", "cm119", "k5tra", "usb audio", "usb pnp")):
                return i, name
        if want:
            raise RuntimeError(f"audio device '{want}' not found")
        i = sd.default.device[0]
        return i, devs[i]["name"] if i is not None and i >= 0 else "default"

    def start(self):
        self._stop.clear()
        if self.sim:
            self.device_name = "simulated"
            self._thread = threading.Thread(target=self._sim_loop, daemon=True)
            self._thread.start()
            return
        try:
            import sounddevice as sd
            idx, name = self._pick_device(sd)
            rate = int(self.cfg["capture_rate"])
            self._ratio = rate // RATE
            if rate % RATE:
                raise RuntimeError("capture_rate must be a multiple of 8000")
            self._taps = _lowpass(63, 3400, rate)
            self._hist = np.zeros(len(self._taps) - 1, dtype=np.float32)
            self._stream = sd.InputStream(device=idx, channels=1, samplerate=rate,
                                          dtype="float32", blocksize=FRAME * self._ratio,
                                          callback=self._cb)
            self._stream.start()
            self.device_name = name
            log.info("capturing from %s @ %d Hz", name, rate)
        except Exception as e:
            self.error = f"audio: {e}"
            log.error(self.error)

    def _cb(self, indata, frames, t, status):
        x = indata[:, 0]
        buf = np.concatenate([self._hist, x])
        y = np.convolve(buf, self._taps, mode="valid")      # len == len(x)
        self._hist = buf[-(len(self._taps) - 1):]
        self._emit(y[:: self._ratio])

    def _emit(self, samples):
        self._pending = np.concatenate([self._pending, samples * self._gain])
        while len(self._pending) >= FRAME:
            f = self._pending[:FRAME]
            self._pending = self._pending[FRAME:]
            pcm = np.clip(f * 32767, -32768, 32767).astype(np.int16)
            self.level = dbfs(pcm)
            try:
                self.on_frame(pcm)
            except Exception:
                log.exception("frame handler")

    # ---------- simulation ----------
    def _sim_loop(self):
        t0 = time.monotonic()
        n = 0
        phase = 0.0
        while not self._stop.is_set():
            t = np.arange(FRAME) / RATE + n * FRAME / RATE
            if SIM.carrier():
                # voice-ish: two tones with a syllabic envelope
                env = 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t)
                sig = 0.25 * env * (np.sin(2 * np.pi * 440 * t) + 0.5 * np.sin(2 * np.pi * 1250 * t + phase))
                sig += np.random.normal(0, 0.01, FRAME)
            else:
                sig = np.random.normal(0, 0.004, FRAME)
            self._emit(sig.astype(np.float32))
            n += 1
            # pace in real time
            delay = t0 + n * 0.02 - time.monotonic()
            if delay > 0:
                time.sleep(delay)

    def stop(self):
        self._stop.set()
        if self._stream:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
        if self._thread:
            self._thread.join(timeout=1)


def tone_frames(seconds, freq=1000.0, level_dbfs=-10.0):
    """Generator of 20 ms int16 frames of a test tone."""
    amp = 32767 * 10 ** (level_dbfs / 20)
    total = int(seconds * RATE / FRAME)
    for n in range(total):
        t = (np.arange(FRAME) + n * FRAME) / RATE
        yield (amp * np.sin(2 * np.pi * freq * t)).astype(np.int16)
