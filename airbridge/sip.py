"""Lean SIP user agent for direct calls to a Kenwood NXR-1000 SIP interface.

No PBX needed: the Pi sends INVITE straight to the repeater's IP.
Supports optional REGISTER, digest auth (401/407), G.711 PCMU/PCMA,
RFC 2833 or SIP INFO DTMF, in-dialog re-INVITE/OPTIONS/BYE from the far
end, and paced RTP fed from a frame queue (silence when empty).
"""
import asyncio
import hashlib
import logging
import random
import re
import socket
import struct
import time
import uuid
from collections import deque

import numpy as np

from .g711 import CODECS

log = logging.getLogger("sip")

T1 = 0.5
COMPACT = {"v": "via", "f": "from", "t": "to", "i": "call-id", "m": "contact",
           "l": "content-length", "c": "content-type", "k": "supported"}


def _rand(n=10):
    return uuid.uuid4().hex[:n]


def local_ip_for(host):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        s.close()


class SipMessage:
    def __init__(self, raw: bytes):
        head, _, body = raw.partition(b"\r\n\r\n")
        lines = head.decode(errors="replace").split("\r\n")
        self.start = lines[0]
        self.headers = []                      # list of (lname, value)
        for ln in lines[1:]:
            if not ln:
                continue
            if ln[0] in " \t" and self.headers:      # folded header
                k, v = self.headers[-1]
                self.headers[-1] = (k, v + " " + ln.strip())
                continue
            k, _, v = ln.partition(":")
            k = k.strip().lower()
            self.headers.append((COMPACT.get(k, k), v.strip()))
        self.body = body.decode(errors="replace")
        p = self.start.split(" ", 2)
        self.is_response = p[0].startswith("SIP/")
        if self.is_response:
            self.status = int(p[1])
            self.reason = p[2] if len(p) > 2 else ""
        else:
            self.method, self.uri = p[0], p[1]

    def get(self, name, default=None):
        name = name.lower()
        for k, v in self.headers:
            if k == name:
                return v
        return default

    def all(self, name):
        name = name.lower()
        out = []
        for k, v in self.headers:
            if k == name:
                out.extend(x.strip() for x in re.split(r",(?![^<]*>)", v))
        return out

    @property
    def cseq(self):
        n, m = self.get("cseq", "0 X").split()
        return int(n), m


def _tag(hval):
    m = re.search(r";\s*tag=([^;>\s]+)", hval or "")
    return m.group(1) if m else None


def _uri(hval):
    m = re.search(r"<([^>]+)>", hval or "")
    return m.group(1) if m else (hval or "").split(";")[0].strip()


def parse_sdp(body):
    ip, port, pts = None, None, []
    rtpmap = {}
    for ln in body.splitlines():
        if ln.startswith("c=IN IP4 "):
            ip = ln.split()[2]
        elif ln.startswith("m=audio "):
            f = ln.split()
            port = int(f[1])
            pts = [int(x) for x in f[3:] if x.isdigit()]
        elif ln.startswith("a=rtpmap:"):
            k, _, v = ln[9:].partition(" ")
            rtpmap[int(k)] = v.split("/")[0].upper()
    return ip, port, pts, rtpmap


def digest(chal, method, uri, user, pw):
    params = dict(re.findall(r'(\w+)="?([^",]*)"?', chal.split(" ", 1)[1]))
    realm, nonce = params.get("realm", ""), params.get("nonce", "")
    ha1 = hashlib.md5(f"{user}:{realm}:{pw}".encode()).hexdigest()
    ha2 = hashlib.md5(f"{method}:{uri}".encode()).hexdigest()
    out = f'Digest username="{user}", realm="{realm}", nonce="{nonce}", uri="{uri}"'
    qop = params.get("qop")
    if qop and "auth" in qop.split(","):
        cn = _rand(8)
        resp = hashlib.md5(f"{ha1}:{nonce}:00000001:{cn}:auth:{ha2}".encode()).hexdigest()
        out += f', qop=auth, nc=00000001, cnonce="{cn}"'
    else:
        resp = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
    out += f', response="{resp}", algorithm=MD5'
    if "opaque" in params:
        out += f', opaque="{params["opaque"]}"'
    return out


