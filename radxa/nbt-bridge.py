#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Valentin Radu
"""nbt-bridge: the bridge board's part of wireless CarPlay for the NBT (docs/WIRELESS-BRIDGE.md).

The NBT does CarPlay itself (nbt_carplay --wireless: iAP2 with the car's authentication chip, the AirPlay receiver);
this board only connects it to the iPhone:
  - its USB gadget (nbt-gadget.sh) is the NBT's network link, bridged with its access point (nbt-net.sh)
  - Bluetooth: it opens the iAP2 RFCOMM channel of a paired iPhone and relays the bytes to nbt_carplay over TCP
    ([fe80::4e:42ff:fe54:1%br0]:7100: the NBT's link-local address, from the MAC the gadget gives the NBT)
  - mDNS (avahi): it publishes the NBT's AirPlay receiver (_airplay._tcp) and, when the iPhone appears on the access
    point (_carplay-ctrl._tcp), asks it to connect (GET /ctrl-int/1/connect), as xcertplay does
Relay protocol (text lines, then raw bytes): we send "nbt-bridge 1", bt, bssid, ssid, psk, channel, security, "end";
nbt_carplay answers deviceid, features, pk, pi, srcvers, model, name, address, port, "end"; when an iPhone's RFCOMM
channel is open we send "iap2 <address>" and relay. The TCP connection lasts one CarPlay session: nbt_carplay closes
it when the session is over, and we start again.
Pairing: the board is always discoverable ("BMW NBT", car audio class, from nbt-setup.sh) and bluetoothctl runs here
in a pseudo-terminal as the pairing agent: every pairing request is accepted and every newly paired device trusted, so
an iPhone pairs from its Bluetooth settings like with a car.
Needs: python3 (with Bluetooth sockets), bluez (bluetoothctl, sdptool), avahi-daemon and avahi-utils.
"""
import os
import pty
import re
import select
import socket
import subprocess
import threading
import time

CONF = "/etc/nbt-bridge.conf"
IAP2_PHONE_UUID = "00000000-deca-fade-deca-deafdecacafe"   # the iPhone's iAP2 RFCOMM service
MAC = r"[0-9A-Fa-f]{2}(?::[0-9A-Fa-f]{2}){5}"


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def conf():
    c = {"IFACE": "br0", "WLAN": "wlan0", "NBT": "fe80::4e:42ff:fe54:1", "PORT": "7100", "CHANNEL": "36",
         "SECURITY": "2", "SSID": "", "PSK": ""}
    try:
        with open(CONF) as f:
            for line in f:
                k, _, v = line.strip().partition("=")
                if k and not k.startswith("#"):
                    c[k.strip()] = v.strip()
    except OSError as e:
        log("no", CONF, "(run nbt-net.sh):", e)
    return c


def run(*cmd, timeout=30):
    try:
        return subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
                              timeout=timeout).stdout
    except (OSError, subprocess.TimeoutExpired) as e:
        log("could not run", cmd[0], e)
        return ""


def bt_address():
    m = re.search(r"Controller (%s)" % MAC, run("bluetoothctl", "show"))
    return m.group(1).upper() if m else None


def paired_phones():
    out = run("bluetoothctl", "devices", "Paired")
    if "Device" not in out:
        out = run("bluetoothctl", "paired-devices")
    return re.findall(r"Device (%s) (.*)" % MAC, out)


_channels = {}
_sdp = threading.Lock()


def iap2_channel(mac):
    """the RFCOMM channel of the iPhone's iAP2 service (SDP; "sdptool records" hangs on an iPhone, browse
    answers), cached"""
    with _sdp:
        if mac in _channels:
            return _channels[mac]
        for record in re.split(r"\n\s*\n", run("sdptool", "browse", mac, timeout=60)):
            m = re.search(r"Channel: (\d+)", record)
            if IAP2_PHONE_UUID in record.lower() and m:
                _channels[mac] = int(m.group(1))
                log("iPhone", mac, "iAP2 on RFCOMM channel", _channels[mac])
                break
        else:
            log("no iAP2 service found on", mac, "(not an iPhone, or out of reach)")
    return _channels.get(mac)


def rfcomm(mac, channel):
    s = socket.socket(socket.AF_BLUETOOTH, socket.SOCK_STREAM, socket.BTPROTO_RFCOMM)
    s.settimeout(15)
    try:
        s.connect((mac, channel))
    except OSError:
        s.close()
        raise
    s.settimeout(None)
    return s


