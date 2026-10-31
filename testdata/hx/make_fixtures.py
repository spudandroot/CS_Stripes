"""Write the MapleJet Hx parser fixtures in this folder (protocol.md §17).

Run once from the repo root and commit the output:  python testdata/hx/make_fixtures.py

Standard library only, and self-contained: it imports no hx_ module, so a bug in the
code can't change a fixture. Everything is sanitized: host 192.0.2.10, UID 1109 and
password 123321 (the manual's own example), no real token, cookie, address or name.
The real pictures weren't kept, so every BMP is synthetic, with the real header and a
black rectangle giving the captured black-pixel count.
"""

import base64
import gzip
import os
import struct

HERE = os.path.dirname(os.path.abspath(__file__))

HOST = "192.0.2.10"
UID = 1109
TOKEN = "Yzg4MzdiMjNmZjhhYWE4YTJkZGU5MTU0NzNjZTA5OTE="     # the manual's example: password 123321
MD5 = "c8837b23ff8aaa8a2dde915473ce0991"
COOKIES = {"Access_token_1109": "fedcba9876543210fedcba9876543210",
           "Auth_token_1109": "0123456789abcdef0123456789abcdef"}

LOGIN_HTML = (b'<!DOCTYPE html><html><body><form method="post"><input type="password" '
              b'name="pass"></form><p>V.72.5</p></body></html>')
MAIN_HTML = b'<!DOCTYPE html><html><body><p>Hx</p></body></html>'

FONTS = ("CourierN", "Arial", "ArialNarrow", "ArialNarrowB", "TimesNewRom", "LinearBeam")
FIRMWARE = "69.75.10"


# --------------------------------------------------------------------------
# The synthetic previews
# --------------------------------------------------------------------------

def bmp(rects, width=1024, height=160):
    """A white 1-bit BMP with black rectangles (x1, y1, x2, y2), top-left origin, x2 and y2
    exclusive; the header the printer sends (62 bytes: file header, info header, palette)."""
    row_bytes = (width + 31) // 32 * 4
    rows = [bytearray(b"\xff" * row_bytes) for _ in range(height)]   # stored bottom-up
    for x1, y1, x2, y2 in rects:
        for y in range(max(0, y1), min(height, y2)):
            row = rows[height - 1 - y]
            for x in range(max(0, x1), min(width, x2)):
                row[x // 8] &= ~(0x80 >> (x % 8)) & 0xFF
    pixels = b"".join(rows)
    header = struct.pack("<2sIHHI", b"BM", 62 + len(pixels), 0, 0, 62)
    info = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 1, 0, len(pixels), 0, 0, 0, 0)
    palette = b"\x00\x00\x00\x00" + b"\xff\xff\xff\x00"
    return header + info + palette + pixels


BMP_2564 = bmp([(10, 10, 651, 14)])     # x 10 to 650, y 10 to 13: 641 x 4
BMP_3492 = bmp([(0, 0, 873, 4)])        # x 0 to 872, y 0 to 3: 873 x 4
BMP_0 = bmp([])


def data_uri(picture):
    # The printer puts a space after "base64,"
    return "data:image/bmp;base64, " + base64.b64encode(picture).decode("ascii")


# --------------------------------------------------------------------------
# SME replies (§17.1, §17.6)
# --------------------------------------------------------------------------

def crlf(lines):
    """Each line ends in CRLF; {FF} is the single byte 0xFF."""
    text = "".join(line + "\r\n" for line in lines)
    return text.encode("ascii").replace(b"{FF}", b"\xff")


def common(txt, fontind, size, pos, bound, pref, objtyp, objid):
    return [
        "<LANG>4</LANG>", f"<TXT>{txt}</TXT>", f"<FONTIND>{fontind}</FONTIND>", f"<SIZE>{size}</SIZE>",
        f"<POS>{pos}</POS>", f"<BOUND>{bound}</BOUND>", "<POSF></POSF>", f"<PREF>{pref}</PREF>",
        "<ROTAT>0</ROTAT>", "<WCOEFF>0</WCOEFF>", "<GROUP>0</GROUP>", f"<OBJTYP>{objtyp}</OBJTYP>",
        "<COLR>0</COLR>", "<XRID>0</XRID>", "<YRID>0</YRID>", "<SXAS>0</SXAS>", "<SYAS>0</SYAS>",
        f"<OBJID>{objid}</OBJID>", "<RP>0</RP>", "<MARG>0,0</MARG>",
    ]


