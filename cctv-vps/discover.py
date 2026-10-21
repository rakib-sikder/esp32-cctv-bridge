#!/usr/bin/env python3
"""
LAN discovery for V380 / macro-video cameras.

Reimplements the broadcast probe the vendor app uses (DeviceLanUDPScanner):
the app sends the ASCII string "NVDEVSEARCH^100" to the subnet broadcast
address on UDP 10008, and each camera answers on UDP 10009 (the app's send
and receive sockets are on different ports) with a single "^"-delimited line
"NVDEVRESULT^100|200|300^...". We parse the fields Functions.ParseDevice reads.

A real reply, captured from the test camera, that this parser is validated on:
  NVDEVRESULT^200^<MAC>^<CAM_IP>^255.255.252.0^<GW>
             ^5040^5050^5051^8800^7050^5040^<DEVICE_ID>^0^1000
  -> mac=[2] reported_ip=[3] mask=[4] gw=[5] ctrl_port=[9]=8800
     device_id=[12]=<DEVICE_ID> config_id=[13]=0

Rebranded firmwares use a different 3-4 letter prefix (XVV / SFV / ICS ...)
but the exact same "^100 / ^RESULT" format, so we probe all known prefixes.

Pure stdlib. No login, no crypto — discovery is entirely plaintext.

Usage:
    python discover.py                 # scan ~2s, print a table
    python discover.py --timeout 4     # listen longer
    python discover.py --json          # machine-readable
"""
import argparse
import json
import socket
import struct
import sys
import time

SEND_PORT = 10008                  # Defines.DEFAULT_SEND_PORT (probe goes here)
RECV_PORT = 10009                  # Defines.DEFAULT_RECV_PORT (reply arrives here)
# prefix -> (search string, result prefix). From DeviceLanUDPScanner static block.
PROBES = {
    "NV":  ("NVDEVSEARCH^100",  "NVDEVRESULT"),
    "XVV": ("XVVSEARCH^100",    "XVVRESULT"),
    "SFV": ("SFVSEARCH^100",    "SFVRESULT"),
    "ICS": ("ICSSEARCH^100",    "ICSRESULT"),
}
RESULT_PREFIXES = tuple(v[1] for v in PROBES.values())


def _broadcast_addrs():
    """Best-effort list of broadcast targets: global + per-interface /24 guess."""
    addrs = {"255.255.255.255"}
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if ip.startswith("127."):
                continue
            addrs.add(ip.rsplit(".", 1)[0] + ".255")
    except OSError:
        pass
    return sorted(addrs)


def parse_result(line, src_ip):
    """Map a NVDEVRESULT^... line to a dict, mirroring Functions.ParseDevice.

    Field indices (0-based after split on '^'), validated against a real reply:
      2  = MAC (or name string on some firmware)
      3  = IP as the device reports it (we trust the UDP source IP more)
      4  = netmask        5 = gateway
      9  = control port (default 8800)
      12 = device ID      13 = config ID
    """
    f = line.split("^")

    def at(i, default=None, cast=str):
        if len(f) > i and f[i] != "":
            try:
                return cast(f[i])
            except (ValueError, TypeError):
                return default
        return default

    return {
        "ip": src_ip,                      # source of the datagram = real IP
        "reported_ip": at(3),
        "mac": at(2),
        "netmask": at(4),
        "gateway": at(5),
        "port": at(9, 8800, int),
        "device_id": at(12, None, int),
        "config_id": at(13, None, int),
        "prefix": f[0] if f else None,
        "raw": line,
    }


def discover(timeout=2.5, verbose=False):
    # Receive socket: the camera replies to UDP 10009, not to our source port.
    rx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    rx.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        rx.bind(("", RECV_PORT))
    except OSError as e:
        # 10009 busy (another scanner / the vendor app running). Fall back and
        # hope the camera also answers the source port on this firmware.
        if verbose:
            print(f"[discover] bind {RECV_PORT} failed ({e}); using ephemeral", file=sys.stderr)
        rx.bind(("", 0))
    rx.settimeout(0.5)

    # Send socket: broadcast the probe to UDP 10008.
    tx = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    tx.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    targets = _broadcast_addrs()
    if verbose:
        print(f"[discover] probe -> {targets} udp/{SEND_PORT}, listening udp/{RECV_PORT}",
              file=sys.stderr)

    def send_burst():
        for search, _ in PROBES.values():
            payload = search.encode("ascii")
            for bc in targets:
                try:
                    tx.sendto(payload, (bc, SEND_PORT))
                except OSError as e:
                    if verbose:
                        print(f"[discover] send to {bc} failed: {e}", file=sys.stderr)

    send_burst()
    found = {}
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            data, addr = rx.recvfrom(2048)
        except socket.timeout:
            send_burst()                   # re-probe in case the first burst was lost
            continue
        text = data.decode("ascii", "replace").strip("\x00").strip()
        if verbose:
            print(f"[discover] <- {addr[0]}: {text!r}", file=sys.stderr)
        if not text.startswith(RESULT_PREFIXES):
            continue                        # ignore our own probe echoes etc.
        dev = parse_result(text, addr[0])
        key = dev["device_id"] if dev["device_id"] is not None else addr[0]
        found[key] = dev                    # dedup repeated replies
    tx.close()
    rx.close()
    return list(found.values())


def main():
    ap = argparse.ArgumentParser(description="Find V380 / macro-video cameras on the LAN")
    ap.add_argument("--timeout", type=float, default=2.5, help="seconds to listen")
    ap.add_argument("--json", action="store_true", help="JSON output")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    devices = discover(timeout=args.timeout, verbose=args.verbose)

    if args.json:
        print(json.dumps(devices, indent=2))
        return 0 if devices else 1

    if not devices:
        print("No cameras found. Are you on the same LAN/subnet as the camera?")
        print("(Some APs block client-to-client broadcast; try a wired/same-AP link.)")
        return 1

    print(f"Found {len(devices)} camera(s):\n")
    print(f"  {'IP':<16} {'CTRL':<6} {'DEVICE ID':<12} {'MAC'}")
    print(f"  {'-'*16} {'-'*6} {'-'*12} {'-'*18}")
    for d in devices:
        print(f"  {d['ip']:<16} {str(d['port']):<6} "
              f"{str(d['device_id']):<12} {d.get('mac') or ''}")
        if d.get("gateway"):
            print(f"      net: mask {d.get('netmask')}  gw {d.get('gateway')}  "
                  f"cfg {d.get('config_id')}")
        print(f"      rtsp://{d['ip']}:554/live/ch00_0   (main, H.265)")
        print(f"      rtsp://{d['ip']}:554/live/ch00_1   (sub)")
        print(f"      onvif http://{d['ip']}:8899/onvif/device_service")
    return 0


if __name__ == "__main__":
    sys.exit(main())