class Agent(threading.Thread):
    """bluetoothctl in a pseudo-terminal: discoverable, pairable, every request accepted, new devices trusted"""
    SETUP = ["power on", "agent DisplayYesNo", "default-agent", "pairable on", "discoverable-timeout 0",
             "discoverable on"]

    def __init__(self):
        super().__init__(daemon=True)

    def send(self, fd, line):
        os.write(fd, (line + "\n").encode())

    def run(self):
        while True:
            try:
                pid, fd = pty.fork()
            except OSError as e:
                log("agent: no pseudo-terminal:", e)
                return
            if pid == 0:
                try:
                    os.execvp("bluetoothctl", ["bluetoothctl"])
                finally:
                    os._exit(127)
            time.sleep(1)
            for c in self.SETUP:
                self.send(fd, c)
                time.sleep(0.2)
            log("agent: discoverable and pairable")
            buf = ""
            while True:
                try:
                    data = os.read(fd, 4096).decode(errors="replace")
                except OSError:
                    break
                if not data:
                    break
                buf += re.sub(r"\x1b\[[0-9;?]*[A-Za-z]|[\x01\x02\r]", "", data)
                lines = buf.split("\n")
                buf = lines.pop()   # a prompt has no line end
                if "(yes/no)" in buf:
                    log("agent:", buf.strip(), "-> yes")
                    self.send(fd, "yes")
                    buf = ""
                for line in lines:
                    m = re.search(r"Device (%s) Paired: yes" % MAC, line)
                    if m:
                        mac = m.group(1).upper()
                        log("agent: paired with", mac, "- trusting it")
                        self.send(fd, "trust " + mac)
                        threading.Thread(target=iap2_channel, args=(mac,), daemon=True).start()
                    elif re.search(r"Device %s Connected: |\[NEW\] Device|Pairing|[Ff]ailed" % MAC, line):
                        log("agent:", line.strip())
            try:
                os.waitpid(pid, 0)
            except OSError:
                pass
            log("agent: bluetoothctl ended, again in 2 s")
            time.sleep(2)


class Mdns:
    """avahi-publish for the NBT's receiver; avahi-browse for the iPhone's _carplay-ctrl, which is asked to connect"""

    def __init__(self, info, iface):
        self.info, self.iface, self.procs, self.done, self.stop = info, iface, [], set(), False
        host = "NBT-CarPlay.local"
        f = info.get("features", "0x0,0x0")
        txt = ["deviceid=" + info["deviceid"], "features=" + f, "flags=0x4", "model=" + info.get("model", "NBT"),
               "srcvers=" + info["srcvers"], "protovers=1.1", "pi=" + info["pi"], "pk=" + info["pk"]]
        self.spawn(["avahi-publish", "-a", "-R", host, info["address"]])
        self.spawn(["avahi-publish", "-s", "-H", host, info.get("name", "BMW NBT"), "_airplay._tcp",
                    info.get("port", "7000")] + txt)
        self.browse = self.spawn(["avahi-browse", "-r", "-p", "-k", "_carplay-ctrl._tcp"], out=True)
        threading.Thread(target=self.watch, daemon=True).start()
        threading.Timer(3, self.check).start()

    def check(self):
        for p in self.procs:
            if p.poll() is not None and not self.stop:
                log("mDNS:", " ".join(p.args[:3]), "... ended with", p.returncode)

    def spawn(self, cmd, out=False):
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE if out else subprocess.DEVNULL,
                                 stderr=subprocess.DEVNULL, text=True)
        except OSError as e:
            log("could not run", cmd[0], e)
            return None
        self.procs.append(p)
        return p

    def watch(self):
        if not self.browse:
            return
        for line in self.browse.stdout:
            f = line.rstrip("\n").split(";")
            if self.stop:
                return
            if len(f) < 9 or f[0] != "=":
                continue
            name, addr, port = f[3], f[7], int(f[8])
            key = (name, addr)
            if key in self.done:
                continue
            log("iPhone on Wi-Fi:", name, addr, "port", port, "on", f[1])
            if self.connect_probe(addr, port, f[1]):
                self.done.add(key)

    def connect_probe(self, addr, port, iface):
        """GET /ctrl-int/1/connect: the iPhone then connects to the receiver whose deviceid we name"""
        dev = str(int(self.info["deviceid"].replace(":", ""), 16))
        for attempt in range(5):
            if self.stop:
                return False
            try:
                if ":" in addr:
                    s = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
                    s.settimeout(5)
                    s.connect((addr, port, 0, socket.if_nametoindex(iface)))
                    host = "[%s]:%d" % (addr, port)
                else:
                    s = socket.create_connection((addr, port), timeout=5)
                    host = "%s:%d" % (addr, port)
                req = ("GET /ctrl-int/1/connect HTTP/1.1\r\nHost: %s\r\nUser-Agent: AirPlay/%s\r\n"
                       "AirPlay-Receiver-Device-ID: %s\r\nConnection: close\r\n\r\n"
                       % (host, self.info["srcvers"], dev))
                s.sendall(req.encode())
                answer = s.recv(1024).decode(errors="replace").split("\r\n")[0]
                s.close()
                log("connect request to", host, "->", answer or "(nothing)")
                if answer:
                    return True
            except OSError as e:
                log("connect request to", addr, port, "attempt", attempt + 1, "failed:", e)
            time.sleep(1)
        return False

    def close(self):
        self.stop = True
        for p in self.procs:
            try:
                p.terminate()
                p.wait(3)
            except (OSError, subprocess.TimeoutExpired):
                p.kill()