SETTINGS_SLOT9 = [
    ("ROTAT", "0"), ("BOLD", "1"), ("WDTH", "1"), ("QUANT", "1"), ("DELAY", "0"), ("QDELAY", "0"),
    ("AUTREP", "0"), ("REVRS", "0"), ("DRAFT", "0"), ("MIRR", "0"), ("SLANT", "0"), ("ANG", "30"),
    ("SWDLY", "0"), ("SWSPD", "250"), ("WPDLY", "0"), ("PRNSPD", "1000"), ("ENCST", "0"),
    ("RESMOD", "0"), ("DLYMM", "0"), ("QDLYMM", "0"), ("ENCO", "0"), ("RESO", "640"),
    ("RESNAM", "UltraFine"), ("ENCSCL", "100"),
]


def prv(bpix, name, picture):
    return [f"<BPIX>{bpix}</BPIX>", f"<MSGNAM>{name}</MSGNAM>", f"<BMP>{data_uri(picture)}</BMP>", "</PRV>"]


def head(type_, tn, xid, msno):
    return ["<SM>", f"<TYPE>{type_}</TYPE>", f"<TN>{tn}</TN>", f"<XID>{xid}</XID>", f"<MSNO>{msno}</MSNO>"]


def slot9_load():
    """A: the slot 9 Load, the verbose FCDOC made through Autopilot."""
    lines = head("Load", "326548", "4", "9")
    lines += ["<FCDOC>", "<VER>1</VER>", "<W>256</W>", "<H>160</H>", "<MSGNAM>STRIPES TEST</MSGNAM>"]
    lines += ["<CTEXT>"] + common("STRIPES TEST 123", 1, 2, "10,10", "10,10,218,33", "", 0, 10001) + ["</CTEXT>"]
    lines += ["<CTDAT>", "<FRMT>Y/M/D</FRMT>", "<AMPM>{FF}</AMPM>", "<DAY>0</DAY>", "<DWEEK>0</DWEEK>",
              "<HOUR>0</HOUR>", "<HORFLG>1</HORFLG>", "<LANG>17742</LANG>", "<MIN>0</MIN>", "<MON>0</MON>",
              "<SEC>0</SEC>", "<YEAR>0</YEAR>", ""]
    lines += common("2026/10/29", 1, 2, "10,60", "10,60,192,83", "PKD:", 1, 1) + ["</CTDAT>"]
    lines += ["<CLOT>", "<INDX>1</INDX>", ""]
    lines += common("", 2, 3, "10,100", "10,100,66,136", "LOT:", 1, 2) + ["</CLOT>"]
    lines += ["<CPPARM>"] + [f"<{k}>{v}</{k}>" for k, v in SETTINGS_SLOT9] + ["</CPPARM>"]
    lines += ["<H>160</H>", "</FCDOC><PRV>"] + prv(2564, "STRIPES TEST", BMP_2564) + ["</SM>"]
    return crlf(lines)


COMPACT_FCDOC = (
    "<FCDOC><MSGNAM></MSGNAM><CTDAT><OBJID>1</OBJID><POS>0,0</POS><PREF>Packed: </PREF>"
    "<FRMT>M/D/y</FRMT><DAY></DAY><MON></MON><YEAR></YEAR><POSF></POSF><FONTIND>1</FONTIND>"
    "<SIZE>4</SIZE><MARG>0,0</MARG><RP>0</RP><LANG>0</LANG></CTDAT><CPPARM><ROTAT>0</ROTAT>"
    "<MIRR>0</MIRR><REVRS>0</REVRS><AUTREP>0</AUTREP><DELAY>0</DELAY><DLYMM>0</DLYMM>"
    "<PRNSPD>2500</PRNSPD><BOLD>1</BOLD><WDTH>1</WDTH><QUANT>1</QUANT><QDELAY>0</QDELAY>"
    "<QDLYMM>0</QDLYMM><RESMOD>0</RESMOD><RESNAM>UltraFine</RESNAM><ENCST>0</ENCST>"
    "<ENCSCL>100</ENCSCL></CPPARM><H>160</H>"
)


