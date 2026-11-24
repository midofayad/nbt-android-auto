# Wireless CarPlay through a bridge board

<!-- SPDX-License-Identifier: GPL-3.0-only -->
<!-- Copyright (C) 2026 Valentin Radu -->

Status (2026-11-24): written and host-tested, **not yet tried in the car**.

The NBT does CarPlay itself, as with the cable (`nbt_carplay`: iAP2 with the car's own Apple authentication chip, the
AirPlay receiver, picture, sound, controller). A small Linux board on the NBT's USB port does what the NBT cannot: the
Bluetooth and the 5 GHz access point the iPhone needs for wireless CarPlay. Tested board: Radxa ZERO 3W (RK3566,
AIC8800 Wi-Fi / Bluetooth, Radxa OS / Debian 12).

```
iPhone --Bluetooth RFCOMM (iAP2)--> board: nbt-bridge.py --TCP over USB NCM--> nbt_carplay --wireless (NBT)
iPhone --Wi-Fi (board's access point)--> br0 bridge --usb0 (NCM)--> nbt_carplay's AirPlay receiver
```

## How a session goes

1. The board's USB gadget (`nbt-gadget.sh`, 1d6b:0104, one CDC NCM function) gives the NBT a network link. native_service
   sees 1d6b:0104 and starts `nbt_carplay --wireless`, which opens the NCM function as it does an iPhone's (IPv6
   link-local `fe80::4e:42ff:fe54:1`, from the MAC address the gadget gives the NBT) and listens on port 7100.
2. `nbt-bridge.py` connects to that port and exchanges a few text lines: the board's Bluetooth address, its access
   point (BSSID, SSID, passphrase, channel, WPA2) for the NBT; the NBT's AirPlay identity (device ID, key, features)
   for the board.
3. The board publishes the NBT's AirPlay receiver over mDNS (`_airplay._tcp`, with avahi) and opens the iAP2 RFCOMM
   channel of a paired iPhone (SDP service `00000000-deca-fade-deca-deafdecacafe`). From then on it relays bytes.
4. Over that, `nbt_carplay` runs iAP2 as xcertplay's wireless client: marker, SYN after the iPhone's marker, link
   acknowledgements; identification with the Bluetooth and wireless CarPlay transports; authentication with the car's
   chip; the access point for RequestAccessoryWiFiConfigurationInformation (5702 → 5703); CarPlayStartSession (4301)
   with the Wi-Fi group and the NBT's address.
5. The iPhone joins the access point. The board finds its `_carplay-ctrl._tcp` service and sends it
   `GET /ctrl-int/1/connect` with the NBT's device ID; the iPhone connects to the AirPlay receiver on the NBT (the
   access point and usb0 are one bridge, so it reaches the NBT directly).
6. AirPlay as with the cable, plus stream 130: the iPhone moves iAP2 into the AirPlay connection (encrypted packages
   to a data port we give, our side as `iAPSendMessage` commands), and may close Bluetooth.
7. When the AirPlay session ends `nbt_carplay` ends; native_service starts it again and the board reconnects.

## Setting up the board

Everything goes over the SD card's FAT partition (the board needs no internet). Packages it needs (`nbt-setup.sh`
checks them first): `python3`, `bluez` (`bluetoothctl`, `sdptool`), `avahi-daemon`, `avahi-utils`, NetworkManager with
`dnsmasq-base`.

1. Copy the folder [radxa/](../radxa/) to the FAT partition (its mount point: `findmnt -t vfat`).
2. `sudo sh <mount point>/radxa/nbt-setup.sh` (optional: `... nbt-setup.sh NAME PASSPHRASE` for the access point;
   default `NBT-CarPlay` and a random passphrase, kept in `/etc/nbt-bridge.conf`)
3. On the iPhone: Settings → Bluetooth → "BMW NBT" → Pair. The board accepts and trusts it by itself
   (`journalctl -u nbt-bridge` shows "paired with ..." and the iPhone's iAP2 RFCOMM channel).

`nbt-setup.sh` installs the scripts and services, sets Bluetooth up as a car in `/etc/bluetooth/main.conf` (name
"BMW NBT", car audio class 0x200420, discoverable and pairable without time limit; the original is kept as
`main.conf.nbt-orig`), starts the USB gadget, runs `nbt-net.sh` (bridge br0 = usb0 + the 5 GHz access point) and starts
`nbt-bridge`, whose bluetoothctl agent accepts every pairing. Before it starts, `nbt-bt-up.sh` resets the AIC8800 Bluetooth
again if Radxa's boot-time reset (`rsetup-aic8800-reset@ttyS1`) left no controller (it sometimes fails). Run it again after changing the files.

The board is powered by the NBT through its OTG port (the USB-C port marked OTG); its other USB-C port stays free.
Debugging over its serial console: pins 6 (GND), 8 (TX), 10 (RX), 1500000 baud.

## Logs

- NBT: `/fs/ram3/nbt-aa-native/carplay-N.log` (`wireless:`, `iap2-bt:`, `iap2-tunnel:`, `airplay:` lines),
  `native.log`.
- Board: `journalctl -u nbt-bridge -f` (relay, RFCOMM, mDNS, the connect request), `journalctl -u NetworkManager`.

## Open points

- Whether the iPhone accepts the car's 2.0B chip for wireless CarPlay (it does over the cable).
- Sound: `nbt_carplay` offers only LPCM (as over the cable). xcertplay offers AAC-LC for media and Opus or AAC-ELD for
  the rest over Wi-Fi; if the iPhone wants compressed audio over Wi-Fi, decoders are needed.
- The board's start-up time (Linux boot) before CarPlay can start.
- The board's current draw from the NBT's port.
