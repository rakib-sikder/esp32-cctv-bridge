#!/usr/bin/env python3
"""
V380 / macro-video encrypted IOT channel (port 8089) — interop for your OWN camera.

Reproduces the camera's LAN control channel used for record-search and settings.
All standard crypto (no secret native cipher):
  - handshake wrapped with AES-128-CBC under a fixed 32-byte key split key||iv
  - X25519 ECDH for the per-session key
  - subsequent requests AES-128-CBC under the exchanged session key

Reconstructed from LoginHelper.keyExchangeByLanSync + Protocol.*.
"""
import hashlib
import json
import os
import socket
import struct
import time
import base64
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

CMD_COMMON_IOT = 939722776
METHOD_LANPASS_REQ = 196870
METHOD_RECFILE_REQ = 197152
IOT_PORT = 8089

# Fixed 32-byte handshake key (LoginHelper.i); used as key[0:16] + iv[16:32].
STATIC_KEY = bytes([54, 58, 229, 35, 61, 56, 207, 132, 150, 154, 99, 78, 159, 150,
                    106, 152, 181, 115, 209, 255, 233, 55, 178, 119, 209, 241, 214,
                    43, 56, 116, 138, 229])

RANDOM_CHARSET = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789")


def _rand_ascii(n):
    return "".join(RANDOM_CHARSET[b % len(RANDOM_CHARSET)] for b in os.urandom(n)).encode()


def aes_cbc_enc(key32, data):
    p = PKCS7(128).padder()
    data = p.update(data) + p.finalize()
    e = Cipher(algorithms.AES(key32[:16]), modes.CBC(key32[16:32])).encryptor()
    return e.update(data) + e.finalize()


def aes_cbc_dec(key32, data):
    d = Cipher(algorithms.AES(key32[:16]), modes.CBC(key32[16:32])).decryptor()
    raw = d.update(data) + d.finalize()
    try:
        u = PKCS7(128).unpadder()
        raw = u.update(raw) + u.finalize()
    except ValueError:
        pass
    return raw


def build_frame(method_id, params, key32, magic, sid=0, req_id=None):
    """16-byte header + AES-CBC(body). body = {id,method_id,chn,ts,params}."""
    if req_id is None:
        req_id = int.from_bytes(os.urandom(2), "big") + 10
    body = {"id": req_id, "method_id": method_id, "chn": 1,
            "ts": int(time.time()), "params": params}
    enc = aes_cbc_enc(key32, json.dumps(body, separators=(",", ":")).encode())
    hdr = bytearray(16)
    struct.pack_into("<i", hdr, 0, CMD_COMMON_IOT)
    hdr[4] = 1          # version
    hdr[5] = 2          # secureType = 2 (AES-CBC)
    hdr[6] = sid & 0xFF
    hdr[7] = magic & 0xFF
    struct.pack_into("<i", hdr, 12, len(enc))
    return bytes(hdr) + enc, req_id


def read_frame(sock, key32, timeout=8):
    sock.settimeout(timeout)
    hdr = b""
    while len(hdr) < 16:
        c = sock.recv(16 - len(hdr))
        if not c:
            raise IOError("no header")
        hdr += c
    cmd = struct.unpack_from("<i", hdr, 0)[0]
    secure = hdr[5]
    dsize = struct.unpack_from("<i", hdr, 12)[0]
    body = b""
    while len(body) < dsize:
        c = sock.recv(dsize - len(body))
        if not c:
            break
        body += c
    if secure == 2:
        txt = aes_cbc_dec(key32, body).decode("utf-8", "replace")
    else:
        txt = body.decode("utf-8", "replace")
    return cmd, secure, txt


def key_exchange(ip, dev_password, port=IOT_PORT, verbose=False):
    """X25519 handshake -> (sKey, sid). dev_password = plaintext device password."""
    priv = X25519PrivateKey.generate()
    pub = priv.public_key().public_bytes_raw()        # 32 bytes
    magic = (os.urandom(1)[0] % 127) + 1
    pub_xor = bytes(pub[i] ^ magic for i in range(len(pub)))
    cKM = base64.b64encode(pub_xor).decode()
    cS = base64.b64encode(hashlib.sha256(pub_xor + dev_password.encode()).digest()).decode()
    tz = _tz_offset()
    params = {"act": 1, "verify": {"cKM": cKM, "cS": cS, "tz": tz, "ts": int(time.time())}}
    frame, rid = build_frame(METHOD_LANPASS_REQ, params, STATIC_KEY, magic, sid=0)

    s = socket.socket(); s.settimeout(8); s.connect((ip, port))
    s.sendall(frame)
    cmd, secure, txt = read_frame(s, STATIC_KEY)
    s.close()
    if verbose:
        print(f"[kex] resp cmd={cmd} secure={secure} json={txt[:400]}")
    data = json.loads(txt)
    code = (data.get("result") or {}).get("code")
    if code != 1000:
        return None, None, data
    verify = data["data"]["verify"]
    dK = base64.b64decode(verify["dK"])               # camera public key
    shared = priv.exchange(X25519PublicKey.from_public_bytes(dK))   # 32-byte sKey
    sid = verify.get("sid", 0)
    return shared, sid, data


def _tz_offset():
    off = -time.timezone                               # seconds east of UTC
    h = off // 3600
    return h * 100 + (off - 3600 * h) // 60


if __name__ == "__main__":
    import sys
    ip = sys.argv[1] if len(sys.argv) > 1 else "192.168.1.100"
    if len(sys.argv) < 3:
        print("usage: python v380_iot.py <camera_ip> <device_plaintext_password>"); sys.exit(1)
    pw = sys.argv[2]
    skey, sid, data = key_exchange(ip, pw, verbose=True)
    if skey:
        print(f"\nKEY EXCHANGE OK  sid={sid}  sKey={skey.hex()}")
    else:
        print(f"\nkey exchange failed: {json.dumps(data)[:300]}")
