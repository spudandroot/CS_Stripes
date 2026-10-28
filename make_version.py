"""Stamps a version into a build. Run before PyInstaller:

    python make_version.py v1.2.0

Writes stripes_version.py (shown in the app, used by self-update) and
version_info.txt (the exe's Properties > Details, via --version-file).
"""

import re
import sys

VERSION_INFO = """\
VSVersionInfo(
  ffi=FixedFileInfo(filevers={numbers}, prodvers={numbers}, mask=0x3f, flags=0x0,
                    OS=0x40004, fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('FileDescription', 'Stripes - Zebra label printing'),
      StringStruct('FileVersion', {version!r}),
      StringStruct('InternalName', 'Stripes'),
      StringStruct('OriginalFilename', 'Stripes.exe'),
      StringStruct('ProductName', 'Stripes'),
      StringStruct('ProductVersion', {version!r})])]),
    VarFileInfo([VarStruct('Translation', [0x0409, 0x04B0])])
  ]
)
"""


def version_numbers(version):
    """'v1.2.3' -> (1, 2, 3, 0), the four numbers Windows wants; zeros for dev builds."""
    m = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", version)
    return tuple(int(n) for n in m.groups()) + (0,) if m else (0, 0, 0, 0)


def stamp(version, folder="."):
    with open(f"{folder}/stripes_version.py", "w", encoding="utf-8") as f:
        f.write(f"VERSION = {version!r}\n")
    with open(f"{folder}/version_info.txt", "w", encoding="utf-8") as f:
        f.write(VERSION_INFO.format(version=version, numbers=version_numbers(version)))


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage: python make_version.py v1.2.0")
    stamp(sys.argv[1])
