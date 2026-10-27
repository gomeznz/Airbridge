"""The bridge engine: ties detection, relay, recording, SIP and TX together.

Threads:
  * audio thread  -> on_frame() (RX) and tx.pull() (TX), both every 20 ms
  * poll thread   -> RSSI/COR sampling, detector, relay, live alarms
  * asyncio loop  -> SIP calls (shared with the web server)

Directions:
  RX  airband (IC-A210) -> K5TRA in  -> SIP -> NXR-1800
  TX  NXR-1800 -> SIP -> K5TRA out + PTT -> airband (IC-A210)
"""
import asyncio
import json
import logging
import os
import threading
import time
from collections import deque

from . import afmeter, hardware, version
from .audio import AudioSource, tone_frames
from .detector import ACTIVE, ATTACK, AfDetector, CarrierDetector, sources_of, volts_to_dbm
from .history import History
from . import latency
from .k5tra import K5tra
from .lcd import Lcd
from .recorder import OverRecording, Store
from .sip import Mark as sip_mark, SipUA
from .tx import TxController, tone_burst

log = logging.getLogger("engine")

LIVE_CALL = ("calling", "trying", "ringing", "connected")


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
        self._relay_override = None
        # lives across engine restarts (settings saves) and is saved to disk
        self.history = History(os.path.join(data_dir, "history.npz"))
        self.build = version.build_info()
        self.versions = version.VersionHistory(data_dir)
        self.prev_build, self.upgraded = self.versions.record(self.build)

    # ---------- lifecycle ----------
    async def start(self):
        self.loop = asyncio.get_running_loop()
        c = self.config
        sim = bool(c["general"]["simulate"])
        self.sim = sim
        self.k5 = K5tra(c["k5tra"], sim)
        self.rssi_in = hardware.RssiInput(c["rssi"], sim)
        self.cor_in = hardware.CorInput(c["cor"], sim, self.k5)
        self.relay = hardware.Relay(c["relay"], sim)
        old = getattr(self, "det", None)
        self.det = CarrierDetector(c["detect"], c["rssi"])
        if old is not None and old.floor.buf:           # keep the learned floor across a settings save
            self.det.floor.buf.extend(old.floor.buf)
            self.det.floor.floor = old.floor.floor
        self.af_det = AfDetector(c["afdetect"])
        self.rx_meter = afmeter.LevelMeter(c["af"]["rx_cal_dbm"])
        self.tx_meter = afmeter.LevelMeter(c["af"]["tx_cal_dbm"])
        self.preroll = deque(maxlen=max(1, int(c["audio"]["preroll_ms"]) // 20))
        self.over = None                 # dict for the current RX over
        self.rec = None
        self.rx_stats = None
        self.pending = deque()           # frames waiting for a call to connect
        self.forwarding = False          # RX frames go straight to call.frames
        self.call_task = None
        self.call_oids = []
        self.last_call_end = 0.0
        self.last_activity = 0.0
        self.relay_until = 0.0
        self.peak_v = None
        self.v_sum, self.v_n = 0.0, 0
        self.alarms = {}                 # name -> since
        self._no_audio_since = None
        self.burst = None                # current TX burst record
        self.last_lag = {"rx": None, "tx": None, "setup_ms": None, "dtmf_ms": None}
        self.delay_test = getattr(self, "delay_test", None) or {"state": "idle"}
        self._dt_listen = False
        self.tx_rec = None

        self.ua = None
        if c["sip"]["enabled"]:
            self.ua = SipUA(c["sip"])
            self.ua.on_incoming = self._on_incoming
            try:
                await self.ua.start()
            except OSError as e:
                self.event("error", f"SIP port {c['sip']['local_port']}: {e}")
                self.ua = None

        self.tx = TxController(c["tx"], _PttTap(self.k5, sim), self._tx_source, self._channel_busy, self)
        self.audio = AudioSource(c["audio"], sim, self.on_frame, self._pull_tx,
                                 hf_band=(float(c["afdetect"]["noise_lo_hz"]), float(c["afdetect"]["noise_hi_hz"])))
        self.audio.start()
        self._running = True
        self.poll_thread = threading.Thread(target=self._poll_loop, daemon=True)
        self.poll_thread.start()
        self._housekeeping = asyncio.create_task(self._housekeep())
        self._today_cache = (0.0, {})
        splash = None if getattr(self, "_splashed", False) else [      # boot splash only, not on settings saves
            "AirBridge".center(20), f"v{self.build['version']}".center(20),
            (self.build.get("commit") or "").center(20), c["general"]["site_name"][:20].center(20)]
        self._splashed = True
        self.lcd = Lcd(c["lcd"], sim, self.lcd_snapshot, splash=splash)
        self.lcd.start()
        self.event("info", f"AirBridge {version.label(self.build)} started ({'SIMULATION' if sim else 'hardware'} mode)")
        for err in self._hw_errors():
            self.event("error", err)

    def _hw_errors(self):
        errs = [self.rssi_in.error, self.cor_in.error, self.relay.error, self.audio.error]
        if "af" in sources_of(self.config["detect"]):
            errs.append(self.af_det.error)
        if self.config["tx"]["enabled"]:
            errs.append(self.k5.error)
        lcd = getattr(self, "lcd", None)
        if lcd and self.config["lcd"]["enabled"]:
            errs.append(lcd.error)
        return [e for e in errs if e]

    async def stop(self):
        self._running = False
        self._housekeeping.cancel()
        if self.poll_thread.is_alive():
            self.poll_thread.join(timeout=2)
        self.lcd.stop()
        self.audio.stop()
        self.tx.force_release()
        with self._lock:
            if self.over:
                self._end_over()
        if self.ua:
            await self.ua.stop()
        self.history.save()
        self.k5.close()
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

    def _call_live(self):
        c = self.call
        return c if c and c.state == "connected" else None

    # ---------- RX audio path ----------
    def on_frame(self, frame):
        now = time.time()
        self.rx_meter.feed(frame, now)
        if self.tx.guard_active(now):
            self.af_det.reset()
            return
        self.af_det.update(self.rx_meter.rms, self.audio.hf_db)                        # own transmission: never record or forward it
        with self._lock:
            self.preroll.append(frame)
            if self.over is None:
                return
            if self.rec:
                self.rec.write(frame)
            if self.rx_stats:
                self.rx_stats.add(frame)
            self._forward(frame)

    def _forward(self, frame):
        if self.over is None or not self.over.get("forward"):
            return
        call = self._call_live()
        if self.forwarding and call:
            call.frames.append(frame)
        else:
            self.pending.append(frame)

    # ---------- TX audio path ----------
    def _tx_source(self):
        call = self._call_live()
        if not call:
            return None
        if self._dt_listen:                  # delay test: never transmit the echo on airband
            call.rx_frames.clear()
            return None
        out = []
        while call.rx_frames:
            out.append(call.rx_frames.popleft())
        return out

    def _pull_tx(self):
        f = self.tx.pull()
        self.tx_meter.feed(f)
        self.tx_meter.active = self.tx.ptt
        return f

    def _channel_busy(self):
        # busy = a carrier is present now (attack or active); the hang period is clear air
        return self.det.state in (ATTACK, ACTIVE)

    # hooks called by TxController (audio thread)
    def burst_start(self, kind):
        now = time.time()
        c = self.config
        with self._lock:
            if kind == "tx" and self.over is not None:
                self.det.state = "idle"           # over was in hang: close it before transmitting
                self._end_over()
        oid = self.store.new_over(now, "tx", "transmitted" if kind == "tx" else "dropped")
        self.burst = {"id": oid, "started": now, "kind": kind, "local": bool(self.tx.local)}
        self.last_activity = now
        if c["tx"]["record"] and c["record"]["enabled"]:
            self.tx_rec = OverRecording(self.store, oid, now, c["record"]["format"])

    def burst_lag(self, buffered_ms):
        """Called when audio starts playing: receive buffer + PTT lead + sound-card output."""
        lag = round(buffered_ms + self.audio.out_latency_ms)
        self.last_lag["tx"] = lag
        if self.burst:
            self.burst["lag"] = lag

    def burst_frame(self, frame):
        if self.tx_rec:
            self.tx_rec.write(frame)

    def burst_end(self, result, detail, res):
        b, self.burst = self.burst, None
        if not b:
            return
        now = time.time()
        self.last_activity = now
        label = "TX test tone" if b.get("local") else "NXR audio"
        alarms = afmeter.check_over_alarms(res, self.config["af"], label) if result == "transmitted" else []
        for a in alarms:
            self.event("alarm", a)
        self.store.update(b["id"], ended=now, duration=round(now - b["started"], 2), forward=result,
                          forward_detail=detail or ("TX test tone" if b.get("local") else None), avg_dbfs=res.get("avg_dbfs"), peak_dbfs=res.get("peak_dbfs"),
                          clips=res.get("clips"), alarms=json.dumps(alarms) if alarms else None,
                          lag_ms=b.get("lag"))
        rec, self.tx_rec = self.tx_rec, None
        if rec:
            threading.Thread(target=rec.close, daemon=True).start()

    # ---------- detection / relay / alarms ----------
    def _poll_loop(self):
        hz = max(1, int(self.config["rssi"]["sample_hz"]))
        period = 1.0 / hz
        nxt = time.monotonic()
        while self._running:
            v = self.rssi_in.read()
            cor = self.cor_in.active()
            now = time.time()
            with self._lock:
                if self.tx.guard_active(now):
                    pass        # A210 transmitting (or just stopped): RSSI/COR are meaningless
                else:
                    n_relearn = self.det.relearned
                    ev = self.det.update(v, cor, now, self.af_det.latched)
                    if self.det.relearned != n_relearn:
                        self.event("alarm", f"RSSI stayed high > {self.config['rssi']['max_carrier_s']} s: "
                                            "floor re-learned (interference or drift?)")
                    if ev == "start":
                        self._start_over(self.det.over_start)
                    elif ev == "end":
                        self._end_over()
                on_v, off_v = self.det.thresholds()
                self.history.add(now, None if self.tx.guard_active(now) else v, self.det.carrier,
                                 self.tx.guard_active(now), self.det.floor.floor, on_v, off_v, self.af_det.value)
                if self.over is not None and self.det.rssi is not None:
                    self.peak_v = max(self.peak_v or 0, self.det.rssi)
                    self.v_sum += self.det.rssi
                    self.v_n += 1
            self._live_alarms(now)
            self._update_relay(now)
            nxt += period
            time.sleep(max(0, nxt - time.monotonic()))

    def _alarm(self, name, on, text, now):
        if on and name not in self.alarms:
            self.alarms[name] = now
            self.event("alarm", text)
        elif not on and name in self.alarms:
            del self.alarms[name]

    def _live_alarms(self, now):
        af = self.config["af"]
        rx = self.rx_meter.snapshot()
        tx = self.tx_meter.snapshot()
        carrier = self.det.state == ACTIVE and not self.tx.guard_active(now)
        if carrier and rx["dbfs"] < float(af["no_audio_dbfs"]):
            self._no_audio_since = self._no_audio_since or now
        else:
            self._no_audio_since = None
        self._alarm("no_audio", bool(self._no_audio_since and now - self._no_audio_since >= float(af["no_audio_s"])),
                    "carrier present but no audio", now)
        self._alarm("rx_clip", rx["clips_1s"] >= 3, "RX audio clipping", now)
        self._alarm("tx_clip", tx["clips_1s"] >= 3, "TX audio clipping", now)

    def _update_relay(self, now):
        rc = self.config["relay"]
        carrier = self.det.carrier
        in_call = bool(self.call and self.call.state in LIVE_CALL)
        ptt = self.tx.ptt
        cond = {"carrier": carrier, "call": in_call, "ptt": ptt, "either": carrier or in_call,
                "any": carrier or in_call or ptt}.get(rc["mode"], carrier)
        if cond:
            self.relay_until = now + float(rc["hold_ms"]) / 1000
        if self._relay_override is not None:
            self.relay.set(self._relay_override)
        else:
            self.relay.set(cond or now < self.relay_until)

    # ---------- RX overs ----------
    def _start_over(self, started):
        c = self.config
        oid = self.store.new_over(started, "rx")
        self.over = {"id": oid, "started": started, "forward": False}
        self.peak_v, self.v_sum, self.v_n = None, 0.0, 0
        self.rx_stats = afmeter.OverStats()
        self.last_activity = time.time()
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
        for f in pre:
            self.rx_stats.add(f)
        self.event("carrier", f"carrier detected (over #{oid})")

        reason = None
        call = self.call
        if not self.ua:
            reason = "SIP disabled" if not c["sip"]["enabled"] else "SIP not running"
        elif call and call.state == "connected" and self.forwarding:
            # an open call (outbound in reply-hold, or inbound from the NXR): use it
            self.over["forward"] = True
            call.frames.extend(pre)
            lag = call.buffered_ms()
            self.last_lag["rx"] = lag
            self.store.update(oid, forward="sent", lag_ms=lag,
                              forward_detail="on inbound call" if call.direction == "in" else "joined open call")
            return
        elif call and call.state in ("calling", "trying", "ringing"):
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
        self.last_activity = ended
        dur = ended - o["started"]
        res = self.rx_stats.result() if self.rx_stats else {}
        self.rx_stats = None
        alarms = afmeter.check_over_alarms(res, self.config["af"], "RX")
        for a in alarms:
            self.event("alarm", f"over #{o['id']}: {a}")
        kw = {"ended": ended, "duration": round(dur, 2),
              "peak_v": round(self.peak_v, 3) if self.peak_v is not None else None,
              "avg_v": round(self.v_sum / self.v_n, 3) if self.v_n else None,
              "avg_dbfs": res.get("avg_dbfs"), "peak_dbfs": res.get("peak_dbfs"), "clips": res.get("clips"),
              "alarms": json.dumps(alarms) if alarms else None}
        self.store.update(o["id"], **kw)
        rec, self.rec = self.rec, None
        if rec:
            threading.Thread(target=rec.close, daemon=True).start()
        self.event("carrier", f"over #{o['id']} ended ({dur:.1f} s)")

    # ---------- SIP: outbound ----------
    def _spawn_call(self, oid):
        if self.call_task and not self.call_task.done():
            return
        self.call_task = asyncio.create_task(self._run_call(oid))

    def _call_state(self, st, err):
        self.event("sip" if st != "failed" else "error", f"call {st}" + (f": {err}" if err else ""))

    def _busy_for_call(self, test):
        """Whether anything still needs the call open."""
        call = self.call
        with self._lock:
            rx = (self.over is not None and self.over.get("forward")) if not test else False
        return rx or bool(call and call.frames) or self.tx.active()

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
            t_d = time.time()
            await self._send_dtmf(call)
            dtmf_ms = round((time.time() - t_d) * 1000) if (sc["dtmf_digits"] or "").strip() else 0
            with self._lock:
                if test_frames is not None:
                    call.frames.extend(test_frames)
                else:
                    call.frames.extend(self.pending)
                    self.pending.clear()
                self.forwarding = True
                lag = call.buffered_ms() if test_frames is None else None
            self.last_lag.update(setup_ms=call.setup_ms, dtmf_ms=dtmf_ms)
            if lag is not None:
                self.last_lag["rx"] = lag
                self.event("sip", f"streaming to NXR {lag / 1000:.2f} s behind live "
                                  f"(setup {call.setup_ms} ms{f', DTMF {dtmf_ms} ms' if dtmf_ms else ''})")
            for oid in oids:
                self.store.update(oid, forward="sent", forward_detail=None, lag_ms=lag)

            # keep the call while airband audio flows, NXR audio flows, or within the reply window
            max_s = float(sc["max_call_s"])
            hold = float(sc["reply_hold_s"])
            self.last_activity = time.time()
            while call.state == "connected":
                now = time.time()
                if self._busy_for_call(test_frames is not None):
                    self.last_activity = now
                elif now - self.last_activity >= hold:
                    await asyncio.sleep(0.3)        # let the last packets out
                    break
                if now - call.answered_at > max_s:
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

    async def _send_dtmf(self, call):
        sc = self.config["sip"]
        digits = (sc["dtmf_digits"] or "").strip()
        if not digits:
            return
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

    # ---------- SIP: inbound ----------
    def _on_incoming(self, call):
        call.on_state = self._call_state
        self.event("sip", f"inbound call from {call.peer_addr[0]} ({call.codec_name})")
        self.call_task = asyncio.create_task(self._run_inbound(call))

    async def _run_inbound(self, call):
        idle = float(self.config["sip"]["inbound_idle_s"])
        with self._lock:
            # an airband over already in progress goes out on this call too
            if self.over is not None and not self.over.get("forward"):
                self.over["forward"] = True
                self.store.update(self.over["id"], forward="sent", forward_detail="on inbound call")
            call.frames.extend(self.pending)
            self.pending.clear()
            self.forwarding = True
        self.last_activity = time.time()
        try:
            while call.state in ("new", "connected"):
                now = time.time()
                if self._busy_for_call(False):
                    self.last_activity = now
                if idle > 0 and now - self.last_activity > idle and call.state == "connected":
                    self.event("sip", f"inbound call idle {idle:.0f} s; hanging up")
                    await call.hangup()
                    break
                await asyncio.sleep(0.1)
        finally:
            with self._lock:
                self.forwarding = False
            self.last_call_end = time.time()

    # ---------- test / manual ----------
    async def run_delay_test(self, listen_s=10.0):
        """Measure every part of the path; time the round trip if the NXR side echoes audio."""
        if not self.ua:
            raise RuntimeError("SIP is disabled or not running")
        if self.call and self.call.state in LIVE_CALL:
            raise RuntimeError("a call is already in progress")
        c, sc = self.config, self.config["sip"]
        r = {"state": "running", "started": time.time(), "step": "SIP ping"}
        self.delay_test = r
        self.event("info", "delay test started")
        try:
            pings = [x for x in [await self.ua.ping() for _ in range(3)] if x is not None]
            r["ping_ms"] = round(sum(pings) / len(pings), 1) if pings else None
            r["ping_note"] = None if pings else "NXR did not answer SIP OPTIONS"
            r["step"] = "calling"
            call = await self.ua.place_call(deque(), self._call_state)
            if not call:
                raise RuntimeError(f"call failed: {self.ua.call.error if self.ua.call else 'unknown'}")
            r["setup_ms"] = call.setup_ms
            await asyncio.sleep(float(sc["post_answer_delay_ms"]) / 1000)
            t_d = time.time()
            await self._send_dtmf(call)
            r["dtmf_ms"] = round((time.time() - t_d) * 1000) if (sc["dtmf_digits"] or "").strip() else 0
            r["step"] = "sending tone, listening for echo"
            self._dt_listen = True
            call.rx_log = []
            lead, burst, tail = latency.tone_burst_frames()
            mark = sip_mark()
            call.frames.extend(lead)
            call.frames.append(mark)
            call.frames.extend(burst + tail)
            deadline = time.time() + listen_s
            onset = None
            while time.time() < deadline and call.state == "connected":
                await asyncio.sleep(0.25)
                if mark.t:
                    onset = latency.find_tone_onset(list(call.rx_log), mark.t)
                    if onset:
                        break
            r["echo_ms"] = round((onset - mark.t) * 1000) if onset and mark.t else None
            await call.hangup()
        except Exception as e:
            r["error"] = str(e)
            log.warning("delay test: %s", e)
            if self.call and self.call.state == "connected":
                await self.call.hangup()
        finally:
            self._dt_listen = False
            if self.call:
                self.call.rx_log = None
            self.last_call_end = time.time()
        # the parts AirBridge itself adds (the NXR's own NXDN processing is extra)
        attack, fwd = float(c["detect"]["attack_ms"]), float(sc["forward_preroll_ms"])
        post = float(sc["post_answer_delay_ms"])
        if r.get("setup_ms") is not None:
            r["rx_new_call_ms"] = round(attack + fwd + r["setup_ms"] + post + (r.get("dtmf_ms") or 0))
        r["rx_open_call_ms"] = round(attack + fwd)
        r["tx_ms"] = round(float(c["tx"]["jitter_ms"]) + float(c["tx"]["ptt_lead_ms"]) + self.audio.out_latency_ms)
        r["state"] = "done"
        r["finished"] = time.time()
        self.event("info", "delay test finished" + (f": echo round trip {r['echo_ms']} ms" if r.get("echo_ms")
                                                     else " (no echo detected)" if not r.get("error") else f": {r['error']}"))
        return r

    async def test_call(self, seconds=5.0):
        if not self.ua:
            raise RuntimeError("SIP is disabled or not running")
        if (self.call_task and not self.call_task.done()) or (self.call and self.call.state in LIVE_CALL):
            raise RuntimeError("a call is already in progress")
        frames = list(tone_frames(seconds, 1000.0, -10.0))
        self.event("info", f"test call: {seconds:.0f} s of 1 kHz tone")
        self.call_task = asyncio.create_task(self._run_call(None, test_frames=frames))

    def test_tx(self, seconds, freq, level_dbfs):
        if not self.config["tx"]["enabled"]:
            raise RuntimeError("TX is disabled")
        if self.tx.state != "idle":
            raise RuntimeError("transmitter busy")
        self.event("info", f"TX test: {seconds:.0f} s of {freq:.0f} Hz at {level_dbfs:.0f} dBFS")
        self.tx.local.extend(tone_burst(seconds, freq, level_dbfs))

    def release_ptt(self):
        self.tx.force_release()
        self.event("info", "PTT released manually")

    async def hangup(self):
        if self.call:
            with self._lock:
                if self.over:
                    self.over["forward"] = False
            self.tx.force_release()
            await self.call.hangup()

    def set_relay_override(self, state):
        self._relay_override = state
        self.event("info", f"relay override: {'auto' if state is None else ('ON' if state else 'OFF')}")

    # ---------- status ----------
    def status(self):
        c = self.config
        d = self.det
        call = self.call
        now = time.time()
        over = self.over
        af = c["af"]
        rx_x = self.rx_meter.samples(4096)
        tx_x = self.tx_meter.samples(4096)
        return {
            "type": "status",
            "t": now,
            "site": c["general"]["site_name"],
            "version": version.label(self.build),
            "simulate": self.sim,
            "rssi_v": None if d.rssi is None else round(d.rssi, 3),
            "rssi_dbm": (lambda x: None if x is None else round(x, 1))(volts_to_dbm(d.rssi, c["rssi"])),
            "on_v": round(self.det.thresholds()[0], 3),
            "off_v": round(self.det.thresholds()[1], 3),
            "rssi_mode": "adaptive" if self.det.adaptive else ("learning" if c["rssi"]["threshold_mode"] == "adaptive" else "fixed"),
            "rssi_floor": None if self.det.floor.floor is None else round(self.det.floor.floor, 3),
            "rssi_offsets": {"on_offset_v": c["rssi"]["on_offset_v"], "off_offset_v": c["rssi"]["off_offset_v"]},
            "cor": d.cor,
            "detector": d.state,
            "detect": {
                "sources": sources_of(c["detect"]),
                "combine": c["detect"].get("combine", "any"),
                "rssi": d.rssi_latched if d.rssi is not None else None,
                "cor": d.cor,
                "af": self.af_det.latched,
                "af_mode": c["afdetect"]["mode"],
                "af_value": None if self.af_det.value is None else round(self.af_det.value, 1),
                "af_on": c["afdetect"]["noise_on_dbfs" if c["afdetect"]["mode"] == "noise" else "level_on_dbfs"],
                "af_off": c["afdetect"]["noise_off_dbfs" if c["afdetect"]["mode"] == "noise" else "level_off_dbfs"],
                "hf_dbfs": None if self.audio.hf_db is None else round(self.audio.hf_db, 1),
            },
            "carrier": d.carrier,
            "guard": self.tx.guard_active(now),
            "over_s": round(now - over["started"], 1) if over else None,
            "over_id": over["id"] if over else None,
            "audio_dev": self.audio.device_name,
            "af": {
                "rx": self.rx_meter.snapshot(),
                "tx": self.tx_meter.snapshot(),
                "tone_rx": afmeter.analyze_tone(rx_x, float(af["rx_cal_dbm"])),
                "tone_tx": afmeter.analyze_tone(tx_x, float(af["tx_cal_dbm"])) if self.tx.ptt else None,
                "spec_rx": afmeter.spectrum(rx_x),
                "spec_tx": afmeter.spectrum(tx_x),
                "tone_freq": af["tone_freq"],
            },
            "alarms": sorted(self.alarms),
            "relay": self.relay.state,
            "lag": self.last_lag,
            "delay_test": self.delay_test,
            "lcd": self.lcd.mirror(),
            "relay_override": self._relay_override,
            "tx": {**self.tx.snapshot(), "hid": self.k5.path, "rx_buffer_ms":
                   round(sum(len(f) for f in list(call.rx_frames)) / 8) if call else 0},
            "sip": {
                "enabled": bool(self.ua),
                "target": f"{c['sip']['target_host']}:{c['sip']['target_port']}",
                "registered": self.ua.registered if self.ua and c["sip"]["register"] else None,
                "reg_error": self.ua.reg_error if self.ua else None,
                "state": call.state if call else "idle",
                "direction": call.direction if call else None,
                "codec": call.codec_name if call else c["sip"]["codec"],
                "connected_s": round(now - call.answered_at, 1) if call and call.answered_at and call.state == "connected" else None,
                "buffer_ms": round(call.buffered_ms() if call and call.state == "connected" else len(self.pending) * 20),
                "rx_packets": call.rx_packets if call else 0,
                "setup_ms": call.setup_ms if call else self.last_lag["setup_ms"],
                "last_error": self.ua.last_error if self.ua else None,
            },
            "errors": self._hw_errors(),
            "today": self.store.stats_today(),
            "sim_auto": hardware.SIM.auto,
            "sim_forced": hardware.SIM.forced,
            "sim_tone": hardware.SIM.tone,
        }

    ALARM_SHORT = {"no_audio": "carrier no audio", "rx_clip": "RX clipping", "tx_clip": "TX clipping"}

    def lcd_snapshot(self):
        """Cheap snapshot for the front-panel LCD (no FFTs, cached DB counts)."""
        now = time.time()
        d, call, tx = self.det, self.call, self.tx
        if now - self._today_cache[0] > 5:
            self._today_cache = (now, self.store.stats_today())
        if tx.ptt:
            state = "tx"
        elif tx.state == "dropping":
            state = "drop"
        else:
            state = d.state
        over = self.over
        return {
            "state": state,
            "tx_s": (now - tx.key_time) if tx.ptt and tx.key_time else None,
            "over_s": (now - over["started"]) if over else None,
            "rssi_v": d.rssi if "rssi" in sources_of(self.config["detect"]) or d.rssi is not None else None,
            "on_v": d.thresholds()[0],
            "af_value": self.af_det.value,
            "sip": ("disabled" if not self.ua else call.state if call and call.state in LIVE_CALL + ("failed",)
                    and not (call.state == "failed" and now - (call.started or 0) > 30) else "idle"),
            "sip_in": bool(call and call.direction == "in"),
            "sip_s": (now - call.answered_at) if call and call.answered_at and call.state == "connected" else None,
            "sip_err": call.error if call else None,
            "alarms": [self.ALARM_SHORT.get(a, a) for a in sorted(self.alarms)] + self._hw_errors()[:1],
            "today": self._today_cache[1],
        }

    async def _housekeep(self):
        """Purge old recordings hourly; save the RSSI history every 5 minutes."""
        n = 0
        while True:
            if n % 12 == 0:
                try:
                    k = self.store.purge(int(self.config["record"]["keep_days"]))
                    if k:
                        self.event("info", f"purged {k} old recordings")
                except Exception as e:
                    log.warning("purge: %s", e)
            if n:
                await asyncio.get_running_loop().run_in_executor(None, self.history.save)
            n += 1
            await asyncio.sleep(300)


class _PttTap:
    """Passes PTT to the K5TRA and mirrors it into the simulator."""

    def __init__(self, k5, sim):
        self.k5, self.sim = k5, sim

    def set_ptt(self, on):
        self.k5.set_ptt(on)
        if self.sim:
            hardware.SIM.ptt = bool(on)
