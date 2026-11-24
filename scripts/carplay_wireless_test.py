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
  3. wireless audio: /info offers AAC-LC on stream 102 (media) and Opus for the rest; a 440 Hz tone as AAC-LC (102,
     48 kHz stereo) and as Opus (100 telephony, 48 kHz mono), made with ffmpeg, comes out decoded (--audio-dump WAVs).
Only checks nbt_carplay against itself and xcertplay's description: the iPhone is the real test.

  python3 scripts/carplay_wireless_test.py AUDIO_DIR   (test_airplay_host's audio dump folder; needs ffmpeg)
"""
import glob
import os
import plistlib
import subprocess
import tempfile
import time
import socket
import struct
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from carplay_test import Cipher2, Rtsp, check, hkdf, pair  # noqa: E402
from cryptography.hazmat.primitives.ciphers.aead import ChaCha20Poly1305  # noqa: E402

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
    audio(r, shared)
    data.close()
    ev.close()
    s.close()


def tone(codec, rate, channels, fmt):
    """0.5 s of 440 Hz with ffmpeg, in the given container"""
    out = tempfile.mktemp(suffix="." + fmt)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=%d" % rate,
                    "-t", "0.5", "-ac", str(channels), "-c:a", codec, "-b:a", "64k", out], check=True)
    data = open(out, "rb").read()
    os.unlink(out)
    return data


def adts_frames(b):
    out = []
    while len(b) >= 7 and b[0] == 0xff and b[1] & 0xf0 == 0xf0:
        head = 7 if b[1] & 1 else 9
        n = ((b[3] & 3) << 11) | (b[4] << 3) | (b[5] >> 5)
        out.append(b[head:n])
        b = b[n:]
    return out


def ogg_packets(b):
    """the packets of an Ogg stream (the two Opus header packets dropped)"""
    packets, cur = [], b""
    while b[:4] == b"OggS":
        nseg = b[26]
        lace = b[27:27 + nseg]
        body = b[27 + nseg:]
        at = 0
        for v in lace:
            cur += body[at:at + v]
            at += v
            if v < 255:
                packets.append(cur)
                cur = b""
        b = body[at:]
    return packets[2:]


def send_rtp(port, key, packets, samples, pt):
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    for i, p in enumerate(packets):
        rtp = bytes([0x80, pt]) + struct.pack(">HII", i, 1000 + samples * i, 0x1234)
        nonce8 = struct.pack("<Q", i)
        sealed = ChaCha20Poly1305(key).encrypt(b"\0" * 4 + nonce8, p, rtp[4:12])
        s.sendto(rtp + sealed + nonce8, ("127.0.0.1", port))
        time.sleep(0.002)
    s.close()


def wav_tone(path):
    """(rate, channels, frames, frequency of the first channel by zero crossings)"""
    b = open(path, "rb").read()
    channels, rate = struct.unpack("<HI", b[22:28])
    pcm = struct.unpack("<%dh" % ((len(b) - 44) // 2), b[44:44 + (len(b) - 44) // 2 * 2])
    first = pcm[::channels]
    loud = [i for i, v in enumerate(first) if abs(v) > 1000]
    if not loud:
        return rate, channels, len(first), 0
    part = first[loud[0]:loud[-1]]
    crossings = sum(1 for a, c in zip(part, part[1:]) if a < 0 <= c)
    return rate, channels, len(first), crossings * rate / max(1, len(part))


def audio(r, shared):
    status, body = r.request("GET", "/info")
    formats = {(f["type"], f["audioType"]): f for f in plistlib.loads(body)["audioFormats"]}
    check("wireless /info: AAC-LC 44.1 / 48 kHz on 102 media, Opus for telephony / Siri / navigation",
          formats[(102, "media")]["audioOutputFormats"] == 0xc00000 and
          formats[(100, "telephony")]["audioOutputFormats"] == 0x70000000 and
          formats[(100, "speechRecognition")]["audioOutputFormats"] == 0x60000000 and
          formats[(101, "default")]["audioOutputFormats"] == 0x40000000)
    for f in glob.glob(os.path.join(AUDIO_DIR, "*.wav")):
        os.unlink(f)
    c_aac, c_opus = 11, 22
    status, body = r.request("SETUP", "/1", plistlib.dumps({"streams": [
        {"type": 102, "audioType": "media", "audioFormat": 0x800000, "streamConnectionID": c_aac},
        {"type": 100, "audioType": "telephony", "audioFormat": 0x40000000, "streamConnectionID": c_opus}]},
        fmt=plistlib.FMT_BINARY))
    streams = {x["type"]: x for x in plistlib.loads(body)["streams"]}
    check("wireless audio: SETUP of AAC-LC (102) and Opus (100) taken %s" % sorted(streams), sorted(streams) == [100, 102])
    aac = adts_frames(tone("aac", 48000, 2, "aac"))
    opus = ogg_packets(tone("libopus", 48000, 1, "ogg"))
    send_rtp(streams[102]["dataPort"], hkdf(shared, "DataStream-Salt%d" % c_aac, "DataStream-Output-Encryption-Key"),
             aac, 1024, 0x60)
    send_rtp(streams[100]["dataPort"], hkdf(shared, "DataStream-Salt%d" % c_opus, "DataStream-Output-Encryption-Key"),
             opus, 960, 0x60)
    time.sleep(0.5)
    r.request("TEARDOWN", "/1", plistlib.dumps({"streams": [{"type": 102}, {"type": 100}]}, fmt=plistlib.FMT_BINARY))
    time.sleep(1)
    wavs = sorted(glob.glob(os.path.join(AUDIO_DIR, "*.wav")))
    got = {os.path.basename(w).split("-")[1]: wav_tone(w) for w in wavs}
    print("    decoded:", got)
    a, o = got.get("102"), got.get("100")
    check("wireless audio: AAC-LC (%d frames) decoded to 48 kHz stereo 440 Hz" % len(aac),
          a and a[0] == 48000 and a[1] == 2 and a[2] >= 1024 * (len(aac) - 2) and abs(a[3] - 440) < 15)
    check("wireless audio: Opus (%d packets) decoded to 48 kHz mono 440 Hz" % len(opus),
          o and o[0] == 48000 and o[1] == 1 and o[2] >= 960 * (len(opus) - 2) and abs(o[3] - 440) < 15)


AUDIO_DIR = sys.argv[1] if len(sys.argv) > 1 else "audio"


def main():
    bluetooth()
    tunnel()
    print("ALL OK")


if __name__ == "__main__":
    main()
