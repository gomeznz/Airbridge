"""20x4 character LCD (HD44780) on an I2C PCF8574 backpack.

Wiring: VCC 5 V, GND, SDA -> GPIO2 (pin 3), SCL -> GPIO3 (pin 5), shared
with the ADS1115. The usual backpacks sit at 0x27 (PCF8574) or 0x3F
(PCF8574A); 'auto' probes both. Most backpacks pull SDA/SCL up to 5 V:
check yours, or use a level shifter, since the Pi's pins are 3.3 V only.

Layout (20 columns):
  1  state + clock        "CARRIER 4.2s   23:47"
  2  RSSI bar + volts     "RSSI ▮▮▮▮▮▯▯▯ 1.23V"
  3  SIP / TX state       "SIP connected  12s" | "TX keyed 3.1s"
  4  alarms, else rotates IP address / today's counts
"""
import logging
import socket
import threading
import time

log = logging.getLogger("lcd")

# PCF8574 -> HD44780 pin map used by nearly all "LCD2004 I2C" backpacks
RS, RW, EN, BL = 0x01, 0x02, 0x04, 0x08
ROW = (0x00, 0x40, 0x14, 0x54)
COLS, ROWS = 20, 4

# CGRAM glyphs 0..5: a cell with 0..5 columns filled, for smooth bar graphs
BAR_GLYPHS = [[sum(1 << (4 - c) for c in range(n))] * 8 for n in range(6)]
FULL, EMPTY = "\x05", "\x00"


class Hd44780:
    def __init__(self, bus, address):
        from smbus2 import SMBus
        self.bus = SMBus(bus)
        self.addr = address
        self.backlight = True
        self._init()

    def _w(self, b):
        self.bus.write_byte(self.addr, b | (BL if self.backlight else 0))

    def _nibble(self, n, mode):
        b = (n & 0xF0) | mode
        self._w(b | EN)
        self._w(b)

    def _byte(self, v, mode=0):
        self._nibble(v & 0xF0, mode)
        self._nibble((v << 4) & 0xF0, mode)

    def cmd(self, v):
        self._byte(v, 0)
        if v in (0x01, 0x02):
            time.sleep(0.002)

    def _init(self):
        time.sleep(0.05)
        for _ in range(3):                      # force 8-bit, then switch to 4-bit
            self._nibble(0x30, 0)
            time.sleep(0.005)
        self._nibble(0x20, 0)
        for c in (0x28, 0x08, 0x01, 0x06, 0x0C):   # 2-line/5x8, off, clear, entry, on (no cursor)
            self.cmd(c)
        for i, g in enumerate(BAR_GLYPHS):
            self.cmd(0x40 | (i << 3))
            for row in g:
                self._byte(row, RS)

    def write_at(self, row, col, text):
        self.cmd(0x80 | (ROW[row] + col))
        for ch in text:
            self._byte(ord(ch) & 0xFF, RS)

    def set_backlight(self, on):
        if on != self.backlight:
            self.backlight = on
            self._w(0)

    def close(self):
        try:
            self.cmd(0x01)
            self.set_backlight(False)
        except OSError:
            pass
        self.bus.close()


def probe(bus, candidates=(0x27, 0x3F)):
    from smbus2 import SMBus
    with SMBus(bus) as b:
        for a in candidates:
            try:
                b.read_byte(a)
                return a
            except OSError:
                continue
    return None


def bar(fraction, cells):
    """Bar graph using the 0..5-column glyphs."""
    units = max(0, min(cells * 5, int(round(fraction * cells * 5))))
    full, part = divmod(units, 5)
    s = FULL * full
    if full < cells:
        s += chr(part) + EMPTY * (cells - full - 1)
    return s


def local_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except OSError:
        return "no network"
    finally:
        s.close()


def fit(text, width=COLS):
    return text[:width].ljust(width)


