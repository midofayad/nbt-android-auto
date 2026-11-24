# Wireless CarPlay through a bridge board

<!-- SPDX-License-Identifier: GPL-3.0-only -->
<!-- Copyright (C) 2026 Valentin Radu -->

Status (2026-11-24, in the car with an iPhone 15 Pro Max on iOS 17): wireless CarPlay works. Bluetooth pairing from the
iPhone, iAP2 over Bluetooth with the car's 2.0B authentication chip, the iPhone joining the board's access point,
AirPlay, the picture (about 30 fps), touch / controller and sound (AAC-LC / Opus, below).

The NBT does CarPlay itself, as with the cable (`nbt_carplay`: iAP2 with the car's own Apple authentication chip, the
AirPlay receiver, picture, sound, controller). A small Linux board on the NBT's USB port does what the NBT cannot: the
Bluetooth and the 5 GHz access point the iPhone needs for wireless CarPlay. Tested board: Radxa ZERO 3W (RK3566,
AIC8800 Wi-Fi / Bluetooth) with Radxa OS (Debian 12 bookworm, kernel 6.1).

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
3. The board's Bluetooth carries the service records of a head unit with wireless CarPlay: the accessory's iAP2
   service (`00000000-deca-fade-deca-deafdecacaff`) and the CarPlay vehicle service
   (`d31fbf50-5d57-2797-a240-41cd484388ec`), registered by `nbt-bridge.py` over bluetoothd's local SDP socket
   (`bluetoothd --compat`). The iPhone reads them when it pairs (2026-11-24 without them: identification,
   authentication and the Wi-Fi configuration went through, but the iPhone never joined the access point).
   The board publishes the NBT's AirPlay receiver over mDNS (`_airplay._tcp`, with avahi) and opens the iAP2 RFCOMM
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

Tested with the stock image `radxa-zero3_bookworm_kde_b1.output_512.img.xz` (Radxa OS, Debian 12 bookworm, kernel
6.1.84; user `rock`, password `rock`). The board needs no internet: the only package missing from the image is
`avahi-utils`, copied over as a file
([avahi-utils_0.8-10+deb12u1_arm64.deb](http://ftp.us.debian.org/debian/pool/main/a/avahi/avahi-utils_0.8-10+deb12u1_arm64.deb),
the same version as the image's `libavahi-client3`).

1. `sudo dpkg -i avahi-utils_0.8-10+deb12u1_arm64.deb`
2. Put the files of [radxa/](../radxa/) into `/home/rock/nbt` (for example from the SD card's FAT partition; with LF
   line ends).
3. `sudo sh /home/rock/nbt/nbt-setup.sh` (optional: `... nbt-setup.sh NAME PASSPHRASE` for the access point; default
   `NBT-CarPlay` and a random passphrase, kept in `/etc/nbt-bridge.conf` and reused when run again)
4. `sudo reboot`
5. On the iPhone: Settings → Bluetooth → the board's name → Pair. The board accepts and trusts it by itself
   (`journalctl -u nbt-bridge` shows "paired with ..." and the iPhone's iAP2 RFCOMM channel).

That is the whole setup. `nbt-setup.sh` checks the tools it needs (`python3` with Bluetooth sockets, `bluetoothctl`,
`sdptool`, `avahi-daemon`, `avahi-publish`, `avahi-browse`, `nmcli`, `dnsmasq`), installs the scripts and services,
sets Bluetooth up in `/etc/bluetooth/main.conf` (car audio class 0x200420, discoverable and pairable without time
limit; the original is kept as `main.conf.nbt-orig`), runs bluetoothd with `--compat` and without A2DP / AVRCP,
disables PipeWire / WirePlumber, starts the USB gadget, runs `nbt-net.sh` (bridge br0 = usb0 + the 5 GHz access point)
and starts `nbt-bridge`, whose bluetoothctl agent accepts every pairing. Before it starts, `nbt-bt-up.sh` resets the
AIC8800 Bluetooth again if Radxa's boot-time reset (`rsetup-aic8800-reset@ttyS1`) left no controller (it sometimes
fails). Run it again after changing the files.

The board's Bluetooth name is its host name (`radxa-zero3` on the stock image): BlueZ's host name plugin takes
precedence over the `Name` that `nbt-setup.sh` writes into `main.conf`. Change it with `sudo hostnamectl hostname NAME`.

The board is powered by the NBT through its OTG port (the USB-C port marked OTG); its other USB-C port stays free.
Serial console (as used for all of this): pins 6 (GND), 8 (TX), 10 (RX), 1500000 baud, 8N1, no flow control.

## Logs

- NBT: `/fs/ram3/nbt-aa-native/carplay-N.log` (`wireless:`, `iap2-bt:`, `iap2-tunnel:`, `airplay:` lines),
  `native.log`.
- Board: `journalctl -u nbt-bridge -f` (relay, RFCOMM, mDNS, the connect request), `journalctl -u NetworkManager`.

## Sound

Over Wi-Fi the iPhone does not use LPCM: with only LPCM offered it set up no audio stream at all. As xcertplay's
wireless table, `nbt_carplay --wireless` offers media as AAC-LC 44.1 / 48 kHz stereo on stream 102 and the rest as
Opus mono (Siri 24 / 48 kHz, navigation and alerts 48 kHz, telephony 16 / 24 / 48 kHz), and the microphone as Opus
16 kHz. `cp_codec.c` decodes with FAAD2 and libopus and encodes the microphone with libopus; the decoded sound goes the
same way as the cable's. In the car: music (AAC-LC 44.1 kHz) and the rest play. `nbt_carplay` holds about 0.2-0.4 s
of media in its queue (the iPhone sends bursts at the start); the rest of the delay before music starts is the
iPhone's own and the car's output. A pause key while music plays drops the queued music and ignores what still arrives
until the iPhone plays again (it sends a short burst after it paused), so the music stops when the key is pressed
(otherwise about 0.5 s later). Next / previous while music plays: the same until the iPhone reports the new track
(NowPlaying: title, queue index or an elapsed time near 0; at most 1.5 s).

The board must not take the iPhone's Bluetooth audio: `nbt-setup.sh` runs bluetoothd without its A2DP / AVRCP plugins
and disables PipeWire / WirePlumber (with them, the trusted iPhone sent its sound to the board).

## Leaving and coming back

Over Wi-Fi an iPhone that goes out of range closes nothing: its connections just go quiet. `nbt_carplay` ends the
session when the screen stream (a message about every second, even on a still screen) is silent for 10 s;
native_service starts the next run, the board's relay connects to it again, and the board tries the paired iPhones'
iAP2 channel every few seconds and also takes the iPhone's own Bluetooth connection. When the iPhone is back in range,
the session starts again without anything to do on the phone (not yet tried in the car).

## Pairing again

After "Forget This Device" on the iPhone, BlueZ fails the next pairings at their final check (Simple Pairing Complete:
Authentication Failure, every few hundred ms) until its Bluetooth starts afresh. `nbt-bridge` resets the chip and
restarts bluetoothd after 3 confirmations within 30 s without a pairing.

## Open points

- The board's start-up time (Linux boot) before CarPlay can start.
- The board's current draw from the NBT's port.
- The AirPlay iAP2 tunnel (stream 130) is implemented but the iPhone kept Bluetooth for iAP2 so far.
