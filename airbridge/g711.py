"""Vectorised G.711 mu-law / A-law codecs (audioop is gone in Python 3.13)."""
import numpy as np

_BIAS = 0x84
_CLIP = 32635
_SEG_AEND = np.array([0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF])


def ulaw_encode(pcm):
    x = pcm.astype(np.int32)
    sign = np.where(x < 0, 0x80, 0)
    x = np.minimum(np.abs(x), _CLIP) + _BIAS
    exp = np.floor(np.log2(x)).astype(np.int32) - 7
    exp = np.clip(exp, 0, 7)
    mant = (x >> (exp + 3)) & 0x0F
    return (~(sign | (exp << 4) | mant) & 0xFF).astype(np.uint8)


def ulaw_decode(data):
    u = ~np.frombuffer(bytes(data), dtype=np.uint8).astype(np.int32) & 0xFF
    t = ((u & 0x0F) << 3) + _BIAS
    t = t << ((u & 0x70) >> 4)
    return np.where(u & 0x80, _BIAS - t, t - _BIAS).astype(np.int16)


def alaw_encode(pcm):
    x = pcm.astype(np.int32) >> 3
    mask = np.where(x >= 0, 0xD5, 0x55)
    x = np.where(x >= 0, x, -x - 1)
    seg = np.searchsorted(_SEG_AEND, x)            # first seg with x <= end
    aval = np.where(seg < 2, (x >> 1) & 0x0F, (x >> np.maximum(seg, 1)) & 0x0F) | (np.minimum(seg, 7) << 4)
    aval = np.where(seg >= 8, 0x7F, aval)
    return ((aval ^ mask) & 0xFF).astype(np.uint8)


def alaw_decode(data):
    a = np.frombuffer(bytes(data), dtype=np.uint8).astype(np.int32) ^ 0x55
    t = (a & 0x0F) << 4
    seg = (a & 0x70) >> 4
    t = np.where(seg == 0, t + 8, t + 0x108)
    t = np.where(seg > 1, t << np.maximum(seg - 1, 0), t)
    return np.where(a & 0x80, t, -t).astype(np.int16)


CODECS = {
    "PCMU": (0, ulaw_encode, ulaw_decode),
    "PCMA": (8, alaw_encode, alaw_decode),
}
