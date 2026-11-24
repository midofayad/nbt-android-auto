#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Valentin Radu
#
# Radxa ZERO 3W as a USB network device (CDC NCM) on its OTG port, for the NBT (wireless CarPlay bridge,
# docs/WIRELESS-BRIDGE.md). nbt_carplay --wireless on the NBT opens it (1d6b:0104).
# Started at boot by nbt-gadget.service.
set -e
G=/sys/kernel/config/usb_gadget/nbt
UDC=$(ls /sys/class/udc | head -n 1)

modprobe libcomposite
mountpoint -q /sys/kernel/config || mount -t configfs none /sys/kernel/config
[ -d "$G" ] && exit 0

mkdir -p "$G"
cd "$G"
echo 0x1d6b > idVendor      # Linux Foundation
echo 0x0104 > idProduct     # Multifunction Composite Gadget
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB
mkdir -p strings/0x409
echo "Radxa" > strings/0x409/manufacturer
echo "NBT wireless bridge" > strings/0x409/product
echo "0001" > strings/0x409/serialnumber

mkdir -p configs/c.1/strings/0x409
echo "NCM" > configs/c.1/strings/0x409/configuration
echo 500 > configs/c.1/MaxPower

mkdir -p functions/ncm.usb0
echo 02:4e:42:54:00:01 > functions/ncm.usb0/host_addr   # the NBT's side
echo 02:4e:42:54:00:02 > functions/ncm.usb0/dev_addr    # the Radxa's side
ln -s functions/ncm.usb0 configs/c.1/

echo "$UDC" > UDC
echo "nbt-gadget: NCM on $UDC"
