"""The bridge engine: ties detection, relay, recording and SIP together.

Threads:
  * audio thread  -> on_frame(): pre-roll ring, recorder, forward queue
  * poll thread   -> RSSI/COR sampling, detector, relay
  * asyncio loop  -> SIP calls (shared with the web server)
"""
import asyncio
import logging
import threading
import time
from collections import deque

from . import hardware
from .audio import FRAME, RATE, AudioSource, tone_frames
from .detector import CarrierDetector, volts_to_dbm
from .recorder import OverRecording, Store
from .sip import SipUA

log = logging.getLogger("engine")


class Engine:
    def __init__(self, config, data_dir):
        self.config = config
        self.data_dir = data_dir
        self.store = Store(data_dir)
        self.loop = None
        self.events = deque(maxlen=300)
        self._lock = threading.RLock()
        self._running = False
        self.listeners = set()           # asyncio queues for live event push

    # ---------- lifecycle ----------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        c = self.config
        sim = bool(c["general"]["simulate"])
        self.sim = sim
        self.rssi_in = hardware.RssiInput(c["rssi"], sim)
        self.cor_in = hardware.CorInput(c["cor"], sim)
        self.relay = hardware.Relay(c["relay"], sim)
        self.det = CarrierDetector(c["detect"], c["rssi"])
        self.preroll = deque(maxlen=max(1, int(c["audio"]["preroll_ms"]) // 20))
        self.over = None                 # dict for the current over
        self.rec = None
        self.pending = deque()           # frames waiting for a call to connect
        self.forwarding = False          # frames go straight to call.frames
        self.call_task = None
        self.call_oids = []
        self.last_call_end = 0.0
        self.relay_until = 0.0
        self.peak_v = None
        self.v_sum, self.v_n = 0.0, 0

        self.ua = None
        if c["sip"]["enabled"]:
            self.ua = SipUA(c["sip"])
            try:
                await self.ua.start()
            except OSError as e:
                self.event("error", f"SIP port {c['sip']['local_port']}: {e}")
                self.ua = None

        self.audio = AudioSource(c["audio"], sim, self.on_frame)
        self.audio.start()
        self._running = True
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()
        self._housekeeping = asyncio.create_task(self._housekeep())
        self.event("info", f"started ({'SIMULATION' if sim else 'hardware'} mode)")
        for err in (self.rssi_in.error, self.cor_in.error, self.relay.error, self.audio.error):
            if err:
                self.event("error", err)

    async def stop(self):
        self._running = False
        self._housekeeping.cancel()
        if self.poll_thread.is_alive():
            self.poll_thread.join(timeout=2)
        self.audio.stop()
        if self.over:
            self._end_over()
        if self.ua:
            await self.ua.stop()
        self.relay.close()
        self.cor_in.close()

    async def restart(self):
        await self.stop()
        await self.start()

    # ---------- events ----------
    def event(self, kind, text):
        e = {"t": time.time(), "kind": kind, "text": text}
        self.events.append(e)
        (log.warning if kind == "error" else log.info)("%s", text)
        if self.loop:
            for q in list(self.listeners):
                self.loop.call_soon_threadsafe(q.put_nowait, {"type": "event", **e})

    @property
    def call(self):
        return self.ua.call if self.ua else None

    # ---------- audio path ----------
    def on_frame(self, frame):
        with self._lock:
            self.preroll.append(frame)
            if self.over is None:
                return
            if self.rec:
                self.rec.write(frame)
            self._forward(frame)

    def _forward(self, frame):
        if self.over is None or not self.over.get("forward"):
            return
        if self.forwarding and self.call and self.call.state == "connected":
            self.call.frames.append(frame)
        else:
            self.pending.append(frame)

    # ---------- detection / relay ----------
    def _poll_loop(self):
        hz = max(1, int(self.config["rssi"]["sample_hz"]))
        period = 1.0 / hz
        nxt = time.monotonic()
        while self._running:
            v = self.rssi_in.read()
            cor = self.cor_in.active()
            now = time.time()
            with self._lock:
                ev = self.det.update(v, cor, now)
                if ev == "start":
                    self._start_over(self.det.over_start)
                elif ev == "end":
                    self._end_over()
                if self.over is not None and self.det.rssi is not None:
                    self.peak_v = max(self.peak_v or 0, self.det.rssi)
                    self.v_sum += self.det.rssi
                    self.v_n += 1
            self._update_relay(now)
            nxt += period
            time.sleep(max(0, nxt - time.monotonic()))

    def _update_relay(self, now):
        rc = self.config["relay"]
        carrier = self.det.carrier
        in_call = bool(self.call and self.call.state in ("calling", "trying", "ringing", "connected"))
        cond = {"carrier": carrier, "call": in_call}.get(rc["mode"], carrier or in_call)
        if cond:
            self.relay_until = now + float(rc["hold_ms"]) / 1000
        if self._relay_override is not None:
            self.relay.set(self._relay_override)
        else:
            self.relay.set(cond or now < self.relay_until)

    _relay_override = None

    # ---------- overs ----------
    def _start_over(self, started):
        c = self.config
        oid = self.store.new_over(started)
        self.over = {"id": oid, "started": started, "forward": False}
        self.peak_v, self.v_sum, self.v_n = None, 0.0, 0
        if c["record"]["enabled"]:
            self.rec = OverRecording(self.store, oid, started, c["record"]["format"])
        pre_all = list(self.preroll)
        for f in pre_all:
            if self.rec:
                self.rec.write(f)
        # the call only gets the tail of the pre-roll: enough to cover the
        # attack time without adding seconds of dead air (and delay) to every over
        keep = max(1, int(c["sip"]["forward_preroll_ms"]) // 20 + int(c["detect"]["attack_ms"]) // 20)
        pre = pre_all[-keep:]
        self.event("carrier", f"carrier detected (over #{oid})")

        # decide forwarding
        reason = None
        if not self.ua:
            reason = "SIP disabled" if not c["sip"]["enabled"] else "SIP not running"
        elif self.call and self.call.state == "connected" and self.forwarding:
            # previous over still draining on an open call: keep using it
            self.over["forward"] = True
            for f in pre:
                self.call.frames.append(f)
            self.store.update(oid, forward="sent", forward_detail="joined open call")
            return
        elif self.call and self.call.state in ("calling", "trying", "ringing"):
            self.over["forward"] = True
            self.pending.extend(pre)
            self.call_oids.append(oid)
            self.store.update(oid, forward="pending", forward_detail="queued behind connecting call")
            return
        elif time.time() - self.last_call_end < float(c["sip"]["cooldown_s"]):
            reason = "cooldown"
        if reason:
            self.store.update(oid, forward="skipped" if reason == "cooldown" else "disabled",
                              forward_detail=reason)
            return
        self.over["forward"] = True
        self.pending.clear()
        self.pending.extend(pre)
        self.call_oids = [oid]
        self.loop.call_soon_threadsafe(self._spawn_call, oid)

    def _end_over(self):
        o = self.over
        if o is None:
            return
        self.over = None
        ended = time.time()
        dur = ended - o["started"]
        kw = {"ended": ended, "duration": round(dur, 2),
              "peak_v": round(self.peak_v, 3) if self.peak_v is not None else None,
              "avg_v": round(self.v_sum / self.v_n, 3) if self.v_n else None}
        self.store.update(o["id"], **kw)
        rec, self.rec = self.rec, None
        if rec:
            threading.Thread(target=rec.close, daemon=True).start()
        self.event("carrier", f"over #{o['id']} ended ({dur:.1f} s)")

    # ---------- SIP ----------
    def _spawn_call(self, oid):
        if self.call_task and not self.call_task.done():
            return
        self.call_task = asyncio.create_task(self._run_call(oid))

    def _call_state(self, st, err):
        self.event("sip" if st != "failed" else "error",
                   f"call {st}" + (f": {err}" if err else ""))

    async def _run_call(self, first_oid, test_frames=None):
        sc = self.config["sip"]
        if not first_oid:
            self.call_oids = []
        oids = self.call_oids
        try:
            call = await self.ua.place_call(deque(), self._call_state)
            if not call:
                why = self.ua.call.error if self.ua.call else "failed"
                for oid in oids:
                    self.store.update(oid, forward="failed", forward_detail=why)
                with self._lock:
                    self.pending.clear()
                    if self.over:
                        self.over["forward"] = False
                return
            await asyncio.sleep(float(sc["post_answer_delay_ms"]) / 1000)
            digits = (sc["dtmf_digits"] or "").strip()
            if digits:
                await asyncio.sleep(float(sc["dtmf_delay_ms"]) / 1000)
                if sc["dtmf_mode"] == "info" or call.dtmf_pt is None:
                    if call.dtmf_pt is None and sc["dtmf_mode"] == "rfc2833":
                        self.event("info", "far end has no telephone-event; using SIP INFO")
                    await call.send_info_dtmf(digits)
                else:
                    call.queue_dtmf(digits)
                    while call._dtmf_queue and call.state == "connected":
                        await asyncio.sleep(0.05)
                    await asyncio.sleep(0.3)
                self.event("sip", f"sent DTMF {digits}")

            with self._lock:
                if test_frames is not None:
                    call.frames.extend(test_frames)
                else:
                    call.frames.extend(self.pending)
                    self.pending.clear()
                self.forwarding = True
            for oid in oids:
                self.store.update(oid, forward="sent", forward_detail=None)

            # stay up while carrier/hang or buffered audio remains
            max_s = float(sc["max_call_s"])
            while call.state == "connected":
                with self._lock:
                    busy = (self.over is not None and self.over.get("forward")) if test_frames is None else False
                if not busy and not call.frames:
                    await asyncio.sleep(0.4)        # let the last packets out
                    break
                if time.time() - call.answered_at > max_s:
                    self.event("error", f"max call time {max_s:.0f}s reached")
                    with self._lock:
                        if self.over:
                            self.over["forward"] = False
                    break
                await asyncio.sleep(0.05)
            await call.hangup()
        except Exception as e:
            log.exception("call")
            self.event("error", f"call error: {e}")
        finally:
            with self._lock:
                self.forwarding = False
                self.pending.clear()
            self.last_call_end = time.time()

    async def test_call(self, seconds=5.0):
        if not self.ua:
            raise RuntimeError("SIP is disabled or not running")
        if self.call_task and not self.call_task.done():
            raise RuntimeError("a call is already in progress")
        frames = list(tone_frames(seconds, 1000.0, -10.0))
        self.event("info", f"test call: {seconds:.0f} s of 1 kHz tone")
        self.call_task = asyncio.create_task(self._run_call(None, test_frames=frames))

    async def hangup(self):
        if self.ua and self.ua.call:
            with self._lock:
                if self.over:
                    self.over["forward"] = False
            await self.ua.call.hangup()

    def set_relay_override(self, state):
        self._relay_override = state
        self.event("info", f"relay override: {'auto' if state is None else ('ON' if state else 'OFF')}")

    # ---------- status ----------
    def status(self):
        c = self.config
        d = self.det
        call = self.ua.call if self.ua else None
        now = time.time()
        over = self.over
        return {
            "type": "status",
            "t": now,
            "site": c["general"]["site_name"],
            "simulate": self.sim,
            "rssi_v": None if d.rssi is None else round(d.rssi, 3),
            "rssi_dbm": (lambda x: None if x is None else round(x, 1))(volts_to_dbm(d.rssi, c["rssi"])),
            "on_v": c["detect"]["on_threshold_v"],
            "off_v": c["detect"]["off_threshold_v"],
            "cor": d.cor,
            "detector": d.state,
            "carrier": d.carrier,
            "over_s": round(now - over["started"], 1) if over else None,
            "over_id": over["id"] if over else None,
            "audio_db": round(self.audio.level, 1),
            "audio_dev": self.audio.device_name,
            "relay": self.relay.state,
            "relay_override": self._relay_override,
            "sip": {
                "enabled": bool(self.ua),
                "target": f"{c['sip']['target_host']}:{c['sip']['target_port']}",
                "registered": self.ua.registered if self.ua and c["sip"]["register"] else None,
                "reg_error": self.ua.reg_error if self.ua else None,
                "state": call.state if call else "idle",
                "codec": call.codec_name if call else c["sip"]["codec"],
                "connected_s": round(now - call.answered_at, 1) if call and call.answered_at and call.state == "connected" else None,
                "buffer_ms": round(call.buffered_ms() if call and call.state == "connected" else len(self.pending) * 20),
                "last_error": self.ua.last_error if self.ua else None,
            },
            "errors": [e for e in (self.rssi_in.error, self.cor_in.error, self.relay.error, self.audio.error) if e],
            "today": self.store.stats_today(),
            "sim_auto": hardware.SIM.auto,
            "sim_forced": hardware.SIM.forced,
        }

    async def _housekeep(self):
        while True:
            try:
                n = self.store.purge(int(self.config["record"]["keep_days"]))
                if n:
                    self.event("info", f"purged {n} old recordings")
            except Exception as e:
                log.warning("purge: %s", e)
            await asyncio.sleep(3600)
