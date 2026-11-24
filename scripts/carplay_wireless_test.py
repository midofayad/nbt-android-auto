"""Host test of nbt_carplay's wireless parts: plays the iPhone against "test_airplay_host ... wireless".

SPDX-License-Identifier: GPL-3.0-only
Copyright (C) 2026 Valentin Radu

  1. iAP2 over Bluetooth (as the bridge board's relay hands it over, 127.0.0.1:7100): our marker first, the SYN only
     after the iPhone's marker, with acknowledgements on; every packet of the iPhone acknowledged; never more packets
     outstanding than the iPhone's window; wireless identification (Bluetooth and wireless CarPlay transports, no USB,
     no power); no PowerSourceUpdate; the Wi-Fi (5703) for 5702 and after 4e0e; CarPlayStartSession with the Wi-Fi.
  2. the iAP2 tunnel (AirPlay stream 130): after pairing, SETUP of the stream gives a data port; our iAP2 comes as
     iAPSendMessage commands on the event connection (marker, SYN without acknowledgements), the iPhone's goes into the
     data port as encrypted packages of type "comm"; the identification over it is the wireless one.
Only checks nbt_carplay against itself and xcertplay's description: the iPhone is the real test.

  python3 scripts/carplay_wireless_test.py
"""
import os
import plistlib
import socket
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from carplay_test import Cipher2, Rtsp, check, hkdf, pair  # noqa: E402

MARKER = bytes([0xff, 0x55, 0x02, 0x00, 0xee, 0x10])
IAP_UUID = "E9459FD0-BCAD-4C45-820F-1E72447EF2F2"


def csum(b):
    return (-sum(b)) & 0xff


def packet(control, seq, ack, session=0, payload=b""):
    n = 9 + (len(payload) + 1 if payload else 0)
    head = bytes([0xff, 0x5a]) + struct.pack(">H", n) + bytes([control, seq, ack, session])
    head += bytes([csum(head)])
    return head + (payload + bytes([csum(payload)]) if payload else b"")


def message(mid, params=b""):
    return b"\x40\x40" + struct.pack(">HH", 6 + len(params), mid) + params


def prm(pid, data=b""):
    return struct.pack(">HH", 4 + len(data), pid) + data


def params(b):
    out = {}
    while len(b) >= 4:
        n, pid = struct.unpack(">HH", b[:4])
        out.setdefault(pid, []).append(b[4:n])
        b = b[n:]
    return out


def syn(window, retransmit, ack_timeout, retries, acks):
    return bytes([1, window]) + struct.pack(">HHH", 0xffff, retransmit, ack_timeout) + bytes([retries, acks]) + \
        bytes([10, 0, 2, 11, 2, 1, 12, 1, 2])


class Link:
    """The iPhone's end of an iAP2 link over a byte source (recv() -> bytes, b"" when nothing came)."""

    def __init__(self, recv, send):
        self.recv, self.send, self.buf, self.msgs = recv, send, b"", b""

    def next(self, tries=30):
        """("marker",) or (control, seq, ack, session, payload); None when nothing came"""
        for _ in range(tries):
            while self.buf and not (self.buf.startswith(b"\xff\x5a") or self.buf.startswith(b"\xff\x55")):
                self.buf = self.buf[1:]
            if self.buf.startswith(b"\xff\x55") and len(self.buf) >= 6:
                self.buf = self.buf[6:]
                return ("marker",)
            if self.buf.startswith(b"\xff\x5a") and len(self.buf) >= 9:
                n = struct.unpack(">H", self.buf[2:4])[0]
                if len(self.buf) >= n:
                    p, self.buf = self.buf[:n], self.buf[n:]
                    assert csum(p[:8]) == p[8]
                    return (p[4], p[5], p[6], p[7], p[9:n - 1] if n > 9 else b"")
            self.buf += self.recv()
        return None

    def message(self, want, on_packet=None):
        """the next control message (id, params) of id want, others skipped; on_packet(pkt) for each data packet"""
        while True:
            while len(self.msgs) >= 6:
                n, mid = struct.unpack(">HH", self.msgs[2:6])
                if len(self.msgs) < n:
                    break
                body, self.msgs = self.msgs[6:n], self.msgs[n:]
                if mid == want:
                    return mid, params(body)
            k = self.next()
            if k is None:
                raise TimeoutError("no %04x" % want)
            if k[0] != "marker" and k[4] and k[3] == 10:
                self.msgs += k[4]
                if on_packet:
                    on_packet(k)


