"""lwIP, Monocypher, libopus and FAAD2 for nbt_carplay (src/carplay), taken by build_all.py into the VM archive.

SPDX-License-Identifier: GPL-3.0-only
Copyright (C) 2026 Valentin Radu

  lwIP 2.2.1 (BSD-3-Clause, https://savannah.nongnu.org/projects/lwip, GitHub mirror lwip-tcpip/lwip, tag
  STABLE-2_2_1_RELEASE): the IPv6 / TCP / UDP stack on the iPhone's NCM link, inside nbt_carplay (the car's own
  network stack is not touched)
  Monocypher 4.0.2 (BSD-2-Clause or CC0-1.0, https://monocypher.org, tag 4.0.2): Ed25519 and X25519 for AirPlay's
  pairing (Mbed TLS has no Ed25519)
  libopus 1.5.2 (BSD-3-Clause, https://opus-codec.org, GitHub xiph/opus, tag v1.5.2): wireless CarPlay's Opus (Siri,
  navigation, telephony, the microphone)
  FAAD2 2.11.2 (GPL-2.0-or-later, https://github.com/knik0/faad2, tag 2.11.2): wireless CarPlay's AAC-LC (media)

Each is cloned once into .local/ (only the tag, without CR LF conversion) and must be at the expected commit; only its
source folders go into the archive (lwip/src/..., monocypher/src/..., opus/celt/..., faad2/libfaad/...). None is part
of this repository.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEPS = [
    ("lwip", "STABLE-2_2_1_RELEASE", "77dcd25a72509eb83f72b033d219b1d40cd8eb95", "https://github.com/lwip-tcpip/lwip.git",
     ("src",)),
    ("monocypher", "4.0.2", "0d85f98c9d9b0227e42cf795cb527dff372b40a4",
     "https://github.com/LoupVaillant/Monocypher.git", ("src",)),
    ("opus", "v1.5.2", "ddbe48383984d56acd9e1ab6a090c54ca6b735a6", "https://github.com/xiph/opus.git",
     ("celt", "silk", "src", "include")),
    ("faad2", "2.11.2", "673a22a3c7c33e96e2ff7aae7c4d2bc190dfbf92", "https://github.com/knik0/faad2.git",
     ("libfaad", "include")),
]


def checkout(name, tag, commit, url):
    path = ROOT / ".local" / ("%s-%s" % (name, tag))
    if not path.exists():
        print("cloning", name, tag, "into", path, flush=True)
        subprocess.run(["git", "-c", "core.autocrlf=false", "-c", "advice.detachedHead=false", "clone", "-q",
                        "--depth", "1", "--branch", tag, url, str(path)], check=True)
    head = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], stdout=subprocess.PIPE,
                          check=True).stdout.decode().strip()
    if head != commit:
        raise SystemExit("%s must be at %s %s (%s)" % (path, name, tag, commit))
    status = subprocess.run(["git", "-C", str(path), "status", "--porcelain"], stdout=subprocess.PIPE,
                            check=True).stdout.decode().strip()
    if status:
        raise SystemExit("%s has local changes" % path)
    return path


def add_carplay_deps(archive):
    for name, tag, commit, url, folders in DEPS:
        path = checkout(name, tag, commit, url)
        for folder in folders:
            for item in sorted((path / folder).rglob("*")):
                if item.is_file() and item.suffix in (".c", ".h"):
                    archive.add("%s/%s" % (name, item.relative_to(path).as_posix()), item.read_bytes())
