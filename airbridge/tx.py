"""TX controller: NXR audio -> K5TRA output + A210 PTT.

Runs inside the audio thread: pull() is called every 20 ms and returns
the frame to play. Keying is VOX on the received NXR audio, with a
jitter buffer, PTT lead/tail, a transmit time-out, and the busy-channel
rule: if the airband channel is busy when NXR audio arrives, the burst
is dropped (never transmitted over a busy channel).
"""
import logging
import math
import time
from collections import deque

import numpy as np

from .afmeter import OverStats, dbfs_rms

log = logging.getLogger("tx")

FRAME = 160
SILENCE = np.zeros(FRAME, dtype=np.int16)

IDLE, LEAD, TX, TAIL, DROP, TOT = "idle", "lead", "tx", "tail", "dropping", "timeout"


class TxController:
    def __init__(self, cfg, k5tra, source, is_busy, hooks):
        """
        cfg      -- the 'tx' config section
        k5tra    -- object with set_ptt(bool)
        source   -- callable returning a list of decoded frames received since last call
        is_busy  -- callable: True while the airband channel is busy
        hooks    -- object with burst_start(kind) / burst_frame(frame) / burst_end(result, detail)
                    / event(kind, text)
        """
        self.cfg = cfg
        self.k5tra = k5tra
        self.source = source
        self.is_busy = is_busy
        self.hooks = hooks
        self.state = IDLE
        self.buf = deque()
        self._pend = np.zeros(0, dtype=np.int16)
        self.local = deque()             # locally generated audio (test tone)
        self.last_voice = 0.0
        self._t = 0.0                    # timer for lead / tail
        self.key_time = None
        self.tx_end = 0.0                # time PTT was last released
        self.last_result = None
        self.stats = None

    # ----- helpers -----
    @property
    def ptt(self):
        return self.state in (LEAD, TX, TAIL)

    def active(self, now=None):
        now = now or time.time()
        return self.state != IDLE or now - self.last_voice < 1.0

    def buffered_ms(self):
        return (len(self.buf) * FRAME + len(self._pend)) / 8

    def _vox(self):
        return float(self.cfg["vox_dbfs"])

    def _ingest(self, now):
        frames = list(self.source() or [])
        # locally generated audio is fed at real-time pace (keeps the buffer near the jitter target)
        target = max(1, int(self.cfg["jitter_ms"]) // 20) + 1
        while self.local and len(self.buf) + len(frames) < target:
            frames.append(self.local.popleft())
        if not frames:
            return
        x = np.concatenate([self._pend] + frames)
        n = len(x) // FRAME
        for i in range(n):
            f = x[i * FRAME:(i + 1) * FRAME]
            voiced = dbfs_rms(f) > self._vox()
            if voiced:
                self.last_voice = now
            self.buf.append((f, voiced))
        self._pend = x[n * FRAME:]

    def _set_ptt(self, on):
        self.k5tra.set_ptt(on)

    def _key(self, now):
        self._set_ptt(True)
        self.state = LEAD
        self._t = now + float(self.cfg["ptt_lead_ms"]) / 1000
        self.key_time = now
        self._lead_start = now - len(self.buf) * FRAME / 8000.0   # first queued audio arrived about here
        self.stats = OverStats()
        self.hooks.burst_start("tx")
        self.hooks.event("tx", "PTT keyed (NXR audio)")

    def _unkey(self, now, result, detail=None):
        self._set_ptt(False)
        self.tx_end = now
        dur = now - (self.key_time or now)
        self.last_result = result
        res = self.stats.result() if self.stats else {}
        self.hooks.burst_end(result, detail, res)
        self.hooks.event("tx", f"PTT released ({dur:.1f} s)" + (f": {detail}" if detail else ""))
        self.stats = None

    # ----- main tick (audio thread) -----
    def pull(self):
        now = time.time()
        if not self.cfg["enabled"]:
            self.source()                    # keep the receive side drained
            self.buf.clear()
            if self.ptt:
                self._unkey(now, "transmitted", "TX disabled")
            self.state = IDLE
            return SILENCE

        self._ingest(now)
        hang = float(self.cfg["vox_hang_ms"]) / 1000
        voice = now - self.last_voice < hang
        jitter_frames = max(1, int(self.cfg["jitter_ms"]) // 20)

        if self.state == IDLE:
            # throw away leading silence so latency stays low
            while self.buf and not self.buf[0][1]:
                self.buf.popleft()
            if self.buf and len(self.buf) >= jitter_frames:
                if self.is_busy():
                    self.state = DROP
                    self.stats = OverStats()
                    self.hooks.burst_start("drop")
                    self.hooks.event("tx", "NXR audio dropped: airband channel busy")
                    self._drain_to_drop()
                else:
                    self._key(now)
            return SILENCE

        if self.state == DROP:
            self._drain_to_drop()
            if not voice:
                self.state = IDLE
                res = self.stats.result() if self.stats else {}
                self.hooks.burst_end("dropped", "airband channel busy", res)
                self.stats = None
            return SILENCE

        if self.state == TOT:
            self.buf.clear()
            if not voice:
                self.state = IDLE
            return SILENCE

        if self.state == LEAD:
            if now >= self._t:
                self.state = TX
                lag_hook = getattr(self.hooks, "burst_lag", None)
                if lag_hook:
                    lag_hook((now - self._lead_start) * 1000)   # oldest queued audio starts playing now
            return SILENCE

        if self.state == TX:
            if now - self.key_time > float(self.cfg["tot_s"]):
                self.hooks.event("error", f"TX time-out after {self.cfg['tot_s']} s")
                self._unkey(now, "transmitted", "time-out")
                self.state = TOT
                self.buf.clear()
                return SILENCE
            if not voice:
                # VOX hang expired: the far end may keep streaming silence, so
                # don't wait for the buffer to empty — drop the unvoiced tail
                self.buf.clear()
                self.state = TAIL
                self._t = now + float(self.cfg["ptt_tail_ms"]) / 1000
                return SILENCE
            if self.buf:
                f, _ = self.buf.popleft()
                self.stats.add(f)
                self.hooks.burst_frame(f)
                return f
            return SILENCE                    # underrun: pad

        if self.state == TAIL:
            if self.buf and voice:            # audio resumed during the tail
                self.state = TX
                return self.pull_again()
            if now >= self._t:
                self._unkey(now, "transmitted")
                self.state = IDLE
            return SILENCE
        return SILENCE

    def pull_again(self):
        f, _ = self.buf.popleft()
        if self.stats:
            self.stats.add(f)
        self.hooks.burst_frame(f)
        return f

    def _drain_to_drop(self):
        while self.buf:
            f, _ = self.buf.popleft()
            if self.stats:
                self.stats.add(f)
            self.hooks.burst_frame(f)

    def force_release(self):
        if self.ptt:
            self._unkey(time.time(), "transmitted", "released")
        self.state = IDLE
        self.buf.clear()
        self.local.clear()

    def guard_active(self, now=None):
        """True while transmitting or within rx_guard_ms after unkey."""
        now = now or time.time()
        return self.ptt or self.state == TOT or now - self.tx_end < float(self.cfg["rx_guard_ms"]) / 1000

    def snapshot(self):
        return {"state": self.state, "ptt": self.ptt,
                "keyed_s": round(time.time() - self.key_time, 1) if self.ptt and self.key_time else None,
                "buffer_ms": round(self.buffered_ms()), "last": self.last_result,
                "enabled": bool(self.cfg["enabled"])}


def tone_burst(seconds, freq, level_dbfs):
    amp = 32767 * 10 ** (level_dbfs / 20)
    n = int(seconds * 8000)
    t = np.arange(n) / 8000
    x = (amp * np.sin(2 * math.pi * freq * t)).astype(np.int16)
    return [x[i:i + FRAME] for i in range(0, n - FRAME + 1, FRAME)]
