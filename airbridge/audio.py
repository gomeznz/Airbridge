"""Full-duplex audio on the K5TRA (CM119A).

RX: capture at the card rate, low-pass and decimate to 8 kHz, emit 20 ms
    frames to on_frame().
TX: every 20 ms pull an 8 kHz frame from pull_tx(), interpolate up to the
    card rate and play it on the K5TRA output (into the A210 mic/line).
"""
import logging
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


class AudioSource:
    def __init__(self, cfg, simulate, on_frame, pull_tx=None, hf_band=(4000, 7000)):
        self.cfg = cfg
        self.hf_band = hf_band
        self.hf_db = None            # noise level above the voice band (raw capture), dBFS
        self.sim = simulate
        self.on_frame = on_frame
        self.pull_tx = pull_tx or (lambda: None)
        self.error = None
        self.device_name = None
        self._stream = None
        self._thread = None
        self._stop = threading.Event()
        self._pending = np.zeros(0, dtype=np.float32)
        self._in_gain = 10 ** (float(cfg["gain_db"]) / 20)
        self._out_gain = 10 ** (float(cfg.get("out_gain_db", 0.0)) / 20)
        self.xruns = 0

    # ---------- device ----------
    def _pick_device(self, sd):
        want = (self.cfg["device"] or "").strip()
        devs = sd.query_devices()
        for i, d in enumerate(devs):
            if d["max_input_channels"] < 1:
                continue
            name = d["name"]
            if want:
                if want == str(i) or want.lower() in name.lower():
                    return i, d
            elif any(k in name.lower() for k in ("c-media", "cm119", "k5tra", "usb audio", "usb pnp")):
                return i, d
        if want:
            raise RuntimeError(f"audio device '{want}' not found")
        i = sd.default.device[0]
        return i, devs[i]

    def start(self):
        self._stop.clear()
        if self.sim:
            self.device_name = "simulated"
            self._thread = threading.Thread(target=self._sim_loop, daemon=True)
            self._thread.start()
            return
        try:
            import sounddevice as sd
            idx, dev = self._pick_device(sd)
            rate = int(self.cfg["capture_rate"])
            if rate % RATE:
                raise RuntimeError("capture_rate must be a multiple of 8000")
            self._ratio = rate // RATE
            self._taps = _lowpass(63, 3400, rate)
            self._hist_in = np.zeros(len(self._taps) - 1, dtype=np.float32)
            self._hist_out = np.zeros(len(self._taps) - 1, dtype=np.float32)
            out_ch = 0
            if self.cfg.get("tx_enabled", True) and dev["max_output_channels"] > 0:
                out_ch = min(2, int(dev["max_output_channels"]))
            self._out_ch = out_ch
            kw = dict(device=idx, samplerate=rate, dtype="float32", blocksize=FRAME * self._ratio,
                      latency="low")
            if out_ch:
                self._stream = sd.Stream(channels=(1, out_ch), callback=self._duplex_cb, **kw)
            else:
                self._stream = sd.InputStream(channels=1, callback=self._in_cb, **kw)
            self._stream.start()
            self.device_name = dev["name"] + ("" if out_ch else " (RX only)")
            log.info("audio on %s @ %d Hz, out channels %d", dev["name"], rate, out_ch)
        except Exception as e:
            self.error = f"audio: {e}"
            log.error(self.error)

    # ---------- callbacks ----------
    def _hf_noise(self, x):
        """RMS level (dBFS) of the raw capture between hf_band[0] and hf_band[1]."""
        n = len(x)
        rate = self._ratio * RATE
        lo, hi = self.hf_band
        if hi >= rate / 2 or n < 64:
            return None
        w = self._win if getattr(self, "_win", None) is not None and len(self._win) == n else np.hanning(n)
        self._win = w
        X = np.fft.rfft(x * w)
        k0, k1 = int(lo * n / rate), int(hi * n / rate)
        ms = 2.0 * float(np.sum(np.abs(X[k0:k1]) ** 2)) / (n * float(np.sum(w * w)))
        return 10 * np.log10(max(ms, 1e-12))

    def _rx(self, x):
        self.hf_db = self._hf_noise(x)
        buf = np.concatenate([self._hist_in, x])
        y = np.convolve(buf, self._taps, mode="valid")
        self._hist_in = buf[-(len(self._taps) - 1):]
        self._emit(y[:: self._ratio])

    def _tx(self, nframes):
        """Return nframes of float output at the card rate."""
        out = np.zeros(nframes, dtype=np.float32)
        need = nframes // self._ratio
        got = 0
        chunks = []
        while got < need:
            f = self.pull_tx()
            if f is None:
                f = np.zeros(FRAME, dtype=np.int16)
            chunks.append(f.astype(np.float32) / 32768.0)
            got += len(f)
        lo = np.concatenate(chunks)[:need] * self._out_gain
        up = np.zeros(need * self._ratio, dtype=np.float32)
        up[:: self._ratio] = lo * self._ratio
        buf = np.concatenate([self._hist_out, up])
        y = np.convolve(buf, self._taps, mode="valid")
        self._hist_out = buf[-(len(self._taps) - 1):]
        out[: len(y)] = np.clip(y, -1, 1)
        return out

    def _in_cb(self, indata, frames, t, status):
        if status:
            self.xruns += 1
        self._rx(indata[:, 0])

    def _duplex_cb(self, indata, outdata, frames, t, status):
        if status:
            self.xruns += 1
        self._rx(indata[:, 0])
        y = self._tx(frames)
        outdata[:] = y[:, None]

    def _emit(self, samples):
        self._pending = np.concatenate([self._pending, samples * self._in_gain])
        while len(self._pending) >= FRAME:
            f = self._pending[:FRAME]
            self._pending = self._pending[FRAME:]
            pcm = np.clip(f * 32767, -32768, 32767).astype(np.int16)
            try:
                self.on_frame(pcm)
            except Exception:
                log.exception("frame handler")

    # ---------- simulation ----------
    def _sim_loop(self):
        t0 = time.monotonic()
        n = 0
        while not self._stop.is_set():
            t = (np.arange(FRAME) + n * FRAME) / RATE
            # HF receiver noise: high with squelch open and no signal, quieted by a carrier
            if SIM.ptt:
                self.hf_db = -90.0
            elif SIM.carrier() or SIM.tone:
                self.hf_db = -70.0 + float(np.random.normal(0, 1.5))
            else:
                self.hf_db = -45.0 + float(np.random.normal(0, 1.5))
            if SIM.ptt:
                sig = np.zeros(FRAME)                       # receiver muted while transmitting
            elif SIM.tone:
                sig = 0.3 * np.sin(2 * np.pi * 1000 * t) + 0.009 * np.sin(2 * np.pi * 2000 * t)
                sig += np.random.normal(0, 0.3 / np.sqrt(2) / 10 ** (SIM.tone_snr / 20), FRAME)
            elif SIM.carrier():
                env = 0.5 + 0.5 * np.sin(2 * np.pi * 3.0 * t)
                sig = 0.25 * env * (np.sin(2 * np.pi * 440 * t) + 0.5 * np.sin(2 * np.pi * 1250 * t))
                sig += np.random.normal(0, 0.01, FRAME)
            else:
                sig = np.random.normal(0, 0.004, FRAME)
            self._emit(sig.astype(np.float32))
            try:
                self.pull_tx()                              # consume TX at real-time pace
            except Exception:
                log.exception("pull_tx")
            n += 1
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
