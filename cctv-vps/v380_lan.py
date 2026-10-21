#!/usr/bin/env python3
"""
V380 / macro-video LAN control client (port 8800) — interop for your OWN camera.

Implements the documented V380 LAN login (standard double AES-128-ECB, no native
code), the same handshake the open-source xiaovv_decoder / prsyahmi projects use.
Goal: log in over the LAN and then search the camera's own SD recordings
(including motion / human / PIR events), which the camera records itself.

This file currently does the LOGIN and prints the result, to verify the control
channel and whether a password is required. Record-search is added next.

    python v380_lan.py --ip 192.168.68.51 --did <DEVICE_ID> --user admin --password ""
"""
import argparse
import socket
import struct
import time
from datetime import datetime
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

STATIC_KEY = b"macrovideo+*#!^@"          # documented V380 static AES key
LOGIN_BUF = 520                           # LOGIN_COMMUNICATION_BUFFER_SIZE
CMD_LOGIN = 31167
CMD_LOGIN_RESPONSE = 31168
# Alternate command pair some firmwares use for LAN login:
CMD_LOGIN_ALT = 1167
CMD_LOGIN_RESPONSE_ALT = 1168
RANDOM_CHARSET = ("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
                  "1234567890!@#$%^&*()_+-=")


def _aes_ecb(key, data):
    p = PKCS7(128).padder()
    data = p.update(data) + p.finalize()
    e = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return e.update(data) + e.finalize()


def _random_key(n=16):
    import secrets
    return "".join(secrets.choice(RANDOM_CHARSET) for _ in range(n)).encode("ascii")


def build_login(did, user, password, cmd=CMD_LOGIN):
    buf = bytearray(LOGIN_BUF)
    struct.pack_into("<i", buf, 0, cmd)          # command
    struct.pack_into("<i", buf, 4, 120)          # unknown1
    buf[8] = 31                                   # unknown2
    struct.pack_into("<i", buf, 9, 10)           # unknown3
    struct.pack_into("<i", buf, 13, did & 0xFFFFFFFF)   # device id
    date = datetime.now().strftime("%Y-%m-%d %H:%M:%S").encode("ascii")
    buf[17:17 + len(date)] = date                # date (32)
    u = user.encode("ascii")[:31]
    buf[49:49 + len(u)] = u                       # username (32)
    # auth zone at 81: randomKey(16) + AES(randomKey, AES(STATIC_KEY, password))(32)
    rk = _random_key(16)
    first = _aes_ecb(STATIC_KEY, password.encode("utf-8"))
    second = _aes_ecb(rk, first)
    buf[81:81 + 16] = rk
    buf[97:97 + len(second)] = second
    return bytes(buf)


def login(ip, port, did, user, password, timeout=4.0, verbose=False):
    results = {}
    for cmd, resp_cmd in ((CMD_LOGIN, CMD_LOGIN_RESPONSE), (CMD_LOGIN_ALT, CMD_LOGIN_RESPONSE_ALT)):
        try:
            s = socket.socket(); s.settimeout(timeout); s.connect((ip, port))
            s.sendall(build_login(did, user, password, cmd=cmd))
            # read up to 256 bytes response
            data = b""
            s.settimeout(timeout)
            while len(data) < 256:
                chunk = s.recv(256 - len(data))
                if not chunk:
                    break
                data += chunk
            s.close()
            if len(data) >= 12:
                rcmd = struct.unpack_from("<i", data, 0)[0]
                lresult = struct.unpack_from("<i", data, 4)[0]
                rvalue = struct.unpack_from("<i", data, 8)[0]
                verdict = rvalue - (rvalue // 100) * 100   # last two decimal digits
                results[cmd] = dict(resp_cmd=rcmd, loginResult=lresult,
                                    resultValue=rvalue, verdict=verdict,
                                    raw=data[:32].hex(), nbytes=len(data))
                if verbose:
                    print(f"[login cmd={cmd}] resp_cmd={rcmd} loginResult={lresult} "
                          f"resultValue={rvalue} verdict={verdict} bytes={len(data)}")
            else:
                results[cmd] = dict(error="short/no response", nbytes=len(data),
                                    raw=data.hex())
                if verbose:
                    print(f"[login cmd={cmd}] short response ({len(data)} bytes): {data.hex()}")
        except Exception as e:
            results[cmd] = dict(error=f"{type(e).__name__}: {e}")
            if verbose:
                print(f"[login cmd={cmd}] {type(e).__name__}: {e}")
    return results


def main():
    ap = argparse.ArgumentParser(description="V380 LAN login probe")
    ap.add_argument("--ip", required=True)
    ap.add_argument("--port", type=int, default=8800)
    ap.add_argument("--did", required=True, type=int)
    ap.add_argument("--user", default="admin")
    ap.add_argument("--password", default="")
    ap.add_argument("-v", "--verbose", action="store_true", default=True)
    args = ap.parse_args()
    r = login(args.ip, args.port, args.did, args.user, args.password, verbose=args.verbose)
    print("\nSUMMARY:")
    for cmd, res in r.items():
        print(f"  cmd {cmd}: {res}")


if __name__ == "__main__":
    main()
