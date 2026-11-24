#!/bin/sh
# SPDX-License-Identifier: GPL-3.0-only
# Copyright (C) 2026 Valentin Radu
#
# Before nbt-bridge: makes sure the AIC8800's Bluetooth controller is there. Radxa's boot-time UART reset
# (rsetup-aic8800-reset@ttyS1) sometimes fails and leaves no controller; then: reset and attach again, up to 5 times
# (without Radxa's attach service: hciattach as in their forum). --force: reset even when the controller is there
# (nbt-bridge.py, when pairing keeps failing).
ok() { timeout 5 bluetoothctl show 2>/dev/null | grep -q '^Controller'; }
force=$([ "$1" = --force ] && echo 1)
for i in 1 2 3 4 5; do
    [ -z "$force" ] && ok && exit 0
    force=""
    echo "nbt-bt-up: resetting the AIC8800 (attempt $i)"
    systemctl stop rsetup-hciattach@ttyS1 2>/dev/null
    pkill -x hciattach
    systemctl restart rsetup-aic8800-reset@ttyS1 || echo "nbt-bt-up: the reset service failed"
    systemctl restart rsetup-hciattach@ttyS1 2>/dev/null || hciattach -s 1500000 /dev/ttyS1 any 1500000 flow nosleep
    sleep 5
done
ok && exit 0
echo "nbt-bt-up: still no Bluetooth controller"
exit 1