def check_wireless_identification(prms, what):
    check(what + ": Bluetooth transport with the board's address, no USB",
          17 in prms and 16 not in prms and params(prms[17][0])[3][0] == bytes.fromhex("112233445566"))
    check(what + ": wireless CarPlay transport \"NBT-test\"", params(prms[24][0])[1][0] == b"NBT-test\0")
    sent = struct.unpack(">%dH" % (len(prms[6][0]) // 2), prms[6][0])
    taken = struct.unpack(">%dH" % (len(prms[7][0]) // 2), prms[7][0])
    check(what + ": no power, 5703 sent, 5702 / 4e0e / 4e0d taken, no ae03",
          prms[8][0] == b"\0" and 0x5703 in sent and 0xae03 not in sent and
          all(x in taken for x in (0x5702, 0x4e0e, 0x4e0d)))


def bluetooth():
    s = socket.create_connection(("127.0.0.1", 7100))
    s.settimeout(0.1)

    def recv():
        try:
            return s.recv(65536)
        except socket.timeout:
            return b""
    link = Link(recv, s.sendall)
    check("Bluetooth: the accessory's marker first", link.next() == ("marker",))
    check("Bluetooth: no SYN before our marker", link.next(tries=5) in (None, ("marker",)))
    s.sendall(MARKER)
    k = link.next()
    while k == ("marker",):
        k = link.next()
    check("Bluetooth: SYN after our marker, acknowledgements on (4000 ms, 500 ms, 4, 3)",
          k[0] == 0x80 and k[4][4:10] == bytes.fromhex("0fa001f40403"))
    acc_seq = k[1]
    seq = 40
    s.sendall(packet(0xc0, seq, acc_seq, 0, syn(2, 1500, 22, 3, 1)))
    k = link.next()
    check("Bluetooth: our SYN acknowledged", k[0] == 0x40 and k[2] == seq and not k[4])

    acked = [acc_seq]
    data = []

    def send(mid, prms=b""):
        nonlocal seq
        seq = (seq + 1) & 0xff
        s.sendall(packet(0x40, seq, acked[0], 10, message(mid, prms)))

    def track(k):
        data.append(k[1])
    send(0x1d00)
    _, idn = link.message(0x1d01, track)
    check_wireless_identification(idn, "Bluetooth identification")
    acked[0] = data[-1]
    s.sendall(packet(0x40, seq, acked[0]))
    send(0x1d02)
    send(0xaa00)
    _, m = link.message(0xaa01, track)
    check("Bluetooth: certificate", m[0][0] == b"test certificate")
    acked[0] = data[-1]
    send(0xaa02, prm(0, b"\x01" * 20))
    link.message(0xaa03, track)
    acked[0] = data[-1]
    before = len(data)
    send(0xaa05)
    for _ in range(40):   # gather what comes without our acknowledgement
        k = link.next(tries=3)
        if k is None:
            break
        if k[0] == 0x40 and k[4]:
            track(k)
            link.msgs += k[4]
    check("Bluetooth: no more than the window (2) outstanding without acknowledgements (%d sent)" %
          (len(data) - before), len(data) - before == 2)
    seen = []
    while True:
        acked[0] = data[-1]
        s.sendall(packet(0x40, seq, acked[0]))
        n = len(data)
        for _ in range(10):
            k = link.next(tries=3)
            if k is None:
                break
            if k[0] == 0x40 and k[4]:
                track(k)
                link.msgs += k[4]
        if len(data) == n:
            break
    while len(link.msgs) >= 6:
        n, mid = struct.unpack(">HH", link.msgs[2:6])
        seen.append(mid)
        link.msgs = link.msgs[n:]
    check("Bluetooth: control after authentication, no PowerSourceUpdate %s" % ["%04x" % x for x in seen],
          seen == [0x5000, 0x5200, 0xae00, 0x4157, 0x4154, 0x6800])
    send(0x5702)
    _, w = link.message(0x5703, track)
    check("Bluetooth: 5703 SSID, passphrase, security 2, channel 36",
          w[1][0] == b"NBT-test\0" and w[2][0] == b"secret123\0" and w[3][0] == b"\2" and w[4][0] == b"\x24")
    acked[0] = data[-1]
    send(0x4e0e, prm(0, b"aa:bb\0"))
    link.message(0x5703, track)
    check("Bluetooth: 5703 again after 4e0e", True)
    acked[0] = data[-1]
    send(0x4300, prm(1, prm(0, b"\1")))
    _, st = link.message(0x4301, track)
    g = params(st[1][0])
    check("Bluetooth: 4301 with the Wi-Fi group and the device identifier",
          0 not in st and g[0][0] == b"NBT-test\0" and g[1][0] == b"secret123\0" and g[2][0] == b"\x24" and
          g[3][0] == b"fe80::4e:42ff:fe54:1\0" and g[4][0] == b"\2" and st[3][0] == b"02:4e:42:54:00:09\0" and
          struct.unpack(">I", st[2][0])[0] == 7000)
    s.close()


def tunnel():
    s = socket.create_connection(("127.0.0.1", 7000))
    r = Rtsp(s)
    shared = pair(r)
    status, body = r.request("SETUP", "/1", plistlib.dumps({"name": "Test iPhone", "model": "iPhone16,2"},
                                                             fmt=plistlib.FMT_BINARY))
    st = plistlib.loads(body)
    ev = socket.create_connection(("127.0.0.1", st["eventPort"]))
    er = Rtsp(ev)
    er.cipher = Cipher2(hkdf(shared, "Events-Salt", "Events-Write-Encryption-Key"),
                        hkdf(shared, "Events-Salt", "Events-Read-Encryption-Key"))
    seed = 0xfedcba9876543210
    status, body = r.request("SETUP", "/1", plistlib.dumps({"streams": [
        {"type": 130, "clientTypeUUID": IAP_UUID, "seed": seed - (1 << 64), "streamConnectionID": 77}]},
        fmt=plistlib.FMT_BINARY))
    streams = plistlib.loads(body)["streams"]
    check("tunnel: SETUP of stream 130 gives a data port %s" % streams,
          len(streams) == 1 and streams[0]["type"] == 130 and streams[0]["dataPort"] > 0 and
          streams[0]["streamConnectionID"] == 77)
    data = socket.create_connection(("127.0.0.1", streams[0]["dataPort"]))
    out = Cipher2(b"\0" * 32, hkdf(shared, "DataStream-Salt%d" % seed, "DataStream-Output-Encryption-Key"))
    ev.settimeout(3)

    def recv():
        try:
            head, body = er.recv_message()
        except socket.timeout:
            return b""
        cmd = plistlib.loads(body)
        ev.sendall(er.cipher.seal(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\nContent-Length: 0\r\n\r\n"))
        assert cmd["type"] == "iAPSendMessage", cmd
        return cmd["params"]["data"]

    def to_accessory(b):
        pkg = struct.pack(">I", 32 + len(b)) + b"\0" * 12 + b"comm" + b"\0" * 12 + b
        data.sendall(out.seal(pkg))
    link = Link(recv, to_accessory)
    check("tunnel: the accessory's marker over iAPSendMessage", link.next() == ("marker",))
    k = link.next()
    check("tunnel: then its SYN, no acknowledgements", k[0] == 0x80 and k[4][4:10] == b"\0" * 6)
    to_accessory(MARKER + packet(0xc0, 7, k[1], 0, syn(4, 0, 0, 0, 0)) +
                 packet(0x40, 8, k[1], 10, message(0x1d00)))
    _, idn = link.message(0x1d01)
    check_wireless_identification(idn, "tunnel identification")
    data.close()
    ev.close()
    s.close()


def main():
    bluetooth()
    tunnel()
    print("ALL OK")


if __name__ == "__main__":
    main()