def compact_load(tn, xid):
    """B and F: a slot 0 Load answered with slot 1, the compact FCDOC another tool made."""
    lines = head("Load", tn, xid, "1") + [COMPACT_FCDOC, "</FCDOC><PRV>"] + prv(3492, "", BMP_3492) + ["</SM>"]
    return crlf(lines)


def prv_only(type_, tn, xid, bpix, name, picture):
    """C, D and E: replies with only the PRV part."""
    return crlf(head(type_, tn, xid, "9") + ["<PRV>"] + prv(bpix, name, picture) + ["</SM>"])


# --------------------------------------------------------------------------
# HTTP replies (§17.2)
# --------------------------------------------------------------------------

def response(status, headers, body):
    lines = [status] + [f"{name}: {value}" for name, value in headers]
    return ("\r\n".join(lines) + "\r\n\r\n").encode("ascii") + body


def autopilot_reply(body):
    return response("HTTP/1.1 200 OK", [("Server", "MapJet/v1.1"), ("Connection", "close"),
                                        ("Content-Length", str(len(body))), ("Content-Type", "text/plain")],
                    body)


def unauthorized():
    gz = gzip.compress(LOGIN_HTML, mtime=0)
    return response("HTTP/1.1 401 Unauthorized",
                    [("Server", "MapJet/v1.1"), ("Connection", "close"), ("Content-Type", "text/html"),
                     ("Content-Encoding", "gzip"), ("Content-Length", str(len(gz)))], gz)


def login_ok():
    gz = gzip.compress(MAIN_HTML, mtime=0)
    return response("HTTP/1.1 200 OK",
                    [("Server", "MapJet/v1.1"), ("Connection", "close"), ("Content-Type", "text/html"),
                     ("Content-Encoding", "gzip"),
                     ("Set-Cookie", f"Access_token_1109={COOKIES['Access_token_1109']}"),
                     ("Set-Cookie", f"Auth_token_1109={COOKIES['Auth_token_1109']}"),
                     ("Content-Length", str(len(gz)))], gz)


# --------------------------------------------------------------------------
# Golden requests (§17.3): this script's own copy of the §2.3 formats
# --------------------------------------------------------------------------

def autopilot_request(xml):
    body = f"<Cookie><Auth_token_{UID}>{TOKEN}</Auth_token_{UID}></Cookie><APCMD>{xml}</APCMD>".encode("utf-8")
    return b"POST / HTTP/1.1\r\nContent-Length: %d\r\nConnection: keep-alive\r\n\r\n" % len(body) + body


def login_request():
    body = f"Type=LOG&pass={MD5}&remember=on".encode("ascii")
    return (f"POST / HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\nAccept-Encoding: gzip\r\n"
            f"Content-Type: application/x-www-form-urlencoded\r\nContent-Length: {len(body)}\r\n\r\n"
            ).encode("ascii") + body


def cookie_header():
    return "; ".join(f"{name}={value}" for name, value in COOKIES.items())


def sme_request(path, body):
    return (f"POST {path} HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\nAccept-Encoding: gzip\r\n"
            f"Cookie: {cookie_header()}\r\nContent-Type: text/plain;charset=UTF-8\r\n"
            f"Content-Length: {len(body)}\r\n\r\n").encode("ascii") + body


def file_request(path):
    return (f"GET {path} HTTP/1.1\r\nHost: {HOST}\r\nConnection: close\r\nAccept-Encoding: gzip\r\n"
            f"Cookie: {cookie_header()}\r\n\r\n").encode("ascii")


# --------------------------------------------------------------------------
# Assumed web files (§17.4) and the folder's README (§17.5)
# --------------------------------------------------------------------------

def assumed_sfl():
    lines = ["<SFL>"] + [f"<FON{i}>{name}</FON{i}>" for i, name in enumerate(FONTS, start=1)] + ["</SFL>"]
    return crlf(lines)


def assumed_idn():
    return crlf(["<IDN>", f"<HXUI>{UID}</HXUI>", f"<FW>{FIRMWARE}</FW>", "</IDN>"])


