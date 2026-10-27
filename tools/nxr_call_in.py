#!/usr/bin/env python3
"""Pretend to be the NXR placing an inbound call to AirBridge.

  python3 tools/nxr_call_in.py --to 127.0.0.1:5062 --talk 3 --hold 6
"""
import argparse
import asyncio
import os
import sys
from collections import deque

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from airbridge.sip import SipUA  # noqa: E402


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--to", default="127.0.0.1:5062")
    ap.add_argument("--port", type=int, default=5071)
    ap.add_argument("--talk", type=float, default=3.0, help="seconds of 700 Hz voice-ish audio")
    ap.add_argument("--delay", type=float, default=0.5)
    ap.add_argument("--hold", type=float, default=6.0, help="seconds to stay on the call")
    a = ap.parse_args()
    host, port = a.to.split(":")
    cfg = {"target_host": host, "target_port": int(port), "target_user": "airbridge", "local_port": a.port,
           "local_user": "nxr", "display_name": "NXR-1800", "auth_user": "", "auth_password": "",
           "register": False, "register_expires": 300, "codec": "PCMU", "ptime_ms": 20, "dtmf_mode": "rfc2833",
           "send_gain_db": 0.0, "inbound_enabled": False}
    ua = SipUA(cfg)
    await ua.start()
    frames = deque()
    call = await ua.place_call(frames)
    if not call:
        print("[nxr-in] call failed:", ua.call.error); return
    print("[nxr-in] connected", flush=True)
    await asyncio.sleep(a.delay)
    n = int(a.talk / 0.02)
    for i in range(n):
        t = (np.arange(160) + i * 160) / 8000
        frames.append((0.3 * np.sin(2 * np.pi * 700 * t) * 32767).astype(np.int16))
    await asyncio.sleep(a.hold)
    got = len(call.rx_frames)
    print(f"[nxr-in] received {call.rx_packets} RTP packets from AirBridge ({got} audio frames buffered)", flush=True)
    await call.hangup()
    await ua.stop()

asyncio.run(main())
