#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Valentin Radu
#
# One-time network setup of the bridge board (docs/WIRELESS-BRIDGE.md), with NetworkManager: bridge br0 = the NCM
# link to the NBT (usb0, from nbt-gadget.sh) + a 5 GHz access point (wlan0). The iPhone on the access point and the
# NBT on usb0 are then on one link (IPv6 link-local, mDNS); the iPhone gets an IPv4 address from NetworkManager's
# shared mode on br0. Writes /etc/nbt-bridge.conf for nbt-bridge.py. Run again to change the name or the passphrase.
#   sudo nbt-net.sh [SSID [PASSPHRASE]]
set -e
SSID=${1:-NBT-CarPlay}
PSK=${2:-$(tr -dc A-Za-z0-9 </dev/urandom | head -c 16)}
CHANNEL=36
WLAN=${WLAN:-wlan0}

# NetworkManager leaves USB gadget interfaces alone (udev NM_UNMANAGED); usb0 has to be a port of br0
echo 'SUBSYSTEM=="net", KERNEL=="usb0", ENV{NM_UNMANAGED}="0"' > /etc/udev/rules.d/90-nbt-usb0.rules
udevadm control --reload
[ -e /sys/class/net/usb0 ] && nmcli device set usb0 managed yes || true

for c in nbt-br nbt-usb nbt-ap; do
    nmcli connection delete "$c" >/dev/null 2>&1 || true
done
nmcli connection add type bridge con-name nbt-br ifname br0 connection.autoconnect yes \
    bridge.stp no bridge.multicast-snooping no bridge.mac-address 02:4E:42:54:00:03 \
    ipv4.method shared ipv4.addresses 10.42.0.1/24 ipv6.method link-local
nmcli connection add type ethernet con-name nbt-usb ifname usb0 slave-type bridge master br0 \
    connection.autoconnect yes
nmcli connection add type wifi con-name nbt-ap ifname "$WLAN" slave-type bridge master br0 connection.autoconnect yes \
    connection.autoconnect-priority 100 \
    wifi.mode ap wifi.ssid "$SSID" wifi.band a wifi.channel "$CHANNEL" \
    wifi-sec.key-mgmt wpa-psk wifi-sec.proto rsn wifi-sec.pairwise ccmp wifi-sec.group ccmp wifi-sec.psk "$PSK"

umask 077
cat > /etc/nbt-bridge.conf <<EOF
SSID=$SSID
PSK=$PSK
CHANNEL=$CHANNEL
SECURITY=2
WLAN=$WLAN
EOF

nmcli device disconnect "$WLAN" >/dev/null 2>&1 || true   # whatever else had the Wi-Fi (an earlier hotspot test)
nmcli connection up nbt-ap || echo "nbt-net: the access point did not come up (see journalctl -u NetworkManager)"
nmcli connection up nbt-usb >/dev/null 2>&1 || echo "nbt-net: usb0 comes up when the NBT is connected"
echo "nbt-net: access point \"$SSID\", passphrase $PSK, channel $CHANNEL"
nmcli device