README = """\
# MapleJet Hx test fixtures

Written by `make_fixtures.py` (standard library only, imports no `hx_` module). Regenerate them
from the repo root with `python testdata/hx/make_fixtures.py` and commit the output. The build
spec that describes them is `docs/maplejet/protocol.md` §17.

Where each file came from:

- `sme_*.bin`: web read-back replies from a real Hx printer (firmware 69.75.10, web page V.72.5),
  captured on 29 Oct 2026 and sanitized. The line breaks (CRLF and blank lines), the single 0xFF byte
  in AMPM and the space after `base64,` in each picture are as captured.
  - `sme_load_slot9.bin`: the slot 9 Load of a message built through Autopilot (verbose FCDOC).
  - `sme_load_slot0_compact.bin`: a slot 0 Load answered with slot 1, made by another tool (compact FCDOC).
  - `sme_save_stale.bin`: the reply SAVEMSG left in the printer's shared buffer.
  - `sme_load_slot9_empty.bin`: the Load of an empty slot.
  - `sme_thumbnail_slot9.bin`: a Thumbnail reply (its XID was chosen; the capture didn't record it).
  - `sme_load_other_client.bin`: another web client's leftover reply (TN 834854).
- `http_*.bin`: whole HTTP replies in the captured formats (Autopilot `0`, `7` and `None`, the 401
  login page, the login with its two cookies).
- `req_*.bin`: golden requests, written from this script's own copy of the request formats. A test
  checks that `hx_protocol`'s builders produce the same bytes.
- `assumed_sfl.xml`, `assumed_idn.xml`: the font list and identity files as the fake printer serves
  them. They were not captured: the format is assumed.

Synthetic parts: every picture (a white 1-bit 1024 x 160 BMP with the real header and one black
rectangle giving the captured black-pixel count), the cookie values, the two HTML pages and the two
assumed files. Gzipped bodies may differ between zlib builds, so tests compare them decompressed.

Nothing in this folder may hold a real address, UID, printer name, token, cookie or password. Use
only host 192.0.2.10, UID 1109 and password 123321 (the Autopilot manual's own example).
"""


FILES = {
    "sme_load_slot9.bin": slot9_load,
    "sme_load_slot0_compact.bin": lambda: compact_load("326548", "6"),
    "sme_save_stale.bin": lambda: prv_only("Save", "", "", 2564, "STRIPES TEST", BMP_2564),
    "sme_load_slot9_empty.bin": lambda: prv_only("Load", "326548", "2", 0, "", BMP_0),
    "sme_thumbnail_slot9.bin": lambda: prv_only("Thumbnail", "326548", "3", 2564, "STRIPES TEST", BMP_2564),
    "sme_load_other_client.bin": lambda: compact_load("834854", "2"),
    "http_autopilot_ok.bin": lambda: autopilot_reply(b"0"),
    "http_autopilot_busy.bin": lambda: autopilot_reply(b"7"),
    "http_autopilot_none.bin": lambda: autopilot_reply(b"None"),
    "http_unauthorized.bin": unauthorized,
    "http_login_ok.bin": login_ok,
    "req_autopilot_prin.bin": lambda: autopilot_request("<PRIN/>"),
    "req_autopilot_print1.bin": lambda: autopilot_request("<PRINT>1</PRINT>"),
    "req_login.bin": login_request,
    "req_load_slot9.bin": lambda: sme_request(
        "/simple_msg.html?sme", b"<SM><TYPE>Load</TYPE><TN>326548</TN><XID>4</XID><MSNO>9</MSNO></SM>"),
    "req_repeat.bin": lambda: sme_request("/simple_msg.html?sme_repeat", b"<SM>RepeatRequest</SM>"),
    "req_get_fonts.bin": lambda: file_request("/sfl.xml"),
    "assumed_sfl.xml": assumed_sfl,
    "assumed_idn.xml": assumed_idn,
    "README.md": lambda: README.encode("utf-8"),
}


def main():
    for name, make in FILES.items():
        data = make()
        with open(os.path.join(HERE, name), "wb") as f:
            f.write(data)
        print(f"{name}: {len(data)} bytes")


if __name__ == "__main__":
    main()
