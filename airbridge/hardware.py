"""Hardware I/O: ADS1115 RSSI input, COR GPIO input, relay GPIO output.

Each has a simulated twin so the whole stack runs without a Pi.
"""
import logging
import math
import random
import threading
import time

log = logging.getLogger("hw")

_PGA = {6.144: 0b000, 4.096: 0b001, 2.048: 0b010,
        1.024: 0b011, 0.512: 0b100, 0.256: 0b101}


class ADS1115:
    """Minimal single-shot ADS1115 driver over smbus2 (no Adafruit stack)."""

    def __init__(self, bus=1, address=0x48, channel=0, fsr=4.096):
        from smbus2 import SMBus
        self.bus = SMBus(bus)
        self.addr = address
        self.fsr = min(_PGA, key=lambda v: abs(v - fsr))
        mux = 0b100 + (channel & 3)            # AINx vs GND
        self.cfg = (1 << 15) | (mux << 12) | (_PGA[self.fsr] << 9) | (1 << 8) \
            | (0b111 << 5) | 0b11              # single-shot, 860 SPS, comp off

    def read_volts(self):
        self.bus.write_i2c_block_data(self.addr, 0x01,
                                      [(self.cfg >> 8) & 0xFF, self.cfg & 0xFF])
        time.sleep(0.0015)
        for _ in range(10):                     # wait for conversion done (OS bit)
            hi, lo = self.bus.read_i2c_block_data(self.addr, 0x01, 2)
            if hi & 0x80:
                break
            time.sleep(0.0005)
        hi, lo = self.bus.read_i2c_block_data(self.addr, 0x00, 2)
        raw = (hi << 8) | lo
        if raw & 0x8000:
            raw -= 1 << 16
        return raw * self.fsr / 32768.0


class SimRadio:
    """Shared fake 'radio' state: RSSI volts, COR and an AM-ish audio signal.

    The web Test page can force a carrier on/off; otherwise it is idle
    unless auto mode is switched on (random overs every so often).
    """

    def __init__(self):
        self.forced = None          # None | True | False
        self.auto = False
        self._auto_until = 0.0
        self._next_auto = time.time() + 10
        self.idle_v = 0.45
        self.carrier_v = 2.1

    def carrier(self):
        now = time.time()
        if self.forced is not None:
            return self.forced
        if self.auto:
            if now > self._next_auto:
                self._auto_until = now + random.uniform(2, 8)
                self._next_auto = self._auto_until + random.uniform(8, 25)
            return now < self._auto_until
        return False

    def rssi_v(self):
        base = self.carrier_v if self.carrier() else self.idle_v
        return max(0.0, base + random.gauss(0, 0.03) + 0.05 * math.sin(time.time() * 3))


SIM = SimRadio()


class RssiInput:
    def __init__(self, cfg, simulate):
        self.cfg = cfg
        self.sim = simulate
        self.adc = None
        if not simulate and cfg["enabled"]:
            try:
                self.adc = ADS1115(cfg["i2c_bus"], int(cfg["i2c_address"]),
                                   cfg["channel"], cfg["gain_fsr_v"])
            except Exception as e:      # keep running; status page shows the fault
                log.error("ADS1115 init failed: %s", e)
        self.error = None if (simulate or self.adc or not cfg["enabled"]) else "ADC not found"

    def read(self):
        if not self.cfg["enabled"]:
            return None
        if self.sim:
            return SIM.rssi_v()
        if not self.adc:
            return None
        try:
            v = self.adc.read_volts() * float(self.cfg["divider_ratio"])
            self.error = None
            return v
        except Exception as e:
            self.error = str(e)
            return None


class CorInput:
    def __init__(self, cfg, simulate):
        self.cfg = cfg
        self.sim = simulate
        self.dev = None
        self.error = None
        if cfg["enabled"] and not simulate:
            try:
                from gpiozero import DigitalInputDevice
                self.dev = DigitalInputDevice(cfg["gpio"], pull_up=cfg["pull_up"])
            except Exception as e:
                self.error = f"COR GPIO: {e}"
                log.error(self.error)

    def active(self):
        if not self.cfg["enabled"]:
            return None
        if self.sim:
            return SIM.carrier()
        if not self.dev:
            return None
        level = bool(self.dev.value)
        return (not level) if self.cfg["active_low"] else level

    def close(self):
        if self.dev:
            self.dev.close()


class Relay:
    def __init__(self, cfg, simulate):
        self.cfg = cfg
        self.dev = None
        self.state = False
        self.error = None
        self._lock = threading.Lock()
        if cfg["enabled"] and not simulate:
            try:
                from gpiozero import DigitalOutputDevice
                self.dev = DigitalOutputDevice(cfg["gpio"], active_high=cfg["active_high"],
                                               initial_value=False)
            except Exception as e:
                self.error = f"Relay GPIO: {e}"
                log.error(self.error)

    def set(self, on):
        on = bool(on) and self.cfg["enabled"]
        with self._lock:
            if on == self.state:
                return
            self.state = on
            if self.dev:
                self.dev.on() if on else self.dev.off()

    def close(self):
        self.set(False)
        if self.dev:
            self.dev.close()