class _Proto(asyncio.DatagramProtocol):
    def __init__(self, ua):
        self.ua = ua

    def datagram_received(self, data, addr):
        self.ua._on_datagram(data, addr)


class SipUA:
    """One UDP endpoint; places one outbound call at a time."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.transport = None
        self.local_ip = None
        self._waiters = {}          # (branch, method) -> asyncio.Queue
        self.call = None
        self.registered = False
        self.reg_error = None
        self._reg_task = None
        self.last_error = None
        self.on_incoming = None     # callback(SipCall) for answered inbound calls

    # ---------- transport ----------
    async def start(self):
        loop = asyncio.get_running_loop()
        self.local_ip = local_ip_for(self.cfg["target_host"])
        self.transport, _ = await loop.create_datagram_endpoint(
            lambda: _Proto(self), local_addr=("0.0.0.0", int(self.cfg["local_port"])))
        log.info("SIP UA on %s:%s -> %s:%s", self.local_ip, self.cfg["local_port"],
                 self.cfg["target_host"], self.cfg["target_port"])
        if self.cfg["register"]:
            self._reg_task = asyncio.create_task(self._register_loop())

    async def stop(self):
        if self.call:
            await self.call.hangup()
        if self._reg_task:
            self._reg_task.cancel()
        if self.transport:
            self.transport.close()

    @property
    def target(self):
        return (self.cfg["target_host"], int(self.cfg["target_port"]))

    def _send(self, text, addr=None):
        self.transport.sendto(text.encode(), addr or self.target)

    def _on_datagram(self, data, addr):
        if not data.strip():
            return                                   # keepalive
        try:
            msg = SipMessage(data)
        except Exception:
            log.debug("unparseable datagram from %s", addr)
            return
        if msg.is_response:
            via = msg.get("via", "")
            m = re.search(r"branch=([^;\s,]+)", via)
            key = (m.group(1) if m else None, msg.cseq[1])
            q = self._waiters.get(key)
            if q:
                q.put_nowait(msg)
            elif msg.cseq[1] == "INVITE" and msg.status >= 200 and self.call:
                self.call._reack(msg)                 # retransmitted 200
            return
        self._on_request(msg, addr)

    def _on_request(self, req, addr):
        call = self.call
        in_dialog = call and req.get("call-id") == call.call_id
        if req.method == "ACK":
            if in_dialog:
                call._got_ack()
            return
        if req.method == "BYE" and in_dialog:
            self._reply(req, addr, 200, "OK")
            call._remote_bye()
            return
        if req.method == "OPTIONS":
            self._reply(req, addr, 200, "OK", extra="Accept: application/sdp\r\n")
            return
        if req.method in ("INVITE", "UPDATE") and in_dialog:
            self._reply(req, addr, 200, "OK", sdp=call.sdp(), contact=True)
            return
        if req.method == "INFO" and in_dialog:
            self._reply(req, addr, 200, "OK")
            return
        if req.method == "INVITE":
            self._incoming(req, addr)
            return
        self._reply(req, addr, 481 if req.method in ("BYE", "CANCEL") else 405,
                    "Call/Transaction Does Not Exist" if req.method in ("BYE", "CANCEL") else "Method Not Allowed")

    def _reply(self, req, addr, code, reason, sdp=None, contact=False, extra=""):
        to = req.get("to", "")
        if "tag=" not in to:
            to += f";tag={self.call.local_tag if self.call else _rand()}"
        lines = [f"SIP/2.0 {code} {reason}"]
        for v in req.all("via"):
            lines.append(f"Via: {v}")
        lines += [f"From: {req.get('from')}", f"To: {to}",
                  f"Call-ID: {req.get('call-id')}", f"CSeq: {req.get('cseq')}",
                  "User-Agent: AirBridge"]
        if contact:
            lines.append(f"Contact: <sip:{self.cfg['local_user']}@{self.local_ip}:{self.cfg['local_port']}>")
        body = sdp or ""
        if body:
            lines.append("Content-Type: application/sdp")
        text = "\r\n".join(lines) + "\r\n" + extra + f"Content-Length: {len(body)}\r\n\r\n" + body
        self._send(text, addr)

    # ---------- inbound calls ----------
    def _allowed(self, host):
        allow = {self.cfg["target_host"].strip()}
        allow |= {h.strip() for h in (self.cfg.get("inbound_allow") or "").split(",") if h.strip()}
        return host in allow

    def _incoming(self, req, addr):
        if not self.cfg.get("inbound_enabled", True):
            self._reply(req, addr, 403, "Forbidden")
            return
        if not self._allowed(addr[0]):
            log.warning("inbound INVITE from %s refused (not allowed)", addr[0])
            self._reply(req, addr, 403, "Forbidden")
            return
        if self.call and self.call.state not in ("ended", "failed"):
            if self.call.call_id == req.get("call-id"):
                return                               # retransmission while we answer
            self._reply(req, addr, 486, "Busy Here")
            return
        call = SipCall(self, deque(), None, direction="in")
        ok, code, reason = call.prepare_inbound(req, addr)
        if not ok:
            self._reply(req, addr, code, reason)
            return
        self.call = call
        if self.on_incoming:
            self.on_incoming(call)                   # engine sets on_state etc.
        self._reply(req, addr, 100, "Trying")
        asyncio.create_task(call.answer(req, addr))

    # ---------- client transactions ----------
    async def transact(self, build, method, final_timeout=32.0, invite=False, addr=None):
        """Send a request built by build(branch) with retransmission.

        Returns the final response (or None on timeout). Provisional
        responses for INVITE are passed to self.call.on_provisional.
        """
        branch = "z9hG4bK" + _rand(12)
        text = build(branch)
        q = asyncio.Queue()
        self._waiters[(branch, method)] = q
        try:
            interval, deadline = T1, time.monotonic() + final_timeout
            got_prov = False
            self._send(text, addr)
            nxt = time.monotonic() + interval
            while time.monotonic() < deadline:
                wait = max(0.01, (nxt if not got_prov else deadline) - time.monotonic())
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=min(wait, deadline - time.monotonic() + 0.01))
                except asyncio.TimeoutError:
                    if not got_prov and time.monotonic() >= nxt:
                        self._send(text, addr)
                        interval = min(interval * 2, 4.0)
                        nxt = time.monotonic() + interval
                    continue
                if msg.status < 200:
                    got_prov = True
                    if invite and self.call:
                        self.call.on_provisional(msg)
                    continue
                return msg, branch
            return None, branch
        finally:
            self._waiters.pop((branch, method), None)

    # ---------- registration ----------
    def _aor(self):
        return f"sip:{self.cfg['local_user']}@{self.cfg['target_host']}"

    async def register_once(self):
        cfg = self.cfg
        call_id, ltag = _rand(16) + "@airbridge", _rand()
        uri = f"sip:{cfg['target_host']}:{cfg['target_port']}"
        auth = None
        for cseq in (1, 2):
            def build(br, auth=auth, cseq=cseq):
                h = [f"REGISTER {uri} SIP/2.0",
                     f"Via: SIP/2.0/UDP {self.local_ip}:{cfg['local_port']};branch={br};rport",
                     "Max-Forwards: 70",
                     f"From: <{self._aor()}>;tag={ltag}", f"To: <{self._aor()}>",
                     f"Call-ID: {call_id}", f"CSeq: {cseq} REGISTER",
                     f"Contact: <sip:{cfg['local_user']}@{self.local_ip}:{cfg['local_port']}>",
                     f"Expires: {int(cfg['register_expires'])}", "User-Agent: AirBridge"]
                if auth:
                    h.append(auth)
                return "\r\n".join(h) + "\r\nContent-Length: 0\r\n\r\n"
            resp, _ = await self.transact(build, "REGISTER")
            if resp is None:
                raise RuntimeError("REGISTER timeout")
            if resp.status in (401, 407) and cseq == 1:
                hdr = "www-authenticate" if resp.status == 401 else "proxy-authenticate"
                name = "Authorization" if resp.status == 401 else "Proxy-Authorization"
                auth = f"{name}: " + digest(resp.get(hdr), "REGISTER", uri,
                                            cfg["auth_user"] or cfg["local_user"], cfg["auth_password"])
                continue
            if resp.status >= 300:
                raise RuntimeError(f"REGISTER {resp.status} {resp.reason}")
            return True

    async def _register_loop(self):
        while True:
            try:
                await self.register_once()
                self.registered, self.reg_error = True, None
                await asyncio.sleep(max(30, int(self.cfg["register_expires"]) * 0.8))
            except asyncio.CancelledError:
                raise
            except Exception as e:
                self.registered, self.reg_error = False, str(e)
                log.warning("register: %s", e)
                await asyncio.sleep(30)

    # ---------- calls ----------
    async def place_call(self, frames: deque, on_state=None):
        if self.call and self.call.state not in ("ended", "failed"):
            raise RuntimeError("call already in progress")
        self.call = SipCall(self, frames, on_state)
        ok = await self.call.invite()
        return self.call if ok else None


class SipCall:
    def __init__(self, ua: SipUA, frames: deque, on_state=None, direction="out"):
        self.ua = ua
        self.direction = direction
        self.peer_addr = None
        self.remote_from = None          # inbound: the caller's From header
        self.local_to = None             # inbound: the To header we answered with
        self.rx_frames = deque(maxlen=600)   # decoded audio from the far end (12 s cap)
        self.rx_packets = 0
        self.last_rx = 0.0
        self._acked = asyncio.Event()
        self.cfg = ua.cfg
        self.frames = frames
        self.on_state = on_state
        self.call_id = _rand(16) + "@airbridge"
        self.local_tag = _rand()
        self.remote_tag = None
        self.remote_target = None
        self.route = []
        self.cseq = 1
        self.state = "new"
        self.error = None
        self.started = None
        self.answered_at = None
        self.ended = asyncio.Event()
        name = self.cfg["codec"].upper()
        self.codec_name = name if name in CODECS else "PCMU"
        self.pt, self.enc, self.dec = CODECS[self.codec_name]
        self.dtmf_pt = None
        self.rtp_sock = None
        self.rtp_port = None
        self.remote_rtp = None
        self._rtp_task = None
        self._dtmf_queue = deque()
        self._last_invite_ack = None
        self._gain = 10 ** (float(self.cfg["send_gain_db"]) / 20)
        self.packets_sent = 0

    # ----- helpers -----
    def _set(self, st, err=None):
        self.state = st
        if err:
            self.error = err
            self.ua.last_error = err
        log.info("call %s: %s%s", self.call_id[:6], st, f" ({err})" if err else "")
        if self.on_state:
            try:
                self.on_state(st, err)
            except Exception:
                log.exception("on_state")

    @property
    def target_uri(self):
        user = self.cfg["target_user"]
        host, port = self.cfg["target_host"], self.cfg["target_port"]
        return f"sip:{user + '@' if user else ''}{host}:{port}"

    def _from(self):
        if self.direction == "in":
            return self.local_to
        return (f'"{self.cfg["display_name"]}" <sip:{self.cfg["local_user"]}@{self.ua.local_ip}:'
                f'{self.cfg["local_port"]}>;tag={self.local_tag}')

    def _to(self):
        if self.direction == "in":
            return self.remote_from
        return f"<{self.target_uri}>" + (f";tag={self.remote_tag}" if self.remote_tag else "")

    def _contact(self):
        return f"<sip:{self.cfg['local_user']}@{self.ua.local_ip}:{self.cfg['local_port']}>"

    def _open_rtp(self):
        for _ in range(50):
            port = random.randrange(16384, 32766, 2)
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.bind(("0.0.0.0", port))
                s.setblocking(False)
                self.rtp_sock, self.rtp_port = s, port
                return
            except OSError:
                s.close()
        raise RuntimeError("no free RTP port")

    def sdp(self):
        ip = self.ua.local_ip
        sid = int(time.time())
        return ("v=0\r\n"
                f"o=AirBridge {sid} {sid} IN IP4 {ip}\r\n"
                "s=AirBridge\r\n"
                f"c=IN IP4 {ip}\r\n"
                "t=0 0\r\n"
                f"m=audio {self.rtp_port} RTP/AVP {self.pt} 101\r\n"
                f"a=rtpmap:{self.pt} {self.codec_name}/8000\r\n"
                "a=rtpmap:101 telephone-event/8000\r\n"
                "a=fmtp:101 0-15\r\n"
                f"a=ptime:{int(self.cfg['ptime_ms'])}\r\n"
                "a=sendrecv\r\n")

    def _request(self, method, branch, cseq, body="", auth=None, extra=()):
        ruri = self.remote_target or self.target_uri
        h = [f"{method} {ruri} SIP/2.0",
             f"Via: SIP/2.0/UDP {self.ua.local_ip}:{self.cfg['local_port']};branch={branch};rport",
             "Max-Forwards: 70", f"From: {self._from()}", f"To: {self._to()}",
             f"Call-ID: {self.call_id}", f"CSeq: {cseq} {method}"]
        h += [f"Route: {r}" for r in self.route]
        if method == "INVITE":
            h += [f"Contact: {self._contact()}",
                  "Allow: INVITE, ACK, BYE, CANCEL, OPTIONS, INFO, UPDATE",
                  "User-Agent: AirBridge"]
        if auth:
            h.append(auth)
        h += list(extra)
        if body and method == "INVITE":
            h.append("Content-Type: application/sdp")
        return "\r\n".join(h) + f"\r\nContent-Length: {len(body)}\r\n\r\n" + body

    def _ack(self, resp, branch=None):
        """ACK: non-2xx uses the INVITE branch; 2xx is a new transaction."""
        n, _ = resp.cseq
        if resp.status >= 300:
            text = "\r\n".join([
                f"ACK {self.target_uri} SIP/2.0",
                f"Via: SIP/2.0/UDP {self.ua.local_ip}:{self.cfg['local_port']};branch={branch};rport",
                "Max-Forwards: 70", f"From: {self._from()}", f"To: {resp.get('to')}",
                f"Call-ID: {self.call_id}", f"CSeq: {n} ACK", "Content-Length: 0", "", ""])
        else:
            text = self._request("ACK", "z9hG4bK" + _rand(12), n)
        self._last_invite_ack = (n, text)
        self.ua._send(text, self._next_hop())

    def _reack(self, resp):
        if self._last_invite_ack and resp.cseq[0] == self._last_invite_ack[0] and resp.status < 300:
            self.ua._send(self._last_invite_ack[1], self._next_hop())

    def _next_hop(self):
        uri = _uri(self.route[0]) if self.route else (self.remote_target or None)
        if not uri:
            return self.peer_addr
        m = re.match(r"sips?:(?:[^@]+@)?([^:;>]+)(?::(\d+))?", uri)
        if not m:
            return None
        return (m.group(1), int(m.group(2) or 5060))

    def on_provisional(self, msg):
        if self.state in ("calling", "trying"):
            if msg.status in (180, 183):
                self._set("ringing")
            elif self.state == "calling":
                self._set("trying")

    # ----- call flow -----
    async def invite(self):
        self.started = time.time()
        self._open_rtp()
        self._set("calling")
        body = self.sdp()
        auth = None
        for attempt in range(2):
            cseq = self.cseq
            def build(br, cseq=cseq, auth=auth):
                return self._request("INVITE", br, cseq, body, auth)
            resp, branch = await self.ua.transact(build, "INVITE", final_timeout=32, invite=True)
            if resp is None:
                return self._fail("no answer (timeout)")
            if resp.status in (401, 407) and attempt == 0:
                self._ack(resp, branch)
                self.cseq += 1
                hdr = "www-authenticate" if resp.status == 401 else "proxy-authenticate"
                name = "Authorization" if resp.status == 401 else "Proxy-Authorization"
                auth = f"{name}: " + digest(resp.get(hdr), "INVITE", self.target_uri,
                                            self.cfg["auth_user"] or self.cfg["local_user"],
                                            self.cfg["auth_password"])
                continue
            if resp.status >= 300:
                self._ack(resp, branch)
                return self._fail(f"{resp.status} {resp.reason}")
            # 2xx
            self.remote_tag = _tag(resp.get("to"))
            self.remote_target = _uri(resp.get("contact")) or self.target_uri
            self.route = list(reversed(resp.all("record-route")))
            self._ack(resp)
            ip, port, pts, rtpmap = parse_sdp(resp.body)
            if not ip or not port:
                await self.hangup()
                return self._fail("answer has no usable SDP")
            if self.pt not in pts:
                alt = next((n for n, (pt, *_r) in CODECS.items() if pt in pts), None)
                if not alt:
                    await self.hangup()
                    return self._fail(f"no common codec (remote offered {pts})")
                self.codec_name = alt
                self.pt, self.enc, self.dec = CODECS[alt]
            self.dtmf_pt = next((p for p, n in rtpmap.items() if n == "TELEPHONE-EVENT"), None)
            self.remote_rtp = (ip, port)
            self.answered_at = time.time()
            self._set("connected")
            self._rtp_task = asyncio.create_task(self._rtp_loop())
            return True
        return self._fail("authentication failed")

    def prepare_inbound(self, req, addr):
        """Validate an inbound INVITE's offer and pick a codec."""
        self.call_id = req.get("call-id")
        self.peer_addr = addr
        self.remote_tag = _tag(req.get("from"))
        self.remote_from = req.get("from")
        self.local_to = req.get("to") + (f";tag={self.local_tag}" if "tag=" not in req.get("to", "") else "")
        self.remote_target = _uri(req.get("contact")) or None
        self.route = req.all("record-route")
        ip, port, pts, rtpmap = parse_sdp(req.body)
        if not ip or not port:
            return False, 488, "Not Acceptable Here"
        pref = [self.codec_name] + [n for n in CODECS if n != self.codec_name]
        choice = next((n for n in pref if CODECS[n][0] in pts), None)
        if not choice:
            return False, 488, "Not Acceptable Here"
        self.codec_name = choice
        self.pt, self.enc, self.dec = CODECS[choice]
        self.dtmf_pt = next((p for p, n in rtpmap.items() if n == "TELEPHONE-EVENT"), None)
        self.remote_rtp = (ip, port)
        try:
            self.cseq = int(req.cseq[0]) % 1000 + 1      # our own CSeq space for BYE/INFO
        except Exception:
            self.cseq = 1
        return True, 200, "OK"

    async def answer(self, req, addr):
        """Send 200 OK with SDP, retransmit until ACK, start media."""
        try:
            self._open_rtp()
        except RuntimeError as e:
            self.ua._reply(req, addr, 500, "Server Internal Error")
            return self._fail(str(e))
        self.started = time.time()
        sdp = self.sdp()
        self.answered_at = time.time()
        self._set("connected")
        self._rtp_task = asyncio.create_task(self._rtp_loop())
        interval, deadline = T1, time.monotonic() + 32
        while not self._acked.is_set() and time.monotonic() < deadline and self.state == "connected":
            self.ua._reply(req, addr, 200, "OK", sdp=sdp, contact=True)
            try:
                await asyncio.wait_for(self._acked.wait(), timeout=interval)
            except asyncio.TimeoutError:
                interval = min(interval * 2, 4.0)
        if not self._acked.is_set() and self.state == "connected":
            log.warning("no ACK for inbound call; hanging up")
            await self.hangup()

    def _got_ack(self):
        self._acked.set()

    def _fail(self, why):
        self._close_rtp()
        self._set("failed", why)
        self.ended.set()
        return False

    def queue_dtmf(self, digits):
        for d in str(digits):
            if d in "0123456789*#ABCD":
                self._dtmf_queue.append(d)

    async def send_info_dtmf(self, digits):
        for d in digits:
            body = f"Signal={d}\r\nDuration=160\r\n"
            self.cseq += 1
            cseq = self.cseq
            def build(br, cseq=cseq, body=body):
                t = self._request("INFO", br, cseq, body)
                return t.replace("Content-Length:", "Content-Type: application/dtmf-relay\r\nContent-Length:", 1)
            await self.ua.transact(build, "INFO", final_timeout=4, addr=self._next_hop())
            await asyncio.sleep(0.25)

    async def _rtp_loop(self):
        ptime = int(self.cfg["ptime_ms"]) / 1000
        spp = int(8000 * ptime)                       # samples per packet
        seq = random.randrange(65536)
        ts = random.randrange(1 << 32)
        ssrc = random.randrange(1 << 32)
        silence = self.enc(np.zeros(spp, dtype=np.int16)).tobytes()
        pending = np.zeros(0, dtype=np.int16)
        marker = True
        t0 = time.monotonic()
        n = 0
        sock, dst = self.rtp_sock, self.remote_rtp
        loop = asyncio.get_running_loop()
        try:
            while self.state == "connected":
                self._receive(sock)

                if self._dtmf_queue and self.dtmf_pt is not None and self.cfg["dtmf_mode"] == "rfc2833":
                    d = self._dtmf_queue.popleft()
                    ev = "0123456789*#ABCD".index(d)
                    dur_step, total = spp, 8000 * 160 // 1000          # 160 ms tone
                    dur = 0
                    first = True
                    while dur < total:
                        dur = min(dur + dur_step, total)
                        payload = struct.pack("!BBH", ev, 10, dur)     # vol -10 dBm0
                        hdr = struct.pack("!BBHII", 0x80, (0x80 if first else 0) | self.dtmf_pt, seq, ts, ssrc)
                        sock.sendto(hdr + payload, dst)
                        seq = (seq + 1) & 0xFFFF
                        first = False
                        n += 1
                        await asyncio.sleep(max(0, t0 + n * ptime - time.monotonic()))
                    for _ in range(3):                                  # end packets
                        payload = struct.pack("!BBH", ev, 0x80 | 10, total)
                        sock.sendto(struct.pack("!BBHII", 0x80, self.dtmf_pt, seq, ts, ssrc) + payload, dst)
                        seq = (seq + 1) & 0xFFFF
                    ts = (ts + total + spp) & 0xFFFFFFFF
                    n += 1
                    await asyncio.sleep(max(0, t0 + n * ptime - time.monotonic()))
                    await asyncio.sleep(0.1)
                    t0 = time.monotonic() - n * ptime
                    marker = True
                    continue

                while len(pending) < spp and self.frames:
                    pending = np.concatenate([pending, self.frames.popleft()])
                if len(pending) >= spp:
                    chunk, pending = pending[:spp], pending[spp:]
                    if self._gain != 1.0:
                        chunk = np.clip(chunk.astype(np.float32) * self._gain, -32768, 32767).astype(np.int16)
                    payload = self.enc(chunk).tobytes()
                else:
                    payload = silence
                hdr = struct.pack("!BBHII", 0x80, (0x80 if marker else 0) | self.pt, seq, ts, ssrc)
                marker = False
                try:
                    sock.sendto(hdr + payload, dst)
                    self.packets_sent += 1
                except OSError as e:
                    log.warning("rtp send: %s", e)
                seq = (seq + 1) & 0xFFFF
                ts = (ts + spp) & 0xFFFFFFFF
                n += 1
                delay = t0 + n * ptime - time.monotonic()
                if delay < -0.2:                    # fell badly behind: resync clock
                    t0 = time.monotonic() - n * ptime
                    delay = 0
                await asyncio.sleep(max(0, delay))
        except asyncio.CancelledError:
            pass

    def _receive(self, sock):
        """Read every waiting RTP packet; decode audio into rx_frames."""
        try:
            while True:
                pkt = sock.recv(2048)
                if len(pkt) < 12 or (pkt[0] >> 6) != 2:
                    continue
                cc = pkt[0] & 0x0F
                off = 12 + 4 * cc
                if pkt[0] & 0x10 and len(pkt) >= off + 4:          # header extension
                    off += 4 + 4 * struct.unpack("!H", pkt[off + 2:off + 4])[0]
                payload = pkt[off:]
                if pkt[0] & 0x20 and payload:                     # padding
                    payload = payload[:-payload[-1]]
                pt = pkt[1] & 0x7F
                self.rx_packets += 1
                if pt == self.pt and payload:
                    self.rx_frames.append(self.dec(payload))
                    self.last_rx = time.time()
        except (BlockingIOError, OSError):
            pass

    def buffered_ms(self):
        return sum(len(f) for f in list(self.frames)) / 8

    async def hangup(self):
        if self.state != "connected":
            return
        self._set("ending")
        if self._rtp_task:
            self._rtp_task.cancel()
        self.cseq += 1
        cseq = self.cseq
        resp, _ = await self.ua.transact(lambda br: self._request("BYE", br, cseq), "BYE", final_timeout=8,
                                         addr=self._next_hop())
        self._close_rtp()
        self._set("ended", None if resp is not None else "no reply to BYE")
        self.ended.set()

    def _remote_bye(self):
        if self._rtp_task:
            self._rtp_task.cancel()
        self._close_rtp()
        self._set("ended", "far end hung up")
        self.ended.set()

    def _close_rtp(self):
        if self.rtp_sock:
            self.rtp_sock.close()
            self.rtp_sock = None
