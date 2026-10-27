"""CM119A (K5TRA) HID GPIO: PTT output and COS input over /dev/hidraw.

The CM108/CM119 family exposes GPIOs through a HID output report
[report-id 0, 0, data, direction-mask, 0]; this is the same method
Direwolf and AllStar's simpleusb driver use. COS is read from the HID
input report button bits (VOLDN = bit 1 on AllStar-style interfaces).
"""
import glob
import logging
import os
import queue
import threading

log = logging.getLogger("k5tra")

CMEDIA_VID = "00000D8C"


def find_hidraw():
    """Return /dev/hidrawN paths for C-Media devices."""
    out = []
    for p in sorted(glob.glob("/sys/class/hidraw/hidraw*/device/uevent")):
        try:
            with open(p) as f:
                txt = f.read().upper()
        except OSError:
            continue
        if CMEDIA_VID in txt:
            out.append("/dev/" + p.split("/")[4])
    return out


class K5tra:
    def __init__(self, cfg, simulate):
        self.cfg = cfg
        self.sim = simulate
        self.path = None
        self.fd = None
        self.error = None
        self.ptt = False
        self._cos = False
        self._q = queue.Queue()
        self._stop = threading.Event()
        if simulate:
            self.path = "simulated"
        else:
            self._open()
        self._thr = threading.Thread(target=self._writer, daemon=True)
        self._thr.start()

    def _open(self):
        path = (self.cfg.get("hid_device") or "").strip()
        if not path:
            found = find_hidraw()
            if not found:
                self.error = "K5TRA HID not found (no C-Media hidraw device)"
                log.error(self.error)
                return
            path = found[0]
        try:
            self.fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
            self.path = path
            self._write(False)                      # make sure PTT starts released
            log.info("K5TRA HID on %s", path)
        except OSError as e:
            self.error = f"K5TRA HID {path}: {e}"
            log.error(self.error)

    def _write(self, on):
        if self.fd is None:
            return
        bit = 1 << (int(self.cfg["ptt_gpio"]) - 1)
        level = on != bool(self.cfg.get("ptt_invert"))
        os.write(self.fd, bytes([0, 0, bit if level else 0, bit, 0]))

    def _writer(self):
        while not self._stop.is_set():
            try:
                on = self._q.get(timeout=0.5)
            except queue.Empty:
                continue
            try:
                self._write(on)
            except OSError as e:
                self.error = f"PTT write: {e}"
                log.error(self.error)

    def set_ptt(self, on):
        on = bool(on)
        if on != self.ptt:
            self.ptt = on
            self._q.put(on)

    def read_cos(self):
        """Latest COS state from HID input reports (None if unavailable)."""
        if self.sim:
            return None
        if self.fd is None:
            return None
        try:
            while True:
                r = os.read(self.fd, 16)
                if not r:
                    break
                # some kernels prefix the report id; the button byte is the first non-id byte
                b = r[1] if len(r) == 5 else r[0]
                self._cos = bool(b & (1 << int(self.cfg["cos_bit"])))
        except BlockingIOError:
            pass
        except OSError as e:
            self.error = f"COS read: {e}"
            return None
        return self._cos != bool(self.cfg.get("cos_invert"))

    def close(self):
        try:
            self._write(False)
        except OSError:
            pass
        self.ptt = False
        self._stop.set()
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
