"""Stamps a version into a build. Run before PyInstaller:

    python make_version.py v1.2.0

Writes stripes_version.py (shown in the app, used by self-update) and
version_info.txt (the exe's Properties > Details, via --version-file).

When the environment variable STRIPES_HX_KEY holds the printer-password key
(64 hex characters; release builds get it from the repository secret), it also
writes hx_key.py with it. Without it, any old hx_key.py is removed and the build
uses the public development key. The key is never printed: only its key id.

Standard library only (no hx_ module), so it runs before anything else exists.
"""

import hashlib
import os
import re
import sys

KEY_ENV = "STRIPES_HX_KEY"     # environment variable: the key as 64 hex characters
KEY_FILE = "hx_key.py"         # written next to stripes_version.py
KEY_BYTES = 32

VERSION_INFO = """\
VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'Stripes - Zebra labels and MapleJet bag printing'),
      StringStruct('FileVersion', {version!r}),
      StringStruct('InternalName', 'Stripes'),
      StringStruct('OriginalFilename', 'Stripes.exe'),
      StringStruct('ProductName', 'Stripes'),
      StringStruct('ProductVersion', {version!r})])]),
    VarFileInfo([VarStruct('Translation', [0x0409, 0x04B0])])
  ]
)
"""

_KEY_MODULE = '''\
"""The printer-password key of this build. Written by make_version.py from the STRIPES_HX_KEY
secret. Generated and git-ignored: never commit it."""
KEY = {key!r}
'''


def version_numbers(version):
    """'v1.2.3' -> (1, 2, 3, 0), the four numbers Windows wants; zeros for dev builds."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
    return tuple(int(n) for n in m.groups()) + (0,) if m else (0, 0, 0, 0)


def stamp(version, folder="."):
    with open(f"{folder}/stripes_version.py", "w", encoding="utf-8") as f:
        f.write(f"VERSION = {version!r}\n")
    with open(f"{folder}/version_info.txt", "w", encoding="utf-8") as f:
        f.write(VERSION_INFO.format(version=version, numbers=version_numbers(version)))


def parse_key(text):
    """64 hex characters (either case, spaces around are fine) -> the 32-byte key.
    The error messages never contain the text, so a bad secret can't end up in a log."""
    text = text.strip()
    if len(text) != 2 * KEY_BYTES:
        raise ValueError(f"{KEY_ENV} must be 64 hex characters (32 bytes), not {len(text)} characters")
    # Checked here, not by bytes.fromhex: that also takes spaces between the bytes
    if not re.fullmatch(r"[0-9A-Fa-f]{64}", text):
        raise ValueError(f"{KEY_ENV} must be hex")
    return bytes.fromhex(text)


def key_id(key):
    """8 hex characters naming a key, the same as hx_protocol.key_id. It reveals nothing about the key."""
    return hashlib.sha256(b"stripes-hx-key-id" + key).hexdigest()[:8]


def stamp_key(key, folder="."):
    """Writes <folder>/hx_key.py with the key and returns its key id. With None, removes
    any hx_key.py (so the build uses the development key) and returns "development"."""
    path = os.path.join(folder, KEY_FILE)
    if key is None:
        try:
            os.remove(path)
        except FileNotFoundError:
            pass
        return "development"
    # Only ever a 32-byte key: the 29-byte development key can't be stamped by accident
    if not isinstance(key, bytes) or len(key) != KEY_BYTES:
        raise ValueError(f"the printer-password key must be {KEY_BYTES} bytes")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(_KEY_MODULE.format(key=key))
    return key_id(key)


def main(argv, environ, folder="."):
    """python make_version.py <version>. Returns the exit code."""
    if len(argv) != 1:
        print("usage: python make_version.py v1.2.0", file=sys.stderr)
        return 1
    version = argv[0]
    # An older build's key never survives this run, even if the run fails part way
    stamp_key(None, folder)
    stamp(version, folder)
    raw = environ.get(KEY_ENV, "").strip()
    if not raw:
        print(f"Stamped {version} with the development key")
        return 0
    try:
        key = parse_key(raw)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 1
    kid = stamp_key(key, folder)
    print(f"Stamped {version} with printer-password key {kid}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:], os.environ))
