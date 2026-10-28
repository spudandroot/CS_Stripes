# PyInstaller recipe for Stripes.exe, used by the Actions build and build.bat:
#     python make_version.py v1.2.0      (writes stripes_version.py and version_info.txt)
#     pyinstaller --noconfirm pyinstaller.spec
# It leaves out parts of Python, Pillow and Tcl that Stripes never uses, so the exe is
# smaller and starts faster. The build's self-check (Stripes.exe --self-check) loads
# everything Stripes does use, so a wrong exclusion fails the build instead of a PC.
import os

EXCLUDES = [
    # Pillow: AVIF pictures (4 MB on its own) and color management; printing is black and white
    "PIL._avif", "PIL.AvifImagePlugin", "PIL._imagingcms", "PIL.ImageCms",
    # pulled in by build tooling and docs, never by Stripes
    "setuptools", "pkg_resources", "_distutils_hack", "distutils", "packaging",
    "pydoc", "pydoc_data", "multiprocessing",
    # OpenSSL (2 MB): the exact preview uses Windows' own HTTPS (wininet_post)
    "ssl", "_ssl", "_hashlib",
]

# Tcl's time zones and month names (only its clock command uses them) and its East Asian
# text encodings (1.4 MB; Windows gives Tk Unicode, and Western PCs use cp1252).
TCL_UNUSED = ("_tcl_data/tzdata/", "_tcl_data/msgs/")
TCL_ENCODINGS_UNUSED = {
    "big5", "cns11643", "cp932", "cp936", "cp949", "cp950", "euc-cn", "euc-jp", "euc-kr", "gb12345",
    "gb2312", "gb2312-raw", "jis0208", "jis0212", "ksc5601", "macJapan", "shiftjis",
}


# Windows 10 and 11 include the Universal C Runtime, so the exe needn't carry its own copy
# (ucrtbase.dll and the api-ms-win-* files). VCRUNTIME140 isn't part of Windows and stays.
def windows_has(dest):
    name = os.path.basename(dest).lower()
    return name == "ucrtbase.dll" or name.startswith("api-ms-win-")


def wanted(dest):
    dest = dest.replace("\\", "/")
    if dest.startswith(TCL_UNUSED):
        return False
    if dest.startswith("_tcl_data/encoding/"):
        return os.path.splitext(os.path.basename(dest))[0] not in TCL_ENCODINGS_UNUSED
    return True


a = Analysis(
    [os.path.join(SPECPATH, "stripes.py")],
    datas=[(os.path.join(SPECPATH, "assets"), "assets")],
    hiddenimports=["pystray._win32"],  # the tray's Windows backend, loaded by name at run time
    excludes=EXCLUDES,
)
a.datas = [entry for entry in a.datas if wanted(entry[0])]
a.binaries = [entry for entry in a.binaries if not windows_has(entry[0])]
pyz = PYZ(a.pure)
exe = EXE(
    pyz, a.scripts, a.binaries, a.datas, [],
    name="Stripes",
    icon=os.path.join(SPECPATH, "assets", "icon.ico"),
    version=os.path.join(SPECPATH, "version_info.txt"),
    console=False,  # windowed
    upx=False,      # compressed exes upset antivirus
)
