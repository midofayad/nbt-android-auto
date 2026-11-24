#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Valentin Radu
#
# Sets up the bridge board for wireless CarPlay (docs/WIRELESS-BRIDGE.md), from the folder this script is in (for
# example /home/rock/nbt): checks the tools, installs the scripts and services, makes Bluetooth a car (car audio
# class, always discoverable and pairable; its name stays the host name), the USB gadget, the bridge and access point
# (nbt-net.sh), and starts everything. Safe to run again.
#   sudo sh nbt-setup.sh [SSID [PASSPHRASE]]
set -e
D=$(cd "$(dirname "$0")" && pwd)
[ "$(id -u)" = 0 ] || { echo "nbt-setup: run it with sudo"; exit 1; }

missing=""
for t in python3 bluetoothctl sdptool avahi-publish avahi-browse avahi-daemon nmcli dnsmasq; do
    command -v $t >/dev/null 2>&1 || missing="$missing $t"
done
python3 -c "import socket; socket.AF_BLUETOOTH" 2>/dev/null || missing="$missing python3-Bluetooth-sockets"
if [ -n "$missing" ]; then
    echo "nbt-setup: missing:$missing (nothing changed)"
    exit 1
fi

install -m 755 "$D/nbt-gadget.sh" "$D/nbt-net.sh" "$D/nbt-bridge.py" "$D/nbt-bt-up.sh" /usr/local/sbin/
install -m 644 "$D/nbt-gadget.service" "$D/nbt-bridge.service" /etc/systemd/system/
systemctl daemon-reload

# Bluetooth: name, car audio class (so the iPhone lists it), never stops being discoverable / pairable
C=/etc/bluetooth/main.conf
[ -f $C.nbt-orig ] || cp $C $C.nbt-orig
sed -i -e '/^\(Name\|Class\|DiscoverableTimeout\|PairableTimeout\|AlwaysPairable\) *=/d' $C
sed -i -e '/^\[General\]/a Name = BMW NBT\nClass = 0x200420\nDiscoverableTimeout = 0\nPairableTimeout = 0\nAlwaysPairable = true' $C
# bluetoothd with its local SDP socket (/run/sdp): nbt-bridge registers the CarPlay service records there; without
# Bluetooth audio (A2DP, AVRCP): the iPhone's sound goes through CarPlay to the NBT, not to the board
BTD=$(ls /usr/libexec/bluetooth/bluetoothd /usr/lib/bluetooth/bluetoothd 2>/dev/null | head -n 1)
mkdir -p /etc/systemd/system/bluetooth.service.d
printf '[Service]\nExecStart=\nExecStart=%s --compat --noplugin=a2dp,avrcp\n' "$BTD" \
    > /etc/systemd/system/bluetooth.service.d/nbt-compat.conf
# and no PipeWire / WirePlumber (they register Bluetooth audio and hands-free with bluetoothd for every login)
systemctl --global mask pipewire.service pipewire.socket pipewire-pulse.service pipewire-pulse.socket \
    wireplumber.service >/dev/null 2>&1 || true
pkill -x wireplumber || true
pkill -x pipewire-pulse || true
pkill -x pipewire || true
systemctl daemon-reload
systemctl enable bluetooth avahi-daemon
systemctl restart bluetooth
systemctl start avahi-daemon

systemctl enable --now nbt-gadget
/usr/local/sbin/nbt-net.sh "$@"
systemctl enable nbt-bridge
systemctl restart nbt-bridge
sleep 5
systemctl --no-pager status nbt-gadget nbt-bridge | grep -e "●" -e Active
journalctl -u nbt-bridge --no-pager -n 10
echo "nbt-setup: done. Reboot, then pair the iPhone: Settings > Bluetooth > $(hostname) (the board's host name)."