def lines(sock, until="end", timeout=10):
    sock.settimeout(timeout)
    data, out = b"", {}
    while True:
        while b"\n" in data:
            line, data = data.split(b"\n", 1)
            line = line.decode().strip()
            if line == until:
                sock.settimeout(None)
                return out, data
            k, _, v = line.partition(" ")
            out[k] = v
        more = sock.recv(4096)
        if not more:
            raise OSError("the NBT closed the relay")
        data += more


def relay(phone, nbt):
    """RFCOMM <-> TCP until the RFCOMM channel ends (then the NBT's side is told by shutdown) or the NBT ends"""
    up = down = 0
    while True:
        r, _, _ = select.select([phone, nbt], [], [])
        if phone in r:
            try:
                b = phone.recv(65536)
            except OSError as e:
                log("RFCOMM:", e)
                b = b""
            if not b:
                log("the iPhone closed RFCOMM (%d bytes to the NBT, %d back)" % (up, down))
                try:
                    nbt.shutdown(socket.SHUT_WR)
                except OSError:
                    pass
                return True
            up += len(b)
            nbt.sendall(b)
        if nbt in r:
            b = nbt.recv(65536)
            if not b:
                log("the NBT ended the session (%d bytes to the NBT, %d back)" % (up, down))
                return False
            down += len(b)
            phone.sendall(b)


def wait_closed(sock, seconds=None):
    """True when the NBT closed the connection (anything it sends meanwhile is dropped)"""
    end = time.time() + seconds if seconds else None
    while True:
        left = end - time.time() if end else None
        if left is not None and left <= 0:
            return False
        r, _, _ = select.select([sock], [], [], left)
        if r:
            try:
                if not sock.recv(65536):
                    return True
            except OSError:
                return True


def session(c, bt, bssid):
    scope = socket.if_nametoindex(c["IFACE"])
    nbt = socket.socket(socket.AF_INET6, socket.SOCK_STREAM)
    nbt.settimeout(3)
    nbt.connect((c["NBT"], int(c["PORT"]), 0, scope))
    log("NBT relay connected")
    nbt.sendall(("nbt-bridge 1\nbt %s\nbssid %s\nssid %s\npsk %s\nchannel %s\nsecurity %s\nend\n"
                 % (bt, bssid, c["SSID"], c["PSK"], c["CHANNEL"], c["SECURITY"])).encode())
    info, rest = lines(nbt)
    log("NBT receiver:", info.get("name"), "[%s]:%s" % (info.get("address"), info.get("port")),
        "deviceid", info.get("deviceid"))
    mdns = Mdns(info, c["IFACE"])
    try:
        while True:
            phones = paired_phones()
            if not phones:
                log("no paired iPhone (pair it once with bluetoothctl)")
            for mac, name in phones:
                ch = iap2_channel(mac)
                if not ch:
                    continue
                try:
                    phone = rfcomm(mac, ch)
                except OSError as e:
                    log("RFCOMM to", name, mac, "channel", ch, "failed:", e)
                    _channels.pop(mac, None)   # looked up again next time
                    continue
                log("RFCOMM to", name, mac, "open")
                with phone:
                    nbt.sendall(("iap2 %s\n" % mac).encode())
                    if rest:
                        phone.sendall(rest)
                    if not relay(phone, nbt):
                        return
                wait_closed(nbt)   # the iPhone is on Wi-Fi now; the NBT ends the relay after the session
                log("the NBT ended the session")
                return
            if wait_closed(nbt, 5):
                log("the NBT closed the relay")
                return
    finally:
        mdns.close()
        nbt.close()


def main():
    Agent().start()
    time.sleep(3)
    for mac, name in paired_phones():
        log("paired:", name, mac)
        iap2_channel(mac)
    last = None
    while True:
        c = conf()
        bt = bt_address()
        try:
            with open("/sys/class/net/%s/address" % c["WLAN"]) as f:
                bssid = f.read().strip().upper()
        except OSError:
            bssid = None
        if not bt or not bssid or not c["SSID"]:
            log("waiting: Bluetooth", bt, "Wi-Fi", bssid, "access point", c["SSID"] or None)
            time.sleep(5)
            continue
        try:
            session(c, bt, bssid)
            last = None
        except OSError as e:
            if str(e) != last:   # the same failure (the NBT not there yet) only once
                log("NBT relay:", e)
            last = str(e)
            time.sleep(2)


if __name__ == "__main__":
    main()
