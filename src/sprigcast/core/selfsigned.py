"""A self-signed certificate for SprigCast's own page on your home network (the Phone Camera), made here.

Phones let a page use the camera only over a secure connection, so the page a phone opens is served over HTTPS
with this certificate. No authority signed it, so the phone warns once ("not private") and you go on. It's made
once (an ECDSA P-256 key, standard library only: no library to ship) and kept with SprigCast's settings.
"""

from __future__ import annotations

import base64
import hashlib
import ipaddress
import secrets
from datetime import datetime, timedelta, timezone

# NIST P-256
_P = 0xFFFFFFFF00000001000000000000000000000000FFFFFFFFFFFFFFFFFFFFFFFF
_A = _P - 3
_N = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551
_G = (0x6B17D1F2E12C4247F8BCE6E563A440F277037D812DEB33A0F4A13945D898C296,
      0x4FE342E2FE1A7F9B8EE7EB4A7C0F9E162BCE33576B315ECECBB6406837BF51F5)


def _add(p, q):
    if p is None:
        return q
    if q is None:
        return p
    if p[0] == q[0] and (p[1] + q[1]) % _P == 0:
        return None
    if p == q:
        s = (3 * p[0] * p[0] + _A) * pow(2 * p[1], -1, _P) % _P
    else:
        s = (q[1] - p[1]) * pow(q[0] - p[0], -1, _P) % _P
    x = (s * s - p[0] - q[0]) % _P
    return x, (s * (p[0] - x) - p[1]) % _P


def _times(k: int, point):
    out = None
    while k:
        if k & 1:
            out = _add(out, point)
        point = _add(point, point)
        k >>= 1
    return out


def _sign(d: int, message: bytes) -> tuple[int, int]:
    z = int.from_bytes(hashlib.sha256(message).digest(), "big")
    while True:
        k = secrets.randbelow(_N - 1) + 1
        r = _times(k, _G)[0] % _N
        if r == 0:
            continue
        s = pow(k, -1, _N) * (z + r * d) % _N
        if s:
            return r, s


# DER, just what a certificate needs
def _len(n: int) -> bytes:
    if n < 0x80:
        return bytes([n])
    body = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(body)]) + body


def _tlv(tag: int, body: bytes) -> bytes:
    return bytes([tag]) + _len(len(body)) + body


def _seq(*parts: bytes) -> bytes:
    return _tlv(0x30, b"".join(parts))


def _int(value: int) -> bytes:
    body = value.to_bytes(value.bit_length() // 8 + 1, "big")
    return _tlv(0x02, body)


def _oid(dotted: str) -> bytes:
    nums = [int(x) for x in dotted.split(".")]
    body = bytearray([40 * nums[0] + nums[1]])
    for n in nums[2:]:
        chunk = [n & 0x7F]
        n >>= 7
        while n:
            chunk.append(0x80 | (n & 0x7F))
            n >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, bytes(body))


def _bits(body: bytes) -> bytes:
    return _tlv(0x03, b"\x00" + body)


def _time(when: datetime) -> bytes:
    return _tlv(0x17, when.strftime("%y%m%d%H%M%SZ").encode())


def _name(common: str) -> bytes:
    return _seq(_tlv(0x31, _seq(_oid("2.5.4.3"), _tlv(0x0C, common.encode()))))


_EC_KEY, _P256, _ECDSA_SHA256 = "1.2.840.10045.2.1", "1.2.840.10045.3.1.7", "1.2.840.10045.4.3.2"


def _pem(kind: str, der: bytes) -> str:
    body = base64.b64encode(der).decode()
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return f"-----BEGIN {kind}-----\n" + "\n".join(lines) + f"\n-----END {kind}-----\n"


def make(addresses: list[str], name: str = "SprigCast", days: int = 397,
         now: datetime | None = None) -> tuple[str, str]:
    """(certificate PEM, private key PEM) for these IP addresses (and sprigcast.local)."""
    now = now or datetime.now(timezone.utc)
    d = secrets.randbelow(_N - 1) + 1
    qx, qy = _times(d, _G)
    point = b"\x04" + qx.to_bytes(32, "big") + qy.to_bytes(32, "big")
    alt = [_tlv(0x82, b"sprigcast.local")]
    for text in addresses:
        try:
            alt.append(_tlv(0x87, ipaddress.ip_address(text).packed))
        except ValueError:
            continue
    extensions = _tlv(0xA3, _seq(
        _seq(_oid("2.5.29.17"), _tlv(0x04, _seq(*alt))),  # who it's for
        _seq(_oid("2.5.29.19"), _tlv(0x04, _seq())),  # not an authority
    ))
    algorithm = _seq(_oid(_ECDSA_SHA256))
    tbs = _seq(
        _tlv(0xA0, _int(2)),  # version 3
        _int(secrets.randbits(63) | 1),
        algorithm,
        _name(name),
        _seq(_time(now - timedelta(days=1)), _time(now + timedelta(days=days))),
        _name(name),
        _seq(_seq(_oid(_EC_KEY), _oid(_P256)), _bits(point)),
        extensions,
    )
    r, s = _sign(d, tbs)
    cert = _seq(tbs, algorithm, _bits(_seq(_int(r), _int(s))))
    key = _seq(_int(1), _tlv(0x04, d.to_bytes(32, "big")), _tlv(0xA0, _oid(_P256)), _tlv(0xA1, _bits(point)))
    return _pem("CERTIFICATE", cert), _pem("EC PRIVATE KEY", key)
