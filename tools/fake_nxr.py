#!/usr/bin/env python3
"""Stand-in for the NXR-1800 SIP interface, for bench testing AirBridge.

Answers INVITEs, optionally challenges with digest auth, records received
G.711 audio to WAV, prints RFC 2833 / INFO DTMF digits, and replies to BYE.

  python3 tools/fake_nxr.py --port 5070 --out /tmp/nxr --auth secret
"""
import argparse
import hashlib
import os
import random
import re
import socket
import struct
import sys
import threading
import time
import wave

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from airbridge.g711 import alaw_decode, ulaw_decode  # noqa: E402

EVENTS = "0123456789*#ABCD"


def hdrs(msg):
    head = msg.split("\r\n\r\n", 1)[0].split("\r\n")
    out = {}
    for ln in head[1:]:
        k, _, v = ln.partition(":")
        out.setdefault(k.strip().lower(), []).append(v.strip())
    return head[0], out, msg.split("\r\n\r\n", 1)[1] if "\r\n\r\n" in msg else ""


class Call:
    def __init__(self, args, call_id):
        self.args = args
        self.rtp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.rtp.bind(("0.0.0.0", 0))
        self.rtp.settimeout(0.5)
        self.port = self.rtp.getsockname()[1]
        os.makedirs(args.out, exist_ok=True)
        self.path = os.path.join(args.out, f"call-{int(time.time())}-{call_id[:6]}.wav")
        self.packets = 0
        self.digits = ""
        self.alive = True
        self.first_pkt = None
        threading.Thread(target=self.run, daemon=True).start()

    def run(self):
        w = wave.open(self.path, "wb")
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(8000)
        last_ev_ts = None
        while self.alive:
            try:
                data, _ = self.rtp.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                break
            if len(data) < 12:
                continue
            pt = data[1] & 0x7F
            ts = struct.unpack("!I", data[4:8])[0]
            payload = data[12:]
            self.packets += 1
            if self.first_pkt is None:
                self.first_pkt = time.time()
            if pt == 0:
                w.writeframes(ulaw_decode(payload).tobytes())
            elif pt == 8:
                w.writeframes(alaw_decode(payload).tobytes())
            elif pt == 101 and len(payload) >= 4:
                ev, flags = payload[0], payload[1]
                if flags & 0x80 and ts != last_ev_ts:
                    last_ev_ts = ts
                    self.digits += EVENTS[ev]
                    print(f"[nxr] DTMF {EVENTS[ev]} (rfc2833)", flush=True)
        w.close()

    def stop(self):
        self.alive = False
        time.sleep(0.6)
        self.rtp.close()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5070)
    ap.add_argument("--out", default="/tmp/fake_nxr")
    ap.add_argument("--auth", default="", help="require digest auth with this password")
    ap.add_argument("--codec", default="PCMU", choices=["PCMU", "PCMA"])
    ap.add_argument("--no-dtmf", action="store_true", help="omit telephone-event from SDP")
    ap.add_argument("--reject", type=int, default=0, help="reject INVITEs with this status")
    args = ap.parse_args()

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.bind(("0.0.0.0", args.port))
    print(f"[nxr] listening on udp/{args.port}", flush=True)
    calls = {}
    nonce = os.urandom(8).hex()
    my_tag = os.urandom(4).hex()
    pt = 0 if args.codec == "PCMU" else 8

    def reply(addr, h, code, reason, body="", extra=""):
        lines = [f"SIP/2.0 {code} {reason}"] + [f"Via: {v}" for v in h["via"]]
        to = h["to"][0] + ("" if "tag=" in h["to"][0] else f";tag={my_tag}")
        lines += [f"From: {h['from'][0]}", f"To: {to}", f"Call-ID: {h['call-id'][0]}", f"CSeq: {h['cseq'][0]}"]
        if body:
            lines += [f"Contact: <sip:nxr@127.0.0.1:{args.port}>", "Content-Type: application/sdp"]
        sock.sendto(("\r\n".join(lines) + "\r\n" + extra + f"Content-Length: {len(body)}\r\n\r\n" + body).encode(), addr)

    while True:
        data, addr = sock.recvfrom(65535)
        msg = data.decode(errors="replace")
        start, h, body = hdrs(msg)
        if start.startswith("SIP/"):
            continue
        method, uri = start.split()[:2]
        cid = h.get("call-id", [""])[0]
        print(f"[nxr] <- {method} {uri}", flush=True)
        if method == "REGISTER":
            reply(addr, h, 200, "OK", extra="Expires: 300\r\n")
        elif method == "INVITE":
            if cid in calls:
                reply(addr, h, 200, "OK", body=calls[cid][1]); continue
            if args.reject:
                reply(addr, h, args.reject, "Rejected"); continue
            if args.auth:
                auth = (h.get("authorization") or [""])[0]
                if not auth:
                    reply(addr, h, 401, "Unauthorized",
                          extra=f'WWW-Authenticate: Digest realm="nxr", nonce="{nonce}", qop="auth"\r\n')
                    continue
                p = dict(re.findall(r'(\w+)="?([^",]*)"?', auth[7:]))
                ha1 = hashlib.md5(f"{p['username']}:nxr:{args.auth}".encode()).hexdigest()
                ha2 = hashlib.md5(f"INVITE:{p['uri']}".encode()).hexdigest()
                exp = hashlib.md5(f"{ha1}:{nonce}:{p['nc']}:{p['cnonce']}:auth:{ha2}".encode()).hexdigest()
                if exp != p.get("response"):
                    reply(addr, h, 403, "Forbidden"); continue
            reply(addr, h, 100, "Trying")
            reply(addr, h, 180, "Ringing")
            time.sleep(0.4)
            c = Call(args, cid)
            m = re.search(r"c=IN IP4 (\S+)", body)
            sdp = ("v=0\r\no=nxr 1 1 IN IP4 127.0.0.1\r\ns=-\r\nc=IN IP4 127.0.0.1\r\nt=0 0\r\n"
                   f"m=audio {c.port} RTP/AVP {pt}{'' if args.no_dtmf else ' 101'}\r\n"
                   f"a=rtpmap:{pt} {args.codec}/8000\r\n"
                   + ("" if args.no_dtmf else "a=rtpmap:101 telephone-event/8000\r\n"))
            calls[cid] = (c, sdp)
            reply(addr, h, 200, "OK", body=sdp)
        elif method == "ACK":
            pass
        elif method == "INFO":
            m = re.search(r"Signal=(\S)", body)
            if m and cid in calls:
                calls[cid][0].digits += m.group(1)
                print(f"[nxr] DTMF {m.group(1)} (info)", flush=True)
            reply(addr, h, 200, "OK")
        elif method == "BYE":
            reply(addr, h, 200, "OK")
            if cid in calls:
                c = calls.pop(cid)[0]
                c.stop()
                print(f"[nxr] call ended: {c.packets} RTP packets, digits='{c.digits}', saved {c.path}", flush=True)
        else:
            reply(addr, h, 200, "OK")


if __name__ == "__main__":
    main()