def render(snap, now, page):
    """Build the four 20-char lines from a light status snapshot."""
    t = time.strftime("%H:%M", time.localtime(now))
    st = snap["state"]
    if st == "tx":
        head = f"TX {snap['tx_s']:.1f}s" if snap.get("tx_s") is not None else "TX"
    elif st == "drop":
        head = "BUSY: NXR DROPPED"
    elif st in ("active", "hang"):
        head = f"CARRIER {snap['over_s']:.1f}s" if snap.get("over_s") is not None else "CARRIER"
        if st == "hang":
            head = "HANG " + head[8:]
    elif st == "attack":
        head = "CARRIER?"
    else:
        head = "IDLE"
    l1 = fit(head, COLS - 6) + " " + t

    v = snap.get("rssi_v")
    if v is None:
        if snap.get("af_value") is not None:
            l2 = "AF " + bar(min(1, max(0, (snap["af_value"] + 90) / 90)), 9) + f" {snap['af_value']:.0f}dB"
        else:
            l2 = "RSSI --"
    else:
        full = max(3.3, (snap.get("on_v") or 1) * 1.6)
        l2 = "RSSI" + bar(v / full, 10) + f"{v:5.2f}V"

    sip = snap.get("sip") or "idle"
    if sip == "connected":
        l3 = f"SIP {'IN ' if snap.get('sip_in') else ''}connected {snap.get('sip_s') or 0:.0f}s"
    elif sip in ("calling", "trying", "ringing"):
        l3 = f"SIP {sip}..."
    elif sip == "failed":
        l3 = "SIP FAIL " + (snap.get("sip_err") or "")
    elif sip == "disabled":
        l3 = "SIP disabled"
    else:
        l3 = "SIP idle"

    alarms = snap.get("alarms") or []
    if alarms:
        a = alarms[int(now / 2) % len(alarms)]
        l4 = ("!" if int(now * 2) % 2 else " ") + a.upper()
    elif page % 2 == 0:
        l4 = snap.get("ip") or ""
    else:
        d = snap.get("today", {})
        l4 = f"Ovr{d.get('overs', 0):>3} NXR{d.get('sent', 0):>3} TX{d.get('transmitted', 0):>3}"
    return [fit(l1), fit(l2), fit(l3), fit(l4)]


class Lcd:
    """Runs its own thread; pulls a snapshot from the engine at refresh_hz."""

    def __init__(self, cfg, simulate, snapshot):
        self.cfg = cfg
        self.sim = simulate
        self.snapshot = snapshot
        self.dev = None
        self.address = None
        self.error = None
        self.lines = [" " * COLS] * ROWS
        self.backlight = True
        self._shown = [None] * ROWS
        self._stop = threading.Event()
        self._last_activity = time.time()
        self._ip = local_ip()
        self._ip_t = time.time()
        self._lock = threading.Lock()
        self._thr = None

    def start(self):
        if not self.cfg["enabled"]:
            return
        self._stop.clear()
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()

    def _connect(self):
        if self.sim:
            return
        try:
            addr = self.cfg.get("address", "auto")
            if isinstance(addr, str):
                addr = probe(int(self.cfg["i2c_bus"])) if addr in ("auto", "") else int(addr, 0)
            if addr is None:
                raise OSError("no LCD found at 0x27 or 0x3F")
            self.dev = Hd44780(int(self.cfg["i2c_bus"]), addr)
            self.address = addr
            self._shown = [None] * ROWS
            self.error = None
            log.info("LCD on I2C 0x%02X", addr)
        except Exception as e:
            self.dev = None
            self.error = f"LCD: {e}"

    def _run(self):
        page, page_t, retry_t = 0, time.time(), 0.0
        period = 1.0 / max(1, float(self.cfg["refresh_hz"]))
        while not self._stop.is_set():
            now = time.time()
            if not self.sim and self.dev is None and now >= retry_t:
                self._connect()
                retry_t = now + 10                  # keep retrying if unplugged
            if now - page_t >= float(self.cfg["page_s"]):
                page, page_t = page + 1, now
            if now - self._ip_t > 30:
                self._ip, self._ip_t = local_ip(), now
            try:
                snap = self.snapshot()
                snap["ip"] = self._ip
                lines = render(snap, now, page)
                busy = snap["state"] != "idle" or snap.get("sip") not in ("idle", "disabled", None) or snap.get("alarms")
                if busy:
                    self._last_activity = now
                bl = {"on": True, "off": False}.get(self.cfg["backlight"],
                                                    now - self._last_activity < float(self.cfg["backlight_timeout_s"]))
                with self._lock:
                    self.lines, self.backlight = lines, bl
                if self.dev:
                    self.dev.set_backlight(bl)
                    for r, text in enumerate(lines):
                        if text != self._shown[r]:
                            self._write_diff(r, text)
            except OSError as e:
                self.error = f"LCD: {e}"
                log.warning("LCD write failed: %s", e)
                try:
                    self.dev.bus.close()
                except Exception:
                    pass
                self.dev = None
            except Exception:
                log.exception("lcd")
            self._stop.wait(period)

    def _write_diff(self, r, text):
        """Only rewrite the changed run of characters to keep I2C traffic low."""
        old = self._shown[r]
        if old is None:
            self.dev.write_at(r, 0, text)
        else:
            diff = [i for i in range(COLS) if old[i] != text[i]]
            if diff:
                a, b = diff[0], diff[-1] + 1
                self.dev.write_at(r, a, text[a:b])
        self._shown[r] = text

    def mirror(self):
        with self._lock:
            return {"enabled": bool(self.cfg["enabled"]), "lines": list(self.lines), "backlight": self.backlight,
                    "address": None if self.address is None else f"0x{self.address:02X}",
                    "error": self.error, "simulated": self.sim}

    def stop(self):
        self._stop.set()
        if self._thr:
            self._thr.join(timeout=2)
        if self.dev:
            self.dev.close()
            self.dev = None
