"""Tests for hx_protocol and hx_fake: the printer client, the parser, the fakes. Run: python -m unittest -v"""

import base64
import gzip
import os
import re
import secrets
import socket
import struct
import sys
import tempfile
import threading
import time
import types
import unittest
from datetime import datetime
from unittest import mock

import hx_fake
import hx_protocol as hp
from hx_core import SYSTEM_CLOCK, FakeClock, Msg
from hx_fake import FakePrinter, FakeSntp, bmp_1bit
from hx_model import (ALLOWED_WRITE_TAGS, FORBIDDEN_QUERY_TAGS, STANDARD_FONTS, TIMINGS, AuthFailed, BadReply, Command, CommandRefused,
                      ForbiddenCommand, NoReply, Printer, PrinterBusy, Timings, WebReadFailed, WrongPrinter)
from hx_protocol import ClientPool, GateRegistry, PrinterClient

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "testdata", "hx")
TOKEN = "Yzg4MzdiMjNmZjhhYWE4YTJkZGU5MTU0NzNjZTA5OTE="     # the manual's example: password 123321
COOKIES = {"Access_token_1109": "fedcba9876543210fedcba9876543210",
           "Auth_token_1109": "0123456789abcdef0123456789abcdef"}
EDT = lambda utc: -4 * 3600.0           # a fixed offset, so the fake's date doesn't depend on the host


def fixture(name):
    with open(os.path.join(FIXTURES, name), "rb") as f:
        return f.read()


def placeholders(body):
    """The BPIX value and the picture replaced: the fake draws boxes, the capture had text."""
    body = re.sub(rb"<BPIX>\d+</BPIX>", b"<BPIX>#</BPIX>", body)
    return re.sub(rb"<BMP>[^<]*</BMP>", b"<BMP>#</BMP>", body)


def start(test, **kw):
    fake = FakePrinter(**kw).start()
    test.addCleanup(fake.stop)
    return fake


def client(fake, clock=SYSTEM_CLOCK, *, timings=TIMINGS, uid=1109, password="123321", gates=None):
    c = PrinterClient("127.0.0.1", uid, hp.make_token(password), port=fake.port, clock=clock,
                      gates=gates or GateRegistry(), timings=timings)
    c.label = "Printer 1"
    return c


def raw(fake, request, *, max_body=hp.MAX_SME_REPLY):
    return hp.http_exchange("127.0.0.1", fake.port, request, timeout_s=2.0, max_body=max_body)


def raw_autopilot(fake, xml, *, uid=1109, token=TOKEN):
    return raw(fake, hp.autopilot_request(uid, token, xml), max_body=hp.MAX_AUTOPILOT_REPLY)


def raw_login(fake):
    reply = hp.http_exchange("127.0.0.1", fake.port, hp.login_request("127.0.0.1", fake.port, hp.password_md5(TOKEN)),
                             timeout_s=2.0, max_body=hp.MAX_LOGIN_REPLY, allow_cut_short=True)
    return reply.cookies()


def kinds(fake):
    with fake.lock:
        return [r["kind"] for r in fake.requests]


def tags(fake):
    with fake.lock:
        return [r["tag"] for r in fake.requests]


def in_thread(work):
    """Run work() on a thread; join() gives what it returned or raised."""
    out = {}

    def run():
        try:
            out["value"] = work()
        except BaseException as e:
            out["error"] = e
    t = threading.Thread(target=run, daemon=True)
    t.start()

    def join():
        t.join(5.0)
        if "error" in out:
            raise out["error"]
        return out.get("value")
    return join


def one_shot_server(test, reply_bytes, *, close_early=False):
    """A server that answers one connection with reply_bytes (or closes without a reply)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    sock.listen(1)
    test.addCleanup(sock.close)

    def serve():
        try:
            conn, _ = sock.accept()
        except OSError:
            return
        with conn:
            conn.settimeout(2.0)
            data = b""
            try:
                while b"\r\n\r\n" not in data:
                    chunk = conn.recv(65536)
                    if not chunk:
                        return
                    data += chunk
                if not close_early:
                    conn.sendall(reply_bytes)
            except OSError:
                pass
    threading.Thread(target=serve, daemon=True).start()
    return sock.getsockname()[1]


def build_slot9(c):
    """A two-item message in slot 9 through command()."""
    c.command(Command.set("CRTMSG", "STRIPES TEST"))
    c.command(Command.block("CTEXT", [("OBJID", 10001), ("POS", "10,10"), ("MARG", "0,0"), ("FONTIND", 1),
                                      ("SIZE", 3), ("TXT", "STRIPES TEST 123")]))
    c.command(Command.block("CTDAT", [("OBJID", 10002), ("XRID", 10001), ("YRID", 10001), ("SXAS", 1),
                                      ("SYAS", 0), ("MARG", "0,0"), ("FONTIND", 2), ("SIZE", 4),
                                      ("PREF", "Packed on "), ("FRMT", "M/D/y"), ("DAY", 0), ("MON", 0), ("YEAR", 0)]))
    c.command(Command.block("CPPARM", [("ENCST", 0), ("DELAY", 480000), ("PRNSPD", 2400), ("RESMOD", 1)]))
    c.command(Command.set("SAVEMSG", 9))


# --------------------------------------------------------------------------
# Token, scrambling, keys, ClientPool
# --------------------------------------------------------------------------

class TokenTests(unittest.TestCase):
    def test_manual_example(self):
        self.assertEqual(hp.make_token("123321"), TOKEN)
        self.assertEqual(hp.password_md5(TOKEN), "c8837b23ff8aaa8a2dde915473ce0991")

    def test_round_trip(self):
        for key in (hp.DEV_KEY, secrets.token_bytes(32)):
            stored = hp.scramble(TOKEN, key)
            self.assertEqual(hp.unscramble(stored, key), TOKEN)
            self.assertNotEqual(hp.scramble(TOKEN, key), stored)          # a new nonce each time
            self.assertTrue(stored.startswith(f"hx1:{hp.key_id(key)}:"))
            self.assertNotIn(TOKEN, stored)

    def test_another_key_and_damage(self):
        stored = hp.scramble(TOKEN, hp.DEV_KEY)
        with self.assertRaises(AuthFailed) as e:
            hp.unscramble(stored, secrets.token_bytes(32))
        self.assertIn("different Stripes build", str(e.exception))
        prefix, payload = stored.rsplit(":", 1)
        damaged = []
        for i in (0, len(payload) // 2, len(payload) - 1):     # the last character too
            c = "A" if payload[i] != "A" else "B"
            damaged.append(f"{prefix}:{payload[:i]}{c}{payload[i + 1:]}")
        for bad in damaged + ["", "hx1:" + hp.key_id(hp.DEV_KEY), f"hx2:{hp.key_id(hp.DEV_KEY)}:{payload}",
                              f"{prefix}:{payload[:20]}", f"{prefix}:{payload}*"]:
            with self.assertRaises(AuthFailed, msg=bad) as e:
                hp.unscramble(bad, hp.DEV_KEY)
            self.assertIn("damaged", str(e.exception))
        with self.assertRaises(AuthFailed):
            hp.password_md5("abc")
        with self.assertRaises(AuthFailed):
            hp.password_md5(base64.b64encode(b"C8837B23FF8AAA8A2DDE915473CE0991").decode())   # not lowercase
        with self.assertRaises(ValueError):
            hp.scramble("abc", hp.DEV_KEY)

    def test_release_key(self):
        with mock.patch.dict(sys.modules, {"hx_key": None}):
            self.assertEqual(hp.release_key(), hp.DEV_KEY)
            self.assertTrue(hp.is_development_key(hp.release_key()))
        short = types.ModuleType("hx_key")
        short.KEY = b"short"
        with mock.patch.dict(sys.modules, {"hx_key": short}):
            with self.assertRaises(RuntimeError):
                hp.release_key()
        good = types.ModuleType("hx_key")
        good.KEY = b"k" * 32
        with mock.patch.dict(sys.modules, {"hx_key": good}):
            self.assertEqual(hp.release_key(), b"k" * 32)
            self.assertFalse(hp.is_development_key(hp.release_key()))
        self.assertRegex(hp.key_id(hp.DEV_KEY), r"^[0-9a-f]{8}$")
        self.assertNotEqual(hp.key_id(hp.DEV_KEY), hp.key_id(b"k" * 32))

    def test_client_pool(self):
        pool = ClientPool(key=hp.DEV_KEY, gates=GateRegistry())
        record = Printer(id="pr_0000000001", name="Printer 1", host="192.0.2.10", uid=1109,
                         token=hp.scramble(TOKEN, hp.DEV_KEY))
        with self.assertRaises(AuthFailed) as e:
            pool.client(Printer(id="pr_0000000002", name="Printer 2", host="192.0.2.11", uid=1109))
        self.assertEqual(e.exception.msg.text, "No password is saved for {printer}.")
        first = pool.client(record)
        self.assertIs(pool.client(record), first)
        self.assertEqual(first.label, "Printer 1")
        self.assertEqual((first.host, first.port, first.uid), ("192.0.2.10", 80, 1109))
        record.token = hp.scramble(TOKEN, hp.DEV_KEY)
        second = pool.client(record)
        self.assertIsNot(second, first)
        record.name = "Line 1 printer"
        self.assertEqual(pool.client(record).label, "Line 1 printer")
        record.token = hp.scramble(TOKEN, secrets.token_bytes(32))
        with self.assertRaises(AuthFailed) as e:
            pool.client(record)
        self.assertEqual(e.exception.msg.text, "{printer}: {problem}")
        self.assertIsInstance(e.exception.msg.values["problem"], Msg)
        self.assertIn("different Stripes build", str(e.exception))


# --------------------------------------------------------------------------
# Commands and the whitelist
# --------------------------------------------------------------------------

VALID_WRITES = [
    Command.set("CRTMSG", "EXCO1-PKO01-MN6D-L9"),
    Command.set("CRTMSG", ""),
    Command.block("CTEXT", [("OBJID", 10002), ("XRID", 10001), ("YRID", 10001), ("SXAS", 1), ("SYAS", 0),
                            ("MARG", "0,0"), ("FONTIND", 2), ("SIZE", 4), ("TXT", "Product of USA and Canada")]),
    Command.block("CTDAT", [("OBJID", 10001), ("POS", "0,0"), ("MARG", "0,0"), ("FONTIND", 2), ("SIZE", 4),
                            ("PREF", "Packed on "), ("POSF", "."), ("FRMT", "M/D/y"), ("DAY", 0), ("MON", 0),
                            ("YEAR", 0)]),
    Command.block("CPPARM", [("ENCST", 0), ("DELAY", 480000), ("PRNSPD", 2400), ("ROTAT", 0), ("MIRR", 0),
                             ("RESMOD", 0), ("BOLD", 1), ("WDTH", 1), ("QUANT", 1), ("REVRS", 0), ("AUTREP", 0)]),
    Command.set("SAVEMSG", 9),
    Command.set("SAVEMSG", 10),
    Command.set("PRINTMSG", 10),
    Command.set("PRINT", 0),
    Command.set("PRINT", 1),
    Command.block("TD", [("DAY", 29), ("MNTH", 10), ("YEAR", 2026), ("HOUR", 8), ("MIN", 45), ("DTRH", 0),
                         ("DTRM", 0)]),
    Command.block("TD", [("HOUR", 24), ("MIN", 0)]),
]

REFUSED_WRITES = (
    [Command.set(tag, 1) for tag in hp.PRINTER_WIDE_WRITES]
    + [Command.block(tag, [("POS", "0,0")]) for tag in hp.UNUSED_WRITES]
    + [Command.set("SAVEMSG", 1), Command.set("PRINTMSG", 8), Command.set("PRINT", 2),
       Command.block("CPPARM", [("SLANT", 0)]),
       Command.block("CTEXT", [("SIZE", 11)]),
       Command.block("CTEXT", [("LANG", 4)]),
       Command("PRINT", "<PRINT>1</PRINT><PRINT>0</PRINT>", True),     # a smuggled second command
       Command.query("PRINT"),
       Command.set("CRTMSG", "A" * 21),
       Command.block("CTEXT", [("SIZE", 3), ("SIZE", 4)]),            # a repeated child
       Command.block("CTEXT", [("TXT", "x" * 251)]),
       Command.block("CTDAT", [("FRMT", "")]),
       Command.block("CPPARM", [("PRNSPD", 2501)]),
       Command.block("CPPARM", [("DELAY", 120_000_001)]),
       Command.block("TD", [("MNTH", 13)]),
       Command.block("TD", [("SEC", 0)]),
       Command("CTEXT", "<CTEXT><TXT>a > b</TXT></CTEXT>", True),     # not escaped
       Command("CTEXT", "<CTEXT><TXT>a\nb</TXT></CTEXT>", True),      # a control character
       Command("CTEXT", "<CTEXT>text<TXT>a</TXT></CTEXT>", True),
       Command("SETPPARAM", "<SETPPARAM><PRNSPD>1</PRNSPD></SETPPARAM>", True)]
)


class CommandTests(unittest.TestCase):
    def test_escaping(self):
        cmd = Command.block("CTEXT", [("TXT", "R&D <1>")])
        self.assertEqual(cmd.xml, "<CTEXT><TXT>R&amp;D &lt;1&gt;</TXT></CTEXT>")
        self.assertEqual(cmd.xml, f"<CTEXT><TXT>{hp.xml_escape('R&D <1>')}</TXT></CTEXT>")
        self.assertEqual(Command.set("CRTMSG", "a&b").xml, f"<CRTMSG>{hp.xml_escape('a&b')}</CRTMSG>")
        hp.check_write(cmd)

    def test_whitelist(self):
        self.assertEqual(sorted({c.tag for c in VALID_WRITES}), sorted(ALLOWED_WRITE_TAGS))
        for cmd in VALID_WRITES:
            hp.check_write(cmd)
        for cmd in REFUSED_WRITES:
            with self.assertRaises(ForbiddenCommand, msg=cmd.xml) as e:
                hp.check_write(cmd)
            self.assertEqual(e.exception.tag, cmd.tag)
            self.assertEqual(str(e.exception), f"Stripes tried to send a command it never sends: {cmd.tag}.")

    def test_refused_before_any_request(self):
        fake = start(self)
        c = client(fake, FakeClock())
        for cmd in REFUSED_WRITES:
            with self.assertRaises(ForbiddenCommand, msg=cmd.xml):
                c.command(cmd)
        for tag in ("CPPARM", "ODPARAM", "RS232", "BEAC", "LOT1", "SETPPARAM", "YEAR", "INSH"):
            with self.assertRaises(ForbiddenCommand, msg=tag):
                c.query(tag)
            with self.assertRaises(ForbiddenCommand, msg=tag):
                c.read(tag)
        for tag in FORBIDDEN_QUERY_TAGS:
            self.assertNotIn(tag, hp.QUERY_SPECS)
        self.assertEqual(fake.requests, [])
        self.assertEqual(fake.violations, [])

    def test_no_editor_builder(self):
        with self.assertRaises(ValueError):
            hp.file_request("192.0.2.10", 80, COOKIES, hp.EDITOR_PATH)
        with self.assertRaises(ValueError):
            hp.file_request("192.0.2.10", 80, COOKIES, "/uss.xml")


# --------------------------------------------------------------------------
# Reply checks
# --------------------------------------------------------------------------

class ReplyTests(unittest.TestCase):
    def test_table(self):
        ok = [("PRIN", "1", 1), ("PRIN", "0", 0), ("MSGNAM", "None", None), ("MSGNAM", "EXCO1-PKO01-MN6D-L9",
              "EXCO1-PKO01-MN6D-L9"), ("DATE", "29/10/2026", "2026-10-29"), ("DATE", "1/2/2027", "2027-02-01"),
              ("TIME", "21:11:41", "21:11:41"), ("TIME", "8:05:00", "08:05:00"), ("MSCNT", "436336", 436336),
              ("DELAY", "284", 284), ("HXUI", "1109", 1109), ("HXNA", "Printer 1", "Printer 1"),
              ("INKT", "None", None), ("PHTP", "-5", -5), ("INKL", "4", 4)]
        for tag, text, value in ok:
            self.assertEqual(hp.check_reply(tag, text), value, (tag, text))
        bad = [("PRIN", "2"), ("MSGNAM", "1"), ("MSGNAM", "A" * 21), ("DATE", "31/02/2026"), ("DATE", "1"),
               ("DATE", "29/10/1999"), ("TIME", "24:00:00"), ("TIME", "12:60:00"), ("INKL", "5"), ("HXUI", "0"),
               ("PRIN", "None"), ("MSCNT", "1.5"), ("MSCNT", ""), ("INKT", "5"), ("INKT", "a\x01b"),
               ("PHTP", "101"), ("HXUI", "12345678901")]
        for tag, text in bad:
            with self.assertRaises(BadReply, msg=(tag, text)) as e:
                hp.check_reply(tag, text, printer="Printer 1")
            self.assertEqual((e.exception.tag, e.exception.reply), (tag, text[:40]))
        with self.assertRaises(PrinterBusy):
            hp.check_reply("PRIN", "7")
        with self.assertRaises(BadReply) as e:
            hp.check_reply("MSGNAM", "B" * 60, printer="Printer 1")
        self.assertEqual(len(e.exception.reply), 40)
        self.assertEqual(str(e.exception), f"Printer 1 gave an answer to MSGNAM that doesn't make sense: {'B' * 40}")

    def test_codes_never_values(self):
        for tag in hp.QUERY_SPECS:
            with self.assertRaises(BadReply, msg=tag):
                hp.check_reply(tag, "-1")
            for code in ("6", "7"):
                with self.assertRaises(PrinterBusy, msg=(tag, code)):
                    hp.check_reply(tag, code)

    def test_decoding(self):
        self.assertEqual(hp.decode_text(b"\xff"), "\xff")
        self.assertEqual(hp.decode_text("ñ".encode()), "ñ")
        fake = start(self)
        self.assertEqual(fake.hx_name, " Printer 1")
        self.assertEqual(client(fake, FakeClock()).read("HXNA"), "Printer 1")
        self.assertEqual(fake.violations, [])

    def test_clock_from(self):
        self.assertEqual(hp.clock_from("2026-10-29", "08:45:00"), datetime(2026, 10, 29, 8, 45))


# --------------------------------------------------------------------------
# The SME parser
# --------------------------------------------------------------------------

def common_pairs(txt, fontind, size, pos, bound, pref, objtyp, objid):
    return [("LANG", "4"), ("TXT", txt), ("FONTIND", fontind), ("SIZE", size), ("POS", pos), ("BOUND", bound),
            ("POSF", ""), ("PREF", pref), ("ROTAT", "0"), ("WCOEFF", "0"), ("GROUP", "0"), ("OBJTYP", objtyp),
            ("COLR", "0"), ("XRID", "0"), ("YRID", "0"), ("SXAS", "0"), ("SYAS", "0"), ("OBJID", objid),
            ("RP", "0"), ("MARG", "0,0")]


class ParserTests(unittest.TestCase):
    def parse(self, name):
        return hp.parse_sme_reply(fixture(name))

    def test_slot9(self):
        r = self.parse("sme_load_slot9.bin")
        self.assertEqual((r.type, r.tn, r.xid, r.msno), ("Load", "326548", "4", 9))
        d = r.design
        self.assertEqual((d.slot, d.name, d.empty, d.width, d.height), (9, "STRIPES TEST", False, 256, 160))
        self.assertEqual([i.tag for i in d.items], ["CTEXT", "CTDAT", "CLOT"])
        self.assertEqual(d.items[0].pairs, common_pairs("STRIPES TEST 123", "1", "2", "10,10", "10,10,218,33", "", "0",
                                                        "10001"))
        ctdat = d.items[1].pairs
        self.assertEqual(ctdat[:11], [("FRMT", "Y/M/D"), ("AMPM", "\xff"), ("DAY", "0"), ("DWEEK", "0"),
                                      ("HOUR", "0"), ("HORFLG", "1"), ("LANG", "17742"), ("MIN", "0"), ("MON", "0"),
                                      ("SEC", "0"), ("YEAR", "0")])
        self.assertEqual(ctdat[11:], common_pairs("2026/10/29", "1", "2", "10,60", "10,60,192,83", "PKD:", "1", "1"))
        self.assertEqual(len(ctdat), 31)
        self.assertEqual(d.items[1].value("LANG"), "4")
        self.assertEqual(d.items[2].pairs, [("INDX", "1")] + common_pairs("", "2", "3", "10,100", "10,100,66,136",
                                                                         "LOT:", "1", "2"))
        self.assertEqual(list(d.settings.items()), list(hx_fake.FAKE_DEFAULT_SETTINGS))
        self.assertEqual(len(d.settings), 24)
        self.assertEqual(d.black_pixels, 2564)
        self.assertEqual(len(d.preview_bmp), hp.PREVIEW_BYTES)
        self.assertEqual((d.read_at, d.tn, d.xid), (0.0, "326548", 4))

    def test_compact(self):
        r = self.parse("sme_load_slot0_compact.bin")
        self.assertEqual((r.type, r.tn, r.xid, r.msno), ("Load", "326548", "6", 1))
        d = r.design
        self.assertEqual((d.slot, d.name, d.empty, d.width, d.height), (1, "", False, None, 160))
        self.assertEqual(len(d.items), 1)
        self.assertEqual(d.items[0].tag, "CTDAT")
        self.assertEqual(d.items[0].pairs, [
            ("OBJID", "1"), ("POS", "0,0"), ("PREF", "Packed: "), ("FRMT", "M/D/y"), ("DAY", ""), ("MON", ""),
            ("YEAR", ""), ("POSF", ""), ("FONTIND", "1"), ("SIZE", "4"), ("MARG", "0,0"), ("RP", "0"), ("LANG", "0")])
        self.assertEqual(list(d.settings.items()), [
            ("ROTAT", "0"), ("MIRR", "0"), ("REVRS", "0"), ("AUTREP", "0"), ("DELAY", "0"), ("DLYMM", "0"),
            ("PRNSPD", "2500"), ("BOLD", "1"), ("WDTH", "1"), ("QUANT", "1"), ("QDELAY", "0"), ("QDLYMM", "0"),
            ("RESMOD", "0"), ("RESNAM", "UltraFine"), ("ENCST", "0"), ("ENCSCL", "100")])
        self.assertEqual(d.black_pixels, 3492)

    def test_other_replies(self):
        r = self.parse("sme_save_stale.bin")
        self.assertEqual((r.type, r.tn, r.xid, r.msno, r.design), ("Save", "", "", 9, None))
        r = self.parse("sme_thumbnail_slot9.bin")
        self.assertEqual((r.type, r.tn, r.xid, r.msno, r.design), ("Thumbnail", "326548", "3", 9, None))
        r = self.parse("sme_load_slot9_empty.bin")
        self.assertEqual((r.type, r.tn, r.xid, r.msno), ("Load", "326548", "2", 9))
        d = r.design
        self.assertEqual((d.slot, d.name, d.empty, d.items, d.settings, d.black_pixels), (9, "", True, [], {}, 0))
        self.assertEqual(d.preview_bmp, bmp_1bit([]))
        r = self.parse("sme_load_other_client.bin")
        self.assertEqual((r.type, r.tn, r.xid, r.msno), ("Load", "834854", "2", 1))
        compact = self.parse("sme_load_slot0_compact.bin").design
        compact.tn, compact.xid = r.design.tn, r.design.xid
        self.assertEqual(r.design, compact)

    def test_robustness(self):
        body = fixture("sme_load_slot9.bin")
        self.assertEqual(hp.parse_sme_reply(body.replace(b"\r\n", b"\n")).design, hp.parse_sme_reply(body).design)
        no_space = body.replace(b"base64, ", b"base64,")
        self.assertEqual(hp.parse_sme_reply(no_space).design, hp.parse_sme_reply(body).design)
        self.assertEqual(hp.parse_sme_reply(b"\xef\xbb\xbf \r\n" + body + b"\r\n\r\n").design.name, "STRIPES TEST")
        self.assertEqual(hp.parse_sme_reply(b'<?xml version="1.0"?>\r\n' + body).design.name, "STRIPES TEST")
        self.assertEqual(bmp_1bit([(10, 10, 651, 14)]), hp.parse_sme_reply(body).design.preview_bmp)
        self.assertEqual(bmp_1bit([(0, 0, 873, 4)]), self.parse("sme_load_slot0_compact.bin").design.preview_bmp)
        # Spaces in values are kept, and no entity is decoded
        r = hp.parse_sme_reply(body.replace(b"<TXT>STRIPES TEST 123</TXT>", b"<TXT> R&amp;D &lt;1&gt; </TXT>"))
        self.assertEqual(r.design.items[0].value("TXT"), " R&amp;D &lt;1&gt; ")
        # A nested container's leaves, and a self-closing leaf
        r = hp.parse_sme_reply(body.replace(b"<RP>0</RP>\r\n<MARG>0,0</MARG>\r\n</CTEXT>",
                                            b"<RP/>\r\n<MARG>0,0</MARG>\r\n<X><Y>1</Y></X>\r\n</CTEXT>"))
        self.assertEqual(r.design.items[0].pairs[-3:], [("RP", ""), ("MARG", "0,0"), ("X.Y", "1")])

    def test_failures(self):
        body = fixture("sme_load_slot9.bin")
        empty = fixture("sme_load_slot9_empty.bin")
        uri = re.search(rb"<BMP>([^<]*)</BMP>", body).group(1)
        narrow = b"data:image/bmp;base64, " + base64.b64encode(bmp_1bit([], 800))
        four_bit = bytearray(bmp_1bit([]))
        four_bit[28] = 4
        bad = [
            body[:len(body) // 2],
            body.replace(uri, narrow),
            body.replace(uri, b"data:image/bmp;base64, " + base64.b64encode(bytes(four_bit))),
            body.replace(uri, b"data:image/png;base64, " + uri[23:]),
            body.replace(uri, uri[:-8] + b"!!!!====" ),
            empty.replace(b"<PRV>\r\n", b"").replace(b"</PRV>\r\n", b"").replace(b"<BPIX>0</BPIX>\r\n<MSGNAM>"
                                                                                 b"</MSGNAM>\r\n", b""),
            body.replace(b"<PRV>\r\n<BPIX>2564</BPIX>\r\n<MSGNAM>STRIPES TEST</MSGNAM>",
                         b"<PRV>\r\n<BPIX>2564</BPIX>\r\n<MSGNAM>OTHER</MSGNAM>"),
            body.replace(b"<CTEXT>\r\n<LANG>", b"<CTEXT>\r\nstray\r\n<LANG>"),
            body.replace(b"<MSNO>9</MSNO>", b"<MSNO>x</MSNO>"),
            body.replace(b"<BPIX>2564</BPIX>", b"<BPIX>-1</BPIX>"),
            body.replace(b"<W>256</W>", b"<W>wide</W>"),
            body.replace(b"</CTEXT>", b"</CTXT>"),
            body.replace(b"<TYPE>Load</TYPE>\r\n", b""),
            body + b"<SM></SM>",
            body.replace(b"<TXT>STRIPES TEST 123</TXT>", b"<TXT>a < b</TXT>"),
            b"<html><body>login</body></html>",
            b"",
        ]
        for i, b in enumerate(bad):
            with self.assertRaises(WebReadFailed, msg=i) as e:
                hp.parse_sme_reply(b)
            self.assertEqual(e.exception.msg.text, "{printer} sent back a message Stripes doesn't recognise.")

    def test_classify(self):
        tn = "326548"
        slot9 = fixture("sme_load_slot9.bin")
        self.assertEqual(hp.classify_reply(slot9, tn=tn, xid=4), "match")
        self.assertEqual(hp.classify_reply(slot9, tn=tn, xid=5), "stale")
        self.assertEqual(hp.classify_reply(slot9, tn=tn, xid=3), "odd")
        self.assertEqual(hp.classify_reply(fixture("sme_save_stale.bin"), tn=tn, xid=5), "stale")
        self.assertEqual(hp.classify_reply(fixture("sme_load_other_client.bin"), tn=tn, xid=5), "foreign")
        self.assertEqual(hp.classify_reply(fixture("sme_thumbnail_slot9.bin"), tn=tn, xid=3), "odd")
        self.assertEqual(hp.classify_reply(b"7", tn=tn, xid=1), "busy")
        self.assertEqual(hp.classify_reply(b" 6\r\n", tn=tn, xid=1), "busy")
        self.assertEqual(hp.classify_reply(b"<html>", tn=tn, xid=1), "odd")
        stripes_tn = hp.new_tn()
        other_pc = slot9.replace(b"<TN>326548</TN>", f"<TN>{stripes_tn}</TN>".encode())
        self.assertEqual(hp.classify_reply(other_pc, tn=tn, xid=4), "stale")
        self.assertEqual(hp.classify_reply(slot9.replace(b"<XID>4</XID>", b"<XID>x</XID>"), tn=tn, xid=4), "odd")
        for _ in range(100):
            new = hp.new_tn()
            self.assertTrue(hp.is_stripes_tn(new), new)
            self.assertTrue(100_049 <= int(new) <= 999_918)
        self.assertFalse(hp.is_stripes_tn("326548"))
        self.assertFalse(hp.is_stripes_tn("834854"))
        self.assertFalse(hp.is_stripes_tn("000042"))
        self.assertEqual(hp.sme_envelope(fixture("sme_save_stale.bin")), ("Save", "", "", "9"))
        self.assertEqual(hp.sme_envelope(b"<SM><TN>1</TN></SM>"), (None, "1", "", ""))
        self.assertIsNone(hp.sme_envelope(b"<SM><TYPE>Load</TYPE>"))

    def test_fonts_and_firmware_files(self):
        self.assertEqual(hp.parse_fonts(fixture("assumed_sfl.xml")), list(STANDARD_FONTS))
        self.assertEqual(hp.parse_fonts(b"<SFL><FON1> A </FON1><FON2>B</FON2><FON4>D</FON4></SFL>"), ["A", "B"])
        self.assertEqual(hp.parse_fonts(b"<FON1>A</FON1><FON2></FON2><FON3>C</FON3>"), ["A"])
        for body in (b"", b"<SFL></SFL>", b"<FON2>B</FON2>"):
            with self.assertRaises(BadReply):
                hp.parse_fonts(body)
        self.assertEqual(hp.parse_firmware(fixture("assumed_idn.xml")), "69.75.10")
        self.assertEqual(hp.parse_firmware(b"<IDN><VER>1</VER><FWVER> 70.1 </FWVER></IDN>"), "70.1")
        self.assertEqual(hp.parse_firmware(b"<FW>" + b"9" * 50 + b"</FW>"), "9" * 40)
        self.assertIsNone(hp.parse_firmware(b"<IDN><HXUI>1109</HXUI></IDN>"))


# --------------------------------------------------------------------------
# Request bytes and the HTTP transport
# --------------------------------------------------------------------------

class RequestTests(unittest.TestCase):
    def test_golden(self):
        calls = {
            "req_autopilot_prin.bin": hp.autopilot_request(1109, TOKEN, "<PRIN/>"),
            "req_autopilot_print1.bin": hp.autopilot_request(1109, TOKEN, "<PRINT>1</PRINT>"),
            "req_login.bin": hp.login_request("192.0.2.10", 80, "c8837b23ff8aaa8a2dde915473ce0991"),
            "req_load_slot9.bin": hp.sme_request("192.0.2.10", 80, COOKIES, repeat=False,
                                                 body=hp.load_body("326548", 4, 9)),
            "req_repeat.bin": hp.sme_request("192.0.2.10", 80, COOKIES, repeat=True, body=b"<SM>RepeatRequest</SM>"),
            "req_get_fonts.bin": hp.file_request("192.0.2.10", 80, COOKIES, "/sfl.xml"),
        }
        for name, request in calls.items():
            self.assertEqual(request, fixture(name), name)
        self.assertEqual(len(hp.autopilot_request(1109, TOKEN, "<PRIN/>").partition(b"\r\n\r\n")[2]), 118)
        self.assertEqual(len(hp.autopilot_request(1109, TOKEN, "<PRINT>0</PRINT>").partition(b"\r\n\r\n")[2]), 127)
        self.assertEqual(len(hp.load_body("326548", 4, 9)), 67)
        # Cookies go Access first whatever the dict order; the port shows when it isn't 80
        backwards = {"Auth_token_1109": "b", "Access_token_1109": "a"}
        self.assertIn(b"Cookie: Access_token_1109=a; Auth_token_1109=b\r\n",
                      hp.sme_request("192.0.2.10", 80, backwards, repeat=False, body=b"x"))
        self.assertIn(b"Host: 192.0.2.10:8080\r\n", hp.login_request("192.0.2.10", 8080, "0" * 32))

    def test_on_the_wire(self):
        fake = start(self)
        fake.web_first_reply_fresh = True
        c = client(fake, FakeClock())
        c.read("PRIN")
        c.load_slot(9)
        with fake.lock:
            requests = list(fake.requests)
        self.assertEqual(requests[0]["raw"], hp.autopilot_request(1109, TOKEN, "<PRIN/>"))
        load = [r for r in requests if r["kind"] == "sme"][0]
        self.assertEqual(load["raw"], hp.sme_request("127.0.0.1", fake.port, c._cookies, repeat=False,
                                                     body=hp.load_body(c.tn, 1, 9)))
        self.assertEqual(load["tag"], "Load")
        self.assertEqual(fake.violations, [])

    def test_http_replies(self):
        port = one_shot_server(self, fixture("http_autopilot_ok.bin"))
        r = hp.http_exchange("127.0.0.1", port, b"POST / HTTP/1.1\r\n\r\n", timeout_s=2.0, max_body=4096)
        self.assertEqual((r.status, r.body, r.complete), (200, b"0", True))
        self.assertEqual(r.header("content-type"), "text/plain")
        self.assertEqual(r.headers[0], ("Server", "MapJet/v1.1"))
        port = one_shot_server(self, fixture("http_unauthorized.bin"))
        r = hp.http_exchange("127.0.0.1", port, b"GET / HTTP/1.1\r\n\r\n", timeout_s=2.0, max_body=4096)
        self.assertEqual((r.status, r.body), (401, hx_fake.LOGIN_HTML))
        port = one_shot_server(self, fixture("http_login_ok.bin"))
        r = hp.http_exchange("127.0.0.1", port, b"POST / HTTP/1.1\r\n\r\n", timeout_s=2.0,
                             max_body=hp.MAX_LOGIN_REPLY, allow_cut_short=True, gunzip=False)
        self.assertEqual(r.cookies(), COOKIES)
        self.assertEqual(gzip.decompress(r.body), hx_fake.MAIN_HTML)

    def test_http_failures(self):
        ok = b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\nNone"
        cases = [
            (b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n4\r\nNone\r\n0\r\n\r\n", BadReply, "HTTP framing"),
            (b"HTTP/2 200 OK\r\n\r\n", BadReply, "HTTP framing"),
            (b"HTTP/1.1 200 OK\r\nBroken header\r\n\r\n", BadReply, "HTTP framing"),
            (b"HTTP/1.1 200 OK\r\nX: " + b"y" * 17000 + b"\r\n\r\n", BadReply, "HTTP framing"),
            (b"HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\n" + b"x" * 5000, BadReply, "reply too large"),
            (b"HTTP/1.1 200 OK\r\n\r\n" + b"x" * 5000, BadReply, "reply too large"),
            (b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 4\r\n\r\nNone", BadReply, "bad gzip"),
            (b"HTTP/1.1 200 OK\r\nContent-Length: 10\r\n\r\nNone", NoReply, None),      # closed short
            (b"HTTP/1.1 200 OK\r\nContent", NoReply, None),                              # closed in the headers
        ]
        for reply, error, text in cases:
            port = one_shot_server(self, reply)
            with self.assertRaises(error, msg=reply[:60]) as e:
                hp.http_exchange("127.0.0.1", port, b"GET / HTTP/1.1\r\n\r\n", timeout_s=1.0, max_body=4096,
                                 label="Printer 1")
            if text:
                self.assertEqual(e.exception.reply, text)
        port = one_shot_server(self, b"", close_early=True)
        with self.assertRaises(NoReply) as e:
            hp.http_exchange("127.0.0.1", port, b"GET / HTTP/1.1\r\n\r\n", timeout_s=1.0, max_body=4096,
                             label="Printer 1")
        self.assertEqual(str(e.exception), "No reply from Printer 1.")
        # Cut short when allowed
        port = one_shot_server(self, b"HTTP/1.1 200 OK\r\nContent-Length: 5000\r\n\r\n" + b"x" * 5000)
        r = hp.http_exchange("127.0.0.1", port, b"GET / HTTP/1.1\r\n\r\n", timeout_s=1.0, max_body=100,
                             allow_cut_short=True)
        self.assertEqual((r.status, len(r.body), r.complete), (200, 100, False))
        port = one_shot_server(self, b"HTTP/1.1 200 OK\r\nContent-Length: 50\r\n\r\nabc")
        r = hp.http_exchange("127.0.0.1", port, b"GET / HTTP/1.1\r\n\r\n", timeout_s=1.0, max_body=100,
                             allow_cut_short=True)
        self.assertEqual((r.body, r.complete), (b"abc", False))
        port = one_shot_server(self, ok)
        self.assertEqual(hp.http_exchange("127.0.0.1", port, b"GET / HTTP/1.1\r\n\r\n", timeout_s=1.0,
                                          max_body=4).body, b"None")
        # Nothing listening
        free = socket.socket()
        free.bind(("127.0.0.1", 0))
        port = free.getsockname()[1]
        free.close()
        with self.assertRaises(NoReply):
            hp.http_exchange("127.0.0.1", port, b"x", timeout_s=0.3, max_body=4)

    def test_timeout(self):
        fake = start(self)
        fake.go_silent()
        started = time.monotonic()
        with self.assertRaises(NoReply):
            hp.http_exchange("127.0.0.1", fake.port, hp.autopilot_request(1109, TOKEN, "<PRIN/>"), timeout_s=0.1,
                             max_body=4096)
        self.assertLess(time.monotonic() - started, 0.5)


# --------------------------------------------------------------------------
# Autopilot through PrinterClient
# --------------------------------------------------------------------------

class AutopilotTests(unittest.TestCase):
    def test_read(self):
        fake = start(self)
        c = client(fake, FakeClock())
        self.assertEqual(c.read("PRIN"), 1)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(c.query("MSGNAM"), "None")
        self.assertIsNone(c.read("MSGNAM"))
        self.assertEqual(c.read("PMCNT"), 100000)
        self.assertEqual((c.nonsense, c.busy_replies), (0, 0))
        self.assertIsNotNone(c.last_request_at)
        self.assertEqual(fake.violations, [])

    def test_authentication(self):
        fake = start(self)
        c = client(fake, FakeClock(), password="wrong")
        with self.assertRaises(AuthFailed) as e:
            c.read("PRIN")
        self.assertEqual(len(fake.requests), 1)
        self.assertIn("refused the password", str(e.exception))
        c = client(fake, FakeClock(), uid=1110)        # another printer at this address
        with self.assertRaises(AuthFailed):
            c.command(Command.set("PRINT", 1))
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.violations, [])

    def test_idle_drop(self):
        clock = FakeClock()
        fake = start(self, clock=clock, drop_after_idle_s=3.0)
        c = client(fake, clock)
        self.assertEqual(c.read("PRIN"), 1)
        clock.advance(5)
        self.assertEqual(c.read("PRIN"), 1)
        self.assertEqual(len(fake.requests), 3)
        self.assertEqual(c.nonsense, 0)
        self.assertEqual(fake.violations, [])

    def test_busy(self):
        fake = start(self)
        c = client(fake, FakeClock())
        fake.override("PRIN", "7")
        with self.assertRaises(PrinterBusy) as e:
            c.read("PRIN")
        self.assertEqual(str(e.exception), "Printer 1 is busy.")
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual((c.busy_replies, c.nonsense), (2, 0))
        answers = iter(["7", "1"])
        fake.override("PRIN", lambda: next(answers))
        self.assertEqual(fake.violations, [])
        fake.reset_observations()
        self.assertEqual(c.read("PRIN"), 1)
        self.assertEqual(len(fake.requests), 2)
        fake.override("PRIN", "6")
        with self.assertRaises(PrinterBusy):
            c.query("PRIN")
        fake.override("PRIN", None)
        self.assertEqual(c.read("PRIN"), 1)
        self.assertEqual(fake.violations, [])

    def test_nonsense(self):
        fake = start(self)
        c = client(fake, FakeClock())
        fake.override("PRIN", "Example")
        with self.assertRaises(BadReply) as e:
            c.read("PRIN")
        self.assertEqual((e.exception.tag, e.exception.reply), ("PRIN", "Example"))
        self.assertEqual(len(fake.requests), 2)
        self.assertEqual(c.nonsense, 2)
        fake.override("PRIN", "-1")
        with self.assertRaises(BadReply):
            c.read("PRIN")
        self.assertEqual(c.nonsense, 4)
        self.assertEqual(c.query("PRIN"), "-1")     # query doesn't check
        answers = iter(["None", "0"])
        fake.override("PRIN", lambda: next(answers))
        self.assertEqual(c.read("PRIN"), 0)
        self.assertEqual(c.nonsense, 5)
        self.assertEqual(fake.violations, [])

    def test_warm_up(self):
        clock = FakeClock()
        fake = start(self, clock=clock)
        c = client(fake, clock)
        c.command(Command.set("PRINT", 0))
        self.assertEqual(tags(fake), ["HXUI", "PRINT"])
        c.command(Command.set("PRINT", 0))
        self.assertEqual(tags(fake), ["HXUI", "PRINT", "PRINT"])
        clock.advance(3.0)
        c.command(Command.set("PRINT", 1))
        self.assertEqual(tags(fake), ["HXUI", "PRINT", "PRINT", "HXUI", "PRINT"])
        self.assertTrue(fake.printing)
        self.assertEqual(fake.writes, ["PRINT", "PRINT", "PRINT"])
        self.assertEqual(fake.violations, [])

    def test_warm_up_wrong_uid(self):
        # The fake authenticates by UID, so the other printer's UID is its HXUI answer
        fake = start(self)
        fake.override("HXUI", "1110")
        c = client(fake, FakeClock())
        with self.assertRaises(WrongPrinter) as e:
            c.command(Command.set("PRINT", 0))
        self.assertEqual((e.exception.expected, e.exception.got), (1109, "1110"))
        self.assertEqual(str(e.exception), "Printer 1 is not the expected printer: UID 1110 answered instead of 1109.")
        self.assertEqual(fake.writes, [])
        self.assertTrue(fake.printing)
        self.assertEqual(fake.violations, [])

    def test_warm_up_failure(self):
        fake = start(self)
        fake.override("HXUI", "7")
        c = client(fake, FakeClock())
        with self.assertRaises(PrinterBusy):
            c.command(Command.set("PRINT", 0))
        self.assertEqual(fake.writes, [])
        self.assertEqual(fake.violations, [])

    def test_write_never_repeated(self):
        fake = start(self)
        c = client(fake, FakeClock())
        fake.override("PRINT", FakePrinter.DROP)
        with self.assertRaises(NoReply) as e:
            c.command(Command.set("PRINT", 0))
        self.assertEqual(str(e.exception), "No reply from Printer 1 to PRINT. It may or may not have been done.")
        self.assertEqual(fake.writes, ["PRINT"])
        self.assertFalse(fake.printing)         # DROP carries the write out
        for answer, code in (("3", 3), ("7", 7), ("-1", -1), ("4", 4)):
            self.assertEqual(fake.violations, [])
            fake.reset_observations()
            fake.override("PRINT", answer)
            with self.assertRaises(CommandRefused) as e:
                c.command(Command.set("PRINT", 1))
            self.assertEqual((e.exception.tag, e.exception.code), ("PRINT", code))
            self.assertEqual(fake.writes, ["PRINT"])
        self.assertEqual(str(e.exception), "Printer 1 refused PRINT (code 4).")
        self.assertEqual(c.busy_replies, 1)
        self.assertFalse(fake.printing)
        self.assertEqual(fake.violations, [])
        fake.reset_observations()
        fake.override("PRINT", "OK then")
        with self.assertRaises(BadReply):
            c.command(Command.set("PRINT", 1))
        self.assertEqual(fake.writes, ["PRINT"])
        self.assertEqual(c.nonsense, 1)
        for answer in ("0", "0 1", "0,1", "0;x"):
            fake.override("PRINT", answer)
            c.command(Command.set("PRINT", 1))
        self.assertTrue(fake.printing)
        self.assertEqual(fake.violations, [])

    def test_echo(self):
        fake = start(self)
        fake.write_echo = True
        c = client(fake, FakeClock())
        c.command(Command.set("PRINT", 1))
        with fake.lock:
            self.assertEqual(fake.requests[-1]["reply"], b"0 1")
        self.assertTrue(fake.printing)
        self.assertEqual(fake.violations, [])

    def test_read_twice_and_check_uid(self):
        fake = start(self)
        c = client(fake, FakeClock())
        self.assertEqual(c.read_twice("PRIN"), 1)
        answers = iter(["0", "1"] * 4)
        fake.override("PRIN", lambda: next(answers))
        with self.assertRaises(BadReply) as e:
            c.read_twice("PRIN")
        self.assertEqual(c.nonsense, 1)
        self.assertEqual(e.exception.reply, "0 / 1")
        self.assertEqual(str(e.exception), "Printer 1 gave two different answers to PRIN.")
        c.check_uid()
        self.assertEqual(fake.violations, [])
        fake.reset_observations()
        fake.override("HXUI", "1110")
        with self.assertRaises(WrongPrinter) as e:
            c.check_uid()
        self.assertEqual((e.exception.expected, e.exception.got), (1109, "1110"))
        self.assertEqual(len(fake.requests), 2)
        answers = iter(["1110", "1111"])
        fake.override("HXUI", lambda: next(answers))
        with self.assertRaises(BadReply):
            c.check_uid()
        self.assertEqual(c.nonsense, 2)
        self.assertEqual(fake.violations, [])

    def test_silent(self):
        fake = start(self)
        fake.go_silent()
        c = client(fake, timings=TIMINGS.scaled(0.05))
        started = time.monotonic()
        with self.assertRaises(NoReply):
            c.read("PRIN")
        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(len(fake.requests), 2)
        self.assertLessEqual(fake.max_in_flight, 1)
        fake.go_silent(False)
        self.assertEqual(c.read("PRIN"), 1)
        self.assertEqual(fake.violations, [])

    def test_values(self):
        clock = FakeClock()
        fake = start(self, clock=clock, offset_fn=EDT)
        c = client(fake, clock)
        self.assertEqual((c.read("DATE"), c.read("TIME")), ("2026-10-29", "08:00:00"))
        self.assertEqual(c.read("INKT"), "Example Black")
        self.assertEqual((c.read("INKL"), c.read("PHTP"), c.read("ECOMOD")), (1, 25, 0))
        self.assertEqual((c.read("ENCST"), c.read("ENWHD"), c.read("ENPPR"), c.read("ENTPM")), (0, 56, 5000, 1))
        self.assertEqual((c.read("PLDEL"), c.read("MNPL"), c.read("PRPL"), c.read("PLVO"), c.read("PLWR"),
                          c.read("NZRO")), (1210, 450, 220, 9500, 27, 2))
        self.assertEqual((c.read("PRNSPD"), c.read("DELAY"), c.read("RESMOD"), c.read("QUANT")), (2500, 0, 0, 1))
        fake.ink_name = ""
        self.assertIsNone(c.read("INKT"))
        self.assertEqual(fake.violations, [])
        encoder = start(self, clock=clock, placement="encoder")
        self.assertEqual(client(encoder, clock).read("ENCST"), 1)


# --------------------------------------------------------------------------
# The gate
# --------------------------------------------------------------------------

class GateTests(unittest.TestCase):
    def run_threads(self, fake, clients):
        errors = []

        def work(c, n):
            try:
                for i in range(5):
                    tag = ("PRIN", "HXUI", "MSGNAM", "PMCNT", "DATE")[(i + n) % 5]
                    value = c.read(tag)
                    expected = {"PRIN": 1, "HXUI": 1109, "MSGNAM": None, "PMCNT": 100000}.get(tag, value)
                    if value != expected:
                        errors.append((tag, value))
                design = c.load_slot(0)
                if design.slot != 1 or design.tn != c.tn:
                    errors.append(("load", design.slot))
            except Exception as e:
                errors.append(e)
        threads = [threading.Thread(target=work, args=(clients[n % len(clients)], n)) for n in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(10)
        self.assertEqual(errors, [])
        self.assertEqual(fake.max_in_flight, 1)
        self.assertEqual(fake.violations, [])

    def test_one_in_flight(self):
        timings = TIMINGS.scaled(0.1)
        fake = start(self, latency_s=0.005)
        pool = ClientPool(key=hp.release_key(), gates=GateRegistry(), timings=timings)
        record = fake.printer_record()
        self.run_threads(fake, [pool.client(record)])
        self.assertEqual(pool.client(record).nonsense, 0)
        fake.reset_observations()
        gates = GateRegistry()
        pair = [client(fake, timings=timings, gates=gates) for _ in range(2)]
        self.run_threads(fake, pair)
        self.assertEqual([c.nonsense for c in pair], [0, 0])

    def test_gate_timeout(self):
        fake = start(self)
        gates = GateRegistry()
        c = client(fake, timings=Timings(gate_wait_s=0.1), gates=gates)
        gate = gates.gate(f"127.0.0.1:{fake.port}")
        self.assertIs(gates.gate(f"127.0.0.1:{fake.port}"), gate)
        held = threading.Event()
        release = threading.Event()

        def hold():
            with gate:
                held.set()
                release.wait(5)
        threading.Thread(target=hold, daemon=True).start()
        held.wait(5)
        try:
            for call in (lambda: c.read("PRIN"), lambda: c.command(Command.set("PRINT", 1)), c.login,
                         lambda: c.load_slot(0), c.fonts):
                with self.assertRaises(PrinterBusy) as e:
                    call()
                self.assertEqual(str(e.exception), "Printer 1 is busy with another request from this PC.")
        finally:
            release.set()
        self.assertEqual(fake.requests, [])
        self.assertEqual(fake.violations, [])

    def test_probe(self):
        fake = start(self)
        gates = GateRegistry()
        c = client(fake, gates=gates, timings=TIMINGS.scaled(0.1))     # a refused connect is slow on Windows
        self.assertIs(c.probe(), True)
        time.sleep(0.05)
        self.assertEqual(fake.requests, [])
        self.assertEqual(fake.max_in_flight, 0)
        self.assertEqual(fake.violations, [])
        self.assertIsNone(c.last_request_at)
        gate = gates.gate(f"127.0.0.1:{fake.port}")
        gate.acquire()
        try:
            started = time.monotonic()
            self.assertIsNone(c.probe())
            self.assertLess(time.monotonic() - started, 0.05)
        finally:
            gate.release()
        fake.stop()
        self.assertIs(c.probe(), False)

    def test_hold_after_no_reply(self):
        # A repeat times out while the printer is still working on it and the deadline stops the Load
        # there: the gate is still held read_retry_s, so another thread's read doesn't overlap it (§8.1)
        timings = Timings(http_timeout_s=0.1, read_retry_s=0.15)
        fake = start(self, offset_fn=EDT)
        fake.web_result_delay_s = float("inf")

        def slow_from_now(body):
            fake.web_latency_s = 0.13           # later web requests outlast the client's timeout
            return body
        fake.web_reply_hook = slow_from_now
        c = client(fake, timings=timings)
        load = in_thread(lambda: c.load_slot(9, deadline=time.monotonic() + 0.45))
        for _ in range(100):
            if "sme" in kinds(fake):
                break
            time.sleep(0.005)
        read = in_thread(lambda: c.read("PRIN"))
        with self.assertRaises(WebReadFailed):
            load()
        self.assertEqual(read(), 1)
        self.assertEqual(fake.violations, [])
        self.assertEqual(fake.max_in_flight, 1)

    def test_worst_cases(self):
        calls = ("read", "command", "read_twice", "load_slot", "fonts")
        self.assertEqual([hp.worst_case_s(c) for c in calls], [5.0, 7.5, 11.0, 10.0, 15.0])
        self.assertEqual([hp.worst_case_s(c) for c in ("query", "probe", "login", "check_uid", "firmware")],
                         [5.0, 5.0, 5.0, 11.0, 15.0])
        half = TIMINGS.scaled(0.5)
        for c in calls:
            self.assertAlmostEqual(hp.worst_case_s(c, half), hp.worst_case_s(c) / 2)
        with self.assertRaises(ValueError):
            hp.worst_case_s("other")


# --------------------------------------------------------------------------
# The web read-back
# --------------------------------------------------------------------------

class WebTests(unittest.TestCase):
    def setUp(self):
        self.clock = FakeClock()
        self.fake = start(self, clock=self.clock, offset_fn=EDT)
        self.c = client(self.fake, self.clock)

    def web_kinds(self):
        return [k for k in kinds(self.fake) if k != "autopilot"]

    def test_first_load(self):
        build_slot9(self.c)
        before = len(self.fake.requests)
        design = self.c.load_slot(9)
        self.assertEqual(kinds(self.fake)[before:], ["login", "sme", "sme_repeat"])
        with self.fake.lock:
            first = self.fake.requests[before + 1]["reply"]
        self.assertIn(b"<TYPE>Save</TYPE>", first)     # the SAVEMSG leftover came first
        self.assertEqual((design.slot, design.name, design.empty), (9, "STRIPES TEST", False))
        self.assertEqual([i.tag for i in design.items], ["CTEXT", "CTDAT"])
        text, date_item = design.items
        self.assertEqual((text.value("TXT"), text.value("SIZE"), text.value("POS"), text.value("OBJID"),
                          text.value("BOUND")), ("STRIPES TEST 123", "2", "10,10", "10001", "10,10,218,33"))
        self.assertEqual((date_item.value("TXT"), date_item.value("PREF"), date_item.value("SIZE"),
                          date_item.value("XRID"), date_item.value("SXAS"), date_item.value("POS")),
                         ("10/29/26", "Packed on ", "3", "10001", "1", "0,0"))
        self.assertEqual((design.settings["DELAY"], design.settings["PRNSPD"], design.settings["RESNAM"],
                          design.settings["ANG"]), ("480000", "2400", "Fine", "30"))
        self.assertGreater(design.black_pixels, 0)
        self.assertEqual((design.tn, design.xid, design.read_at), (self.c.tn, 1, self.clock.time()))
        self.assertEqual((self.c.nonsense, self.c.relogins), (0, 0))
        self.assertEqual(self.fake.violations, [])

    def test_stale_kinds(self):
        self.fake.leave_in_buffer(fixture("sme_load_other_client.bin"))
        self.assertEqual(self.c.load_slot(9).slot, 9)
        self.assertEqual(self.c.nonsense, 1)
        self.c.nonsense = 0
        self.c.load_slot(9)
        self.assertEqual(self.fake.violations, [])
        self.fake.reset_observations()
        self.c.load_slot(9)
        with self.fake.lock:
            first = self.fake.requests[0]["reply"]
        self.assertIn(f"<TN>{self.c.tn}</TN>".encode(), first)
        self.assertIn(b"<XID>2</XID>", first)
        self.assertEqual(self.web_kinds(), ["sme", "sme_repeat"])
        self.assertEqual(self.c.nonsense, 0)
        # Another Stripes PC's leftover is expected
        other = client(self.fake, self.clock)
        other.load_slot(0)
        self.c.load_slot(0)
        self.assertEqual(self.c.nonsense, 0)
        self.assertEqual(self.fake.violations, [])

    def test_cap(self):
        self.fake.web_result_delay_s = float("inf")
        with self.assertRaises(WebReadFailed) as e:
            self.c.load_slot(9)
        self.assertEqual(str(e.exception), "Printer 1 didn't send back the message in slot 9 in time.")
        self.assertEqual(self.web_kinds(), ["login", "sme"] + ["sme_repeat"] * 5)
        fake = start(self, clock=self.clock)
        fake.web_result_delay_s = float("inf")
        c = client(fake, self.clock, timings=Timings(read_retry_s=1.0))
        with self.assertRaises(WebReadFailed):
            c.load_slot(9)
        self.assertEqual(kinds(fake), ["login", "sme", "sme_repeat", "sme_repeat"])
        self.assertEqual((self.fake.violations, fake.violations), ([], []))

    def test_delayed_result(self):
        self.fake.web_result_delay_s = 1.2
        self.assertEqual(self.c.load_slot(9).slot, 9)
        self.assertEqual(self.web_kinds(), ["login", "sme", "sme_repeat", "sme_repeat", "sme_repeat"])
        self.assertEqual(self.fake.violations, [])

    def test_deadline(self):
        with self.assertRaises(WebReadFailed):
            self.c.load_slot(9, deadline=self.clock.monotonic() + 1.0)
        self.assertEqual(self.fake.requests, [])
        self.fake.web_result_delay_s = float("inf")
        deadline = self.clock.monotonic() + 3.5
        with self.assertRaises(WebReadFailed) as e:
            self.c.load_slot(9, deadline=deadline)
        self.assertIn("in time", str(e.exception))
        self.assertEqual(self.web_kinds(), ["login", "sme", "sme_repeat", "sme_repeat", "sme_repeat"])
        with self.fake.lock:
            starts = [r["t"] for r in self.fake.requests]
        self.assertTrue(all(t <= deadline - TIMINGS.http_timeout_s for t in starts), starts)
        self.assertLessEqual(self.clock.monotonic(), deadline)
        self.assertEqual(self.fake.violations, [])
        # fonts and firmware follow the same rule
        self.fake.reset_observations()
        with self.assertRaises(BadReply):
            self.c.fonts(deadline=self.clock.monotonic() + 1.0)
        self.assertIsNone(self.c.firmware(deadline=self.clock.monotonic() + 1.0))
        self.assertEqual(self.fake.requests, [])
        self.assertEqual(self.fake.violations, [])

    def test_gate_deadline(self):
        fake = start(self)
        gates = GateRegistry()
        c = client(fake, gates=gates)
        gate = gates.gate(f"127.0.0.1:{fake.port}")
        cases = [(lambda d: c.read("PRIN", deadline=d), 5.3, PrinterBusy),
                 (lambda d: c.query("PRIN", deadline=d), 5.3, PrinterBusy),
                 (lambda d: c.command(Command.set("PRINT", 1), deadline=d), 7.8, PrinterBusy),
                 (lambda d: c.load_slot(0, deadline=d), 2.3, WebReadFailed),
                 (lambda d: c.fonts(deadline=d), 2.3, BadReply)]
        gate.acquire()
        try:
            for call, ahead, error in cases:
                started = time.monotonic()
                with self.assertRaises(error):
                    call(time.monotonic() + ahead)
                waited = time.monotonic() - started
                self.assertGreater(waited, 0.2)
                self.assertLess(waited, 0.5)
            started = time.monotonic()
            self.assertIsNone(c.firmware(deadline=time.monotonic() + 2.3))
            self.assertLess(time.monotonic() - started, 0.5)
            # A deadline already too close: no wait at all
            started = time.monotonic()
            with self.assertRaises(PrinterBusy):
                c.read("PRIN", deadline=time.monotonic() + 1.0)
            self.assertLess(time.monotonic() - started, 0.1)
        finally:
            gate.release()
        self.assertEqual(fake.requests, [])
        # A gate freed in time lets it through
        gate.acquire()
        threading.Timer(0.1, gate.release).start()
        self.assertEqual(c.read("PRIN", deadline=time.monotonic() + 5.3), 1)
        self.assertEqual(len(fake.requests), 1)
        self.assertEqual(fake.violations, [])

    def test_fresh(self):
        self.fake.web_first_reply_fresh = True
        self.assertEqual(self.c.load_slot(0).slot, 1)
        self.assertEqual(self.web_kinds(), ["login", "sme"])
        self.assertEqual(self.fake.violations, [])

    def test_logging_in_again(self):
        self.c.load_slot(9)
        self.assertEqual(self.fake.violations, [])
        self.fake.expire_web_session()
        self.fake.reset_observations()
        self.assertEqual(self.c.load_slot(9).slot, 9)
        self.assertEqual(self.web_kinds(), ["sme", "login", "sme", "sme_repeat"])
        with self.fake.lock:
            statuses = [r["status"] for r in self.fake.requests]
        self.assertEqual(statuses[0], 401)
        self.assertEqual(self.c.relogins, 1)
        self.assertEqual(self.fake.violations, [])
        self.fake.set_password("other")
        self.fake.reset_observations()
        with self.assertRaises(AuthFailed):
            self.c.load_slot(9)
        self.assertEqual(self.web_kinds(), ["sme", "login"])
        self.assertEqual(self.c._cookies, {})
        self.assertEqual(self.fake.violations, [])
        # One session only: the other client's login ends ours, and the next call logs in again
        fake = start(self, clock=self.clock)
        fake.single_web_session = True
        a, b = client(fake, self.clock), client(fake, self.clock)
        a.load_slot(0)
        b.load_slot(0)
        a.load_slot(0)
        self.assertEqual((a.relogins, b.relogins), (1, 0))
        self.assertEqual((a.nonsense, b.nonsense), (0, 0))
        self.assertEqual(fake.violations, [])

    def test_broken_login_reply_is_auth_failed(self):
        # §2.4 and §10.1 rule 3: a Login reply that is neither 200 with the cookies nor 401 is AuthFailed.
        # A 200 with a broken header line raised BadReply, which the poller's second reading didn't
        # catch: it dropped every cycle unjudged and the machine stayed green on an unchecked origin
        for reply in (b"HTTP/1.1 200 OK\r\nbroken header\r\n\r\n", b"HTTP/2 200 OK\r\n\r\n",
                      b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n0\r\n\r\n"):
            for call in ("login", "load_slot"):
                with self.subTest(reply=reply[:24], call=call):
                    port = one_shot_server(self, reply)
                    c = PrinterClient("127.0.0.1", 1109, TOKEN, port=port, clock=self.clock, gates=GateRegistry())
                    c.label = "Printer 1"
                    if call == "login":
                        c._cookies = {"Access_token_1109": "old", "Auth_token_1109": "old"}
                    with self.assertRaises(AuthFailed) as e:
                        c.login() if call == "login" else c.load_slot(9)
                    self.assertIn("refused the password", str(e.exception))
                    self.assertEqual(c._cookies, {})                    # forgotten
                    self.assertEqual((c.nonsense, c.relogins), (1, 0))  # still counted as nonsense (§13)

    def test_second_login_page(self):
        # A session that is refused again right after logging in: AuthFailed, once per call
        self.c.login()
        with self.fake.lock:
            self.fake.sessions.clear()
        original = self.fake._login

        def login_without_session(req):
            reply = original(req)
            self.fake.sessions.clear()
            return reply
        self.fake._login = login_without_session
        self.assertEqual(self.fake.violations, [])
        self.fake.reset_observations()
        with self.assertRaises(AuthFailed):
            self.c.load_slot(9)
        self.assertEqual(self.web_kinds(), ["sme", "login", "sme"])
        self.assertEqual(self.fake.violations, [])

    def test_dropped_load(self):
        self.c.load_slot(9)
        self.assertEqual(self.fake.violations, [])
        self.fake.drop_after_idle_s = 3.0
        self.clock.advance(5)
        self.fake.reset_observations()
        self.assertEqual(self.c.load_slot(9).xid, 3)
        with self.fake.lock:
            loads = [r["body"] for r in self.fake.requests if r["kind"] == "sme"]
            first_status = self.fake.requests[0]["status"]
        self.assertIsNone(first_status)
        self.assertEqual([re.search(rb"<XID>(\d+)</XID>", b).group(1) for b in loads], [b"2", b"3"])
        self.assertEqual(self.c.nonsense, 0)
        self.assertEqual(self.fake.violations, [])

    def dropping(self, idle_s, *, logged_in, quiet=True, timings=TIMINGS):
        """A fresh fake that closes without a reply any request after idle_s of quiet, and a client
        (logged in when logged_in); with quiet, 5 s of quiet have passed already."""
        fake = start(self, clock=self.clock, offset_fn=EDT)
        c = client(fake, self.clock, timings=timings)
        if logged_in:
            c.login()
        fake.drop_after_idle_s = idle_s
        if quiet:
            self.clock.advance(5)
        fake.reset_observations()
        return fake, c

    def held(self, fake, kinds_sent):
        """How long the call kept the gate after its last request, which got no reply."""
        self.assertEqual(kinds(fake), kinds_sent)
        with fake.lock:
            last = fake.requests[-1]
        self.assertIsNone(last["status"])           # closed without a reply: NoReply
        self.assertEqual(fake.violations, [])
        return self.clock.monotonic() - last["t"]

    def test_hold_after_no_reply_and_deadline(self):
        # §8.1 "Any NoReply": the gate stays held read_retry_s, also when the deadline then stops the call
        # (the retry no longer fits, the hold still does)
        r = TIMINGS.read_retry_s
        calls = [("login", False, lambda c, d: c.load_slot(9, deadline=d), WebReadFailed),
                 ("sme", True, lambda c, d: c.load_slot(9, deadline=d), WebReadFailed),
                 ("get", True, lambda c, d: c.fonts(deadline=d), BadReply),
                 ("get", True, lambda c, d: c.firmware(deadline=d), None)]
        for kind, logged_in, call, error in calls:
            with self.subTest(kind=kind, error=error):
                fake, c = self.dropping(3.0, logged_in=logged_in)
                deadline = self.clock.monotonic() + TIMINGS.http_timeout_s + 0.3
                if error is None:
                    self.assertIsNone(call(c, deadline))
                else:
                    with self.assertRaises(error):
                        call(c, deadline)
                self.assertAlmostEqual(self.held(fake, [kind]), r, places=6)
                self.assertLessEqual(self.clock.monotonic(), deadline)
        # A repeat with no reply, then the deadline
        fake, c = self.dropping(r, logged_in=False, quiet=False)     # the repeat 0.5 s after the Load is dropped
        fake.web_result_delay_s = float("inf")
        deadline = self.clock.monotonic() + 2.9
        with self.assertRaises(WebReadFailed):
            c.load_slot(9, deadline=deadline)
        self.assertAlmostEqual(self.held(fake, ["login", "sme", "sme_repeat"]), r, places=6)
        self.assertLessEqual(self.clock.monotonic(), deadline)
        # A full hold doesn't fit: held up to the deadline, not past it
        fake, c = self.dropping(0.0, logged_in=False, timings=Timings(http_timeout_s=0.2))
        deadline = self.clock.monotonic() + 0.3
        with self.assertRaises(WebReadFailed):
            c.load_slot(9, deadline=deadline)
        self.assertAlmostEqual(self.held(fake, ["login"]), 0.3, places=6)
        self.assertAlmostEqual(self.clock.monotonic(), deadline, places=6)

    def test_hold_after_second_no_reply(self):
        # Without a deadline: the second attempt gets no reply either, and the gate is held before NoReply
        r = TIMINGS.read_retry_s
        calls = [("login", False, lambda c: c.login()),
                 ("login", False, lambda c: c.load_slot(9)),
                 ("sme", True, lambda c: c.load_slot(9)),
                 ("get", True, lambda c: c.fonts()),
                 ("get", True, lambda c: c.firmware())]
        for kind, logged_in, call in calls:
            with self.subTest(kind=kind):
                fake, c = self.dropping(r, logged_in=logged_in)
                with self.assertRaises(NoReply):
                    call(c)
                self.assertAlmostEqual(self.held(fake, [kind, kind]), r, places=6)
                with fake.lock:
                    first, second = (q["t"] for q in fake.requests)
                self.assertAlmostEqual(second - first, r, places=6)

    def test_wrong_slot(self):
        self.fake.web_reply_hook = lambda body: body.replace(b"<MSNO>9</MSNO>", b"<MSNO>10</MSNO>")
        with self.assertRaises(WebReadFailed) as e:
            self.c.load_slot(9)
        self.assertEqual(str(e.exception), "Printer 1 sent back slot 10 instead of slot 9.")
        self.assertEqual(self.c.nonsense, 1)
        self.fake.web_reply_hook = lambda body: body.replace(b"<FCDOC>", b"<FCDOC><BROKEN>")
        with self.assertRaises(WebReadFailed) as e:
            self.c.load_slot(1)
        self.assertEqual(str(e.exception), "Printer 1 sent back a message Stripes doesn't recognise.")
        self.assertEqual(self.c.nonsense, 2)
        with self.assertRaises(ValueError):
            self.c.load_slot(11)
        with self.assertRaises(ValueError):
            self.c.load_slot(-1)
        self.assertEqual(self.fake.violations, [])

    def test_slot_0(self):
        design = self.c.load_slot(0)
        self.assertEqual((design.slot, design.name, design.width, design.height), (1, "", None, 160))
        self.assertEqual(design.items[0].pairs[2], ("PREF", "Packed: "))
        self.assertEqual(self.fake.violations, [])

    def test_fonts_firmware(self):
        self.assertEqual(self.c.fonts(), list(STANDARD_FONTS))
        self.fake.gzip_files = True
        self.assertEqual(self.c.fonts(), list(STANDARD_FONTS))
        self.assertEqual(self.c.firmware(), "69.75.10")
        self.fake.files["/sfl.xml"] = None
        with self.assertRaises(BadReply) as e:
            self.c.fonts()
        self.assertEqual(str(e.exception), "Printer 1 didn't send its font list.")
        self.assertEqual(e.exception.tag, "FONTS")
        self.fake.files["/sfl.xml"] = b"<SFL></SFL>"
        with self.assertRaises(BadReply):
            self.c.fonts()
        self.fake.files["/idn.xml"] = None
        self.assertIsNone(self.c.firmware())
        self.assertEqual(self.c.nonsense, 0)
        self.assertEqual(self.web_kinds().count("login"), 1)
        self.assertEqual(self.fake.violations, [])


# --------------------------------------------------------------------------
# The fake printer itself
# --------------------------------------------------------------------------

SLOT9_WRITES = [
    "<CRTMSG>STRIPES TEST</CRTMSG>",
    "<CTEXT><OBJID>10001</OBJID><POS>10,10</POS><TXT>STRIPES TEST 123</TXT><FONTIND>1</FONTIND><SIZE>3</SIZE>"
    "<MARG>0,0</MARG></CTEXT>",
    "<CTDAT><POS>10,60</POS><PREF>PKD:</PREF><FRMT>Y/M/D</FRMT><DAY>0</DAY><MON>0</MON><YEAR>0</YEAR>"
    "<FONTIND>1</FONTIND><SIZE>3</SIZE><MARG>0,0</MARG></CTDAT>",
    "<CLOT><POS>10,100</POS><PREF>LOT:</PREF><INDX>1</INDX><FONTIND>2</FONTIND><SIZE>4</SIZE><MARG>0,0</MARG></CLOT>",
    "<SAVEMSG>9</SAVEMSG>",
]


class FakePrinterTests(unittest.TestCase):
    def load_raw(self, fake, xid, slot, cookies=None):
        cookies = cookies or raw_login(fake)
        raw(fake, hp.sme_request("127.0.0.1", fake.port, cookies, repeat=False, body=hp.load_body("326548", xid, slot)))
        return raw(fake, hp.sme_request("127.0.0.1", fake.port, cookies, repeat=True, body=b"<SM>RepeatRequest</SM>"))

    def test_verbose_fidelity(self):
        fake = start(self, clock=FakeClock(), offset_fn=EDT)
        for xml in SLOT9_WRITES:
            self.assertEqual(raw_autopilot(fake, xml).body, b"0")
        reply = self.load_raw(fake, 4, 9)
        self.assertEqual(reply.status, 200)
        self.assertEqual(reply.header("Content-Type"), "application/xml")
        self.assertEqual(placeholders(reply.body), placeholders(fixture("sme_load_slot9.bin")))
        self.assertEqual(fake.violations, ["write not whitelisted CLOT"])
        self.assertEqual(fake.writes, ["CRTMSG", "CTEXT", "CTDAT", "CLOT", "SAVEMSG"])
        design = hp.parse_sme_reply(reply.body).design
        self.assertEqual(design.black_pixels, 208 * 23 + 182 * 23 + 56 * 36)

    def test_compact_fidelity(self):
        fake = start(self, clock=FakeClock())
        reply = self.load_raw(fake, 6, 0)
        self.assertEqual(placeholders(reply.body), placeholders(fixture("sme_load_slot0_compact.bin")))
        encoder = start(self, clock=FakeClock(), placement="encoder")
        self.assertIn(b"<ENCST>1</ENCST>", self.load_raw(encoder, 6, 0).body)
        self.assertEqual(fake.violations, [])

    def test_buffer(self):
        fake = start(self, clock=FakeClock())
        cookies = raw_login(fake)
        first = raw(fake, hp.sme_request("127.0.0.1", fake.port, cookies, repeat=False,
                                         body=hp.load_body("326548", 1, 9))).body
        self.assertEqual(hp.sme_envelope(first), ("Save", "", "", "1"))      # the start-up buffer
        empty = raw(fake, hp.sme_request("127.0.0.1", fake.port, cookies, repeat=True,
                                         body=b"<SM>RepeatRequest</SM>")).body
        self.assertEqual(placeholders(empty), placeholders(fixture("sme_load_slot9_empty.bin").replace(
            b"<XID>2</XID>", b"<XID>1</XID>")))
        self.assertEqual(empty, fixture("sme_load_slot9_empty.bin").replace(b"<XID>2</XID>", b"<XID>1</XID>"))
        for xml in SLOT9_WRITES:
            raw_autopilot(fake, xml)
        save = raw(fake, hp.sme_request("127.0.0.1", fake.port, cookies, repeat=True,
                                        body=b"<SM>RepeatRequest</SM>")).body
        self.assertEqual(placeholders(save), placeholders(fixture("sme_save_stale.bin")))
        self.assertEqual(raw(fake, hp.sme_request("127.0.0.1", fake.port, {}, repeat=True,
                                                  body=b"<SM>RepeatRequest</SM>")).status, 401)

    def test_overlap(self):
        fake = start(self, latency_s=0.05)
        first = in_thread(lambda: raw_autopilot(fake, "<PRIN/>").body)
        time.sleep(0.01)
        self.assertEqual(raw_autopilot(fake, "<PRIN/>").body, b"7")
        self.assertEqual(first(), b"1")
        self.assertEqual(fake.max_in_flight, 2)
        self.assertEqual(fake.violations, ["two requests in flight"])
        fake = start(self, latency_s=0.05, mixup=True)
        fake.mixup_p = 1.0
        first = in_thread(lambda: raw_autopilot(fake, "<PRIN/>").body)
        time.sleep(0.01)
        self.assertEqual(raw_autopilot(fake, "<MSGNAM/>").body, b"1")      # the PRIN's reply
        self.assertEqual(first(), b"1")
        fake = start(self, latency_s=0.05, busy_on_overlap=False)
        first = in_thread(lambda: raw_autopilot(fake, "<PRIN/>").body)
        time.sleep(0.01)
        self.assertEqual(raw_autopilot(fake, "<MSGNAM/>").body, b"None")
        first()

    def test_overload(self):
        fake = start(self, latency_s=0.1)
        fake.overload_at = 3
        fake.overload_pause_s = 0.2
        joins = []
        for _ in range(3):
            joins.append(in_thread(lambda: raw_autopilot(fake, "<PRIN/>")))
            time.sleep(0.01)
        for join in joins:
            with self.assertRaises(NoReply):
                join()
        time.sleep(0.2)
        self.assertEqual(raw_autopilot(fake, "<PRIN/>").body, b"1")
        self.assertEqual(fake.max_in_flight, 3)

    def test_multi_value(self):
        fake = start(self)
        fake.multi_value_hang_s = 0.1
        started = time.monotonic()
        self.assertEqual(raw_autopilot(fake, "<CPPARM/>").body, b"6")
        self.assertGreaterEqual(time.monotonic() - started, 0.1)
        self.assertEqual(fake.violations, ["multi-value query CPPARM"])

    def test_editor_page(self):
        fake = start(self, editor_page_bytes=200_000, editor_stall_s=0.2)
        started = time.monotonic()
        page = in_thread(lambda: hp.http_exchange("127.0.0.1", fake.port, b"GET /simple_msg.html HTTP/1.1\r\n\r\n",
                                                  timeout_s=2.0, max_body=300_000))
        time.sleep(0.03)
        self.assertEqual(raw_autopilot(fake, "<PRIN/>").body, b"1")
        self.assertGreaterEqual(time.monotonic() - started, 0.2)
        self.assertEqual(len(page().body), 200_000)
        self.assertEqual(fake.violations, ["editor page requested"])

    def test_settings_delay_td(self):
        clock = FakeClock()
        fake = start(self, clock=clock, offset_fn=EDT)
        c = client(fake, clock)
        c.command(Command.set("CRTMSG", "T"))
        c.command(Command.block("CTEXT", [("TXT", "x")]))
        c.command(Command.block("CPPARM", [("DELAY", 284176), ("RESMOD", 3)]))
        c.command(Command.set("SAVEMSG", 10))
        c.command(Command.set("PRINTMSG", 10))
        self.assertEqual(fake.current_slot, 10)
        self.assertEqual(c.query("DELAY"), "284")
        self.assertEqual(fake.slots[10].settings["RESNAM"], "Draft")
        self.assertEqual(c.read("MSGNAM"), "T")
        c.command(Command.block("TD", [("DAY", 29), ("MNTH", 10), ("YEAR", 2026), ("HOUR", 8), ("MIN", 45)]))
        self.assertEqual(c.read("TIME"), "08:45:00")
        clock.advance(61)
        self.assertEqual((c.read("DATE"), c.read("TIME")), ("2026-10-29", "08:46:01"))
        c.command(Command.block("TD", [("HOUR", 24), ("MIN", 0), ("DTRH", 0), ("DTRM", 0)]))
        self.assertEqual(c.read("TIME"), "00:00:00")
        self.assertEqual(fake.rollover, (0, 0))
        self.assertEqual(raw_autopilot(fake, "<TD><HOUR>25</HOUR></TD>").body, b"3")
        with self.assertRaises(ForbiddenCommand):
            c.command(Command.block("TD", [("HOUR", 25)]))         # the client never sends it
        self.assertEqual(raw_autopilot(fake, "<TD><DAY>31</DAY><MNTH>2</MNTH></TD>").body, b"3")
        self.assertEqual(fake.violations, [])

    def test_write_errors(self):
        fake = start(self)
        answers = [
            ("<CTEXT><TXT>a</TXT></CTEXT>", b"5"),                 # no design in progress
            ("<CPPARM><BOLD>1</BOLD></CPPARM>", b"5"),
            ("<SAVEMSG>9</SAVEMSG>", b"5"),
            ("<CRTMSG>" + "A" * 21 + "</CRTMSG>", b"3"),
            ("<CRTMSG>T</CRTMSG>", b"0"),
            ("<CTEXT><SIZE>11</SIZE></CTEXT>", b"3"),
            ("<CTEXT><FONTIND>7</FONTIND></CTEXT>", b"3"),
            ("<CTEXT><FRMT>Y</FRMT></CTEXT>", b"3"),                # not a CTEXT child
            ("<CTEXT>junk</CTEXT>", b"2"),
            ("<CPPARM><PRNSPD>2501</PRNSPD></CPPARM>", b"3"),
            ("<CPPARM><SLANT>1</SLANT></CPPARM>", b"3"),
            ("<SAVEMSG>11</SAVEMSG>", b"3"),
            ("<PRINTMSG>5</PRINTMSG>", b"4"),                      # an empty slot
            ("<PRINT>2</PRINT>", b"3"),
            ("<CBARC><POS>0,0</POS></CBARC>", b"1"),
            ("<SETPPARAM><PRNSPD>1</PRNSPD></SETPPARAM>", b"0"),
            ("<PRIN/><PRINT>0</PRINT>", b"1"),
            ("<BEAC/>", b"-1"),
            ("<LOT1/>", b""),
            ("garbage", b"2"),
        ]
        for xml, answer in answers:
            self.assertEqual(raw_autopilot(fake, xml).body, answer, xml)
        self.assertTrue(fake.printing)
        self.assertEqual(fake.violations, [
            "write outside slots 9 and 10", "write outside slots 9 and 10", "write not whitelisted CBARC",
            "printer-wide command SETPPARAM", "two commands in one request", "query not in catalogue BEAC",
            "query not in catalogue LOT1"])
        fake.reset_observations()
        raw_autopilot(fake, "<CRTMSG>T</CRTMSG>")
        raw_autopilot(fake, "<SAVEMSG>1</SAVEMSG>")      # the printing slot, while printing
        self.assertEqual(fake.violations, ["write outside slots 9 and 10", "SAVEMSG to the printing slot"])

    def test_web_violations(self):
        fake = start(self)
        cookies = raw_login(fake)
        self.assertEqual(raw(fake, hp.file_request("127.0.0.1", fake.port, cookies, "/sfl.xml").replace(
            b"/sfl.xml", b"/uss.xml")).status, 404)
        raw(fake, hp.sme_request("127.0.0.1", fake.port, cookies, repeat=False, body=b"<SM><TYPE>Save</TYPE></SM>"))
        raw(fake, hp.sme_request("127.0.0.1", fake.port, cookies, repeat=False,
                                 body=b"<SM><TYPE>Thumbnail</TYPE><TN>1</TN><XID>1</XID><MSNO>9</MSNO></SM>"))
        self.assertEqual(raw(fake, b"POST /other HTTP/1.1\r\nContent-Length: 0\r\n\r\n").status, 404)
        self.assertEqual(fake.violations, ["web file requested /uss.xml", "bad SME request", "bad SME request",
                                           "web file requested /other"])
        r = raw(fake, b"GET / HTTP/1.1\r\n\r\n")
        self.assertEqual((r.status, r.body), (401, hx_fake.LOGIN_HTML))
        r = raw(fake, b"GET / HTTP/1.1\r\nCookie: Access_token_1109=%s; Auth_token_1109=%s\r\n\r\n" % (
            cookies["Access_token_1109"].encode(), cookies["Auth_token_1109"].encode()))
        self.assertEqual((r.status, r.body), (200, hx_fake.MAIN_HTML))

    def test_unauthorized(self):
        fake = start(self)
        for request in (hp.autopilot_request(1109, hp.make_token("wrong"), "<PRIN/>"),
                        hp.autopilot_request(1110, TOKEN, "<PRIN/>"), b"POST / HTTP/1.1\r\nContent-Length: 3\r\n\r\nabc"):
            r = raw(fake, request, max_body=4096)
            self.assertEqual((r.status, r.body, r.header("Content-Encoding")), (401, hx_fake.LOGIN_HTML, "gzip"))
        r = hp.http_exchange("127.0.0.1", fake.port, hp.login_request("127.0.0.1", fake.port, "0" * 32),
                             timeout_s=2.0, max_body=hp.MAX_LOGIN_REPLY, allow_cut_short=True)
        self.assertEqual((r.status, r.cookies(), r.body), (200, {}, hx_fake.LOGIN_HTML))
        self.assertEqual(fake.writes, [])

    def test_print_bags(self):
        clock = FakeClock()
        fake = start(self, clock=clock)
        c = client(fake, clock)
        build_slot9(c)
        c.command(Command.set("PRINTMSG", 9))
        self.assertEqual(fake.counters["MSCNT"], 0)        # back to 0 on a message change (OPEN)
        fake.print_bags(2)
        self.assertEqual((fake.counters["MSCNT"], fake.counters["PMCNT"]), (2, 100002))
        self.assertEqual(fake.printed[-1], {"slot": 9, "name": "STRIPES TEST", "encoder": False,
                                            "position_mm": None, "delay_us": 480000, "at": clock.time()})
        c.command(Command.set("PRINT", 0))
        fake.print_bags(5)
        self.assertEqual(len(fake.printed), 2)
        self.assertEqual(fake.violations, [])

    def test_print_bags_slot_1(self):
        fake = start(self, clock=FakeClock())
        fake.print_bags(3)
        self.assertEqual((fake.counters["PMCNT"], fake.counters["MSCNT"], len(fake.printed)), (100003, 3, 3))
        self.assertEqual(fake.printed[0]["delay_us"], 0)

    def test_encoder_bags(self):
        clock = FakeClock()
        fake = start(self, clock=clock, placement="encoder")
        c = client(fake, clock)
        c.command(Command.set("CRTMSG", "E"))
        c.command(Command.block("CTEXT", [("TXT", "x")]))
        c.command(Command.block("CPPARM", [("ENCST", 1), ("DLYMM", 348)]))
        c.command(Command.set("SAVEMSG", 9))
        c.command(Command.set("PRINTMSG", 9))
        fake.print_bags(1)
        self.assertEqual(fake.printed[0]["encoder"], True)
        self.assertEqual((fake.printed[0]["position_mm"], fake.printed[0]["delay_us"]), (348, None))
        self.assertEqual(fake.violations, [])

    def test_printer_clock(self):
        clock = FakeClock()
        fake = FakePrinter(clock=clock, offset_fn=EDT)
        self.assertEqual(fake.base_offset_s, -14400.0)
        self.assertEqual(fake.printer_clock(), datetime(2026, 10, 29, 8, 0))
        fake.clock_offset_s = 7860.0          # 2 h 11 min fast, like the printer seen
        self.assertEqual(fake.printer_clock(), datetime(2026, 10, 29, 10, 11))
        with self.assertRaises(RuntimeError):
            fake.printer_record()

    def test_bmp(self):
        picture, black = hx_fake._draw([(10, 10, 20, 20), (15, 15, 25, 25), (-5, -5, 2, 2), (1020, 150, 1100, 200)],
                                       1024, 160)
        self.assertEqual(black, 100 + 100 - 25 + 4 + 40)
        self.assertEqual(picture, bmp_1bit([(10, 10, 20, 20), (15, 15, 25, 25), (-5, -5, 2, 2), (1020, 150, 1100, 200)]))
        self.assertEqual(len(picture), 20542)
        self.assertEqual(hp.decode_preview(hp.PREVIEW_PREFIX + base64.b64encode(picture).decode()), picture)
        # Pixel (x, y) is bit 7 - x % 8 of byte x // 8 in stored row 159 - y; a 0 bit is black
        row = 62 + (159 - 10) * 128
        self.assertEqual(picture[row + 1] >> 5 & 1, 0)      # x 10
        self.assertEqual(picture[row + 1] >> 6 & 1, 1)      # x 9
        self.assertEqual(hx_fake._render_date("Y/M/D H:U:E m y", datetime(2026, 1, 31, 8, 5, 9), months=1),
                         "2026/02/28 08:05:09 FEB 26")
        self.assertEqual(hx_fake._render_date("D/M/Y", datetime(2024, 2, 29), years=1, days=1), "01/03/2025")


class FakeSntpTests(unittest.TestCase):
    def ask(self, server, timeout=0.5):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.settimeout(timeout)
        now = time.time() + 2_208_988_800
        transmit = struct.pack("!II", int(now), int((now % 1) * 2 ** 32))
        sock.sendto(b"\x23" + bytes(39) + transmit, ("127.0.0.1", server.port))
        return sock.recvfrom(1024)[0], transmit

    def test_answer(self):
        server = FakeSntp(offset_s=3600.0).start()
        self.addCleanup(server.stop)
        reply, transmit = self.ask(server)
        self.assertEqual(len(reply), 48)
        self.assertEqual((reply[0] >> 6, (reply[0] >> 3) & 7, reply[0] & 7, reply[1]), (0, 4, 4, 2))
        self.assertEqual(reply[12:16], b"FAKE")
        self.assertEqual(reply[24:32], transmit)
        seconds = struct.unpack("!I", reply[40:44])[0] - 2_208_988_800
        self.assertLess(abs(seconds - (time.time() + 3600)), 2)
        self.assertEqual(server.requests, 1)

    def test_silent(self):
        server = FakeSntp(silent=True).start()
        self.addCleanup(server.stop)
        with self.assertRaises(socket.timeout):
            self.ask(server, timeout=0.2)
        self.assertEqual(server.requests, 1)


class FakeWorldTests(unittest.TestCase):
    def world(self, **kw):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        world = hx_fake.FakeWorld(tmp.name, **kw)
        self.addCleanup(world.close)
        return tmp.name, world

    def test_time_source(self):
        _, world = self.world()
        self.assertEqual(world.time_source().now(fresh=True).source, "server")
        self.assertGreaterEqual(world.sntp.requests, 1)

    def test_unknown_printer_setting(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(TypeError):
                hx_fake.FakeWorld(tmp, printers=[{"no_such_knob": 1}])
            world = hx_fake.FakeWorld(tmp, printers=[{"web_latency_s": 0.25, "latency_s": 0.01, "seed": 3}])
            try:
                self.assertEqual((world.printers[0].web_latency_s, world.printers[0].latency_s), (0.25, 0.01))
                self.assertEqual(len(world.printers), 1)
            finally:
                world.close()
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", world.printers[0].port), timeout=0.5).close()

    def test_close(self):
        _, world = self.world()
        world.close()
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.addCleanup(sock.close)
        sock.settimeout(0.2)
        sock.sendto(b"\x23" + bytes(47), ("127.0.0.1", world.sntp.port))
        with self.assertRaises(OSError):        # a timeout, or refused: either way nobody answers
            sock.recvfrom(1024)
        world.close()                           # twice is harmless

    def test_default_offset(self):
        import hx_time
        fake = FakePrinter()
        self.assertIs(fake.offset_fn, hx_time.local_utc_offset)
        clock = FakeClock()                                     # 29 Oct 2026 12:00 UTC
        fake = FakePrinter(clock=clock, offset_fn=EDT)
        self.assertEqual(fake.base_offset_s, -4 * 3600.0)
        self.assertEqual(fake.printer_clock(), datetime(2026, 10, 29, 8, 0))
        # The fake's clock starts at the local time a TimeSource with the same offset function gives
        self.assertEqual(hx_time.TimeSource([], clock=clock, offset_fn=EDT).now().local, fake.printer_clock())

    def test_seed(self):
        import hx_share
        import hx_time
        from hx_model import Company, Film, Line, Machine, PrintText, Product, from_json
        tmp, world = self.world(printers=[{}, {"placement": "encoder", "name": "Printer B"}])
        self.assertEqual(world.share_root, os.path.join(tmp, "share", "v2"))
        store = world.store(world.identity("jsmith"))

        def load(cls, record_id):
            data = store.read_json(hx_share.record_rel_for(cls, record_id))
            self.assertIsNotNone(data, (cls, record_id))
            return from_json(cls, data)
        company = load(Company, "company")
        self.assertEqual(company.time_server, f"127.0.0.1:{world.sntp.port}")
        self.assertEqual([c.name for c in company.countries], ["USA", "Canada", "Mexico"])
        self.assertEqual(company.countries[0].aliases, ["US", "U.S.A.", "United States"])
        self.assertEqual([(i.name, i.cartridge, i.colour) for i in company.inks],
                         [("Example Black", "plastic", "black"), ("Example White", "plastic", "white"),
                          ("Example Paper Black", "paper", "black")])
        self.assertEqual((company.admin_group_sid, company.sender_group_sid),
                         ("S-1-5-21-1000-2000-3000-1101", "S-1-5-21-1000-2000-3000-1102"))
        self.assertEqual((company.revision, company.updated_by), (1, "EXAMPLE\\admin"))
        self.assertEqual(world.ids["company"], "company")
        line = load(Line, world.ids["line"])
        self.assertEqual((line.name, line.code, line.cartridge, line.notify_user),
                         ("Line 1", "L1", "plastic", "EXAMPLE\\jsmith"))
        first = load(Machine, world.ids["machine1"])
        second = load(Machine, world.ids["machine2"])
        self.assertEqual((first.name, first.code, first.placement, first.print_speed, first.head_distance_mm),
                         ("Line 1 Bagger 1", "B1", "delay", 2400, None))
        self.assertEqual((second.name, second.placement, second.head_distance_mm, second.encoder_wheel_mm,
                          second.encoder_ppr), ("Line 1 Bagger 2", "encoder", 412.0, 56.0, 5000))
        self.assertEqual((first.line_id, first.printer_id, second.printer_id),
                         (line.id, world.ids["printer1"], world.ids["printer2"]))
        self.assertEqual(load(Printer, world.ids["printer1"]).name, "Printer 1")
        printer = load(Printer, world.ids["printer2"])
        self.assertEqual((printer.name, printer.host, printer.port, printer.uid, printer.revision),
                         ("Printer B", "127.0.0.1", world.printers[1].port, 1109, 1))
        self.assertEqual(hp.unscramble(printer.token, hp.release_key()), TOKEN)
        film = load(Film, world.ids["film"])
        self.assertEqual((film.code, film.artwork_revision, film.status, film.side, film.cartridge, film.ink_colour),
                         ("EXCO1", "Rev C", "active", "back", "plastic", "black"))
        self.assertEqual((film.length_mm, film.position_mm, film.delay_ms), (610.0, 150.0, 480.0))
        self.assertEqual((film.font, film.size, film.resolution, film.bold, film.width), (2, 4, 0, 1, 1))
        self.assertEqual((film.print_text_ids, film.default_print_text_id),
                         ([world.ids["print_text"]], world.ids["print_text"]))
        print_text = load(PrintText, world.ids["print_text"])
        self.assertEqual((print_text.name, print_text.code, print_text.status),
                         ("Packed on + origin", "PKO", "active"))
        self.assertEqual([(i.id, i.kind, i.before, i.placement) for i in print_text.items],
                         [("i1", "packed_date", "Packed on ", "right"), ("i2", "origin", "", "below"),
                          ("i3", "lot", "Lot ", "right")])
        self.assertEqual(print_text.items[0].date_format, "MM/DD/YY")
        product = load(Product, world.ids["product"])
        self.assertEqual((product.name, product.customer, film.product_id),
                         ("Example Co 2lb Onion", "Example Co", product.id))
        changes = store.read_json("config/hx/changes.json")
        self.assertEqual((changes["file_type"], changes["counter"], changes["written_by"]),
                         ("changes", 1, "EXAMPLE\\admin"))
        # The company's time server is the fake one, port included, as make_services reads it
        self.assertEqual(hx_time.TimeSource([company.time_server]).now(fresh=True).source, "server")
        views = []
        real_view = world.fs.view

        def view(**kw):
            views.append(real_view(**kw))
            return views[-1]
        with mock.patch.object(world.fs, "view", view):
            a = world.store(world.identity("a"))
            b = world.store(world.identity("b"))
        self.assertEqual(len(views), 2)
        self.assertIsNot(views[0], views[1])
        self.assertIsNot(a.fs, b.fs)
        token = world.printers[0].printer_record().token
        self.assertEqual(hp.unscramble(token, hp.release_key()), TOKEN)
        ident = world.identity("akim", admin=True)
        self.assertEqual((ident.user, ident.computer, ident.pid), ("EXAMPLE\\akim", "PC-AKIM", 1))
        self.assertTrue(ident.is_admin(company) and ident.is_sender(company))
        self.assertFalse(world.identity("x", sender=False).is_sender(company))
        self.assertFalse(world.identity("x").is_admin(company))
        paths = world.paths(ident)
        self.assertEqual(paths.share_root, world.share_root)
        self.assertTrue(paths.roaming.startswith(os.path.join(tmp, "pcs", ident.key)))
        self.assertTrue(paths.local.startswith(os.path.join(tmp, "pcs", ident.key)))
        self.assertNotEqual(paths.roaming, paths.local)
        self.assertEqual(paths.machine, os.path.join(tmp, "pcs", "PC-AKIM", "machine"))
        self.assertTrue(os.path.isdir(paths.machine))
        # Two users of one PC share its machine folder, not their own folders
        other = world.paths(world.identity("akim", pid=2))
        self.assertEqual(other.machine, paths.machine)
        self.assertNotEqual(other.local, paths.local)

    def test_first_cycle_loads_fully(self):
        import hx_share
        from hx_core import EventBus
        _, world = self.world(printers=[{}, {"placement": "encoder"}])
        ident = world.identity("jsmith")
        store = world.store(ident)
        data = hx_share.DataService(store=store, local=hx_share.LocalStore(world.paths(ident)), bus=EventBus(),
                                    identity=ident, clock=world.clock, timings=world.timings)
        data.run_once()
        self.assertEqual(data.problems, [])
        current = data.current
        self.assertEqual(current.source, "share")
        self.assertEqual(current.company.time_server, f"127.0.0.1:{world.sntp.port}")
        self.assertEqual(set(current.machines), {world.ids["machine1"], world.ids["machine2"]})
        self.assertEqual(set(current.printers), {world.ids["printer1"], world.ids["printer2"]})
        self.assertEqual((set(current.lines), set(current.products), set(current.films), set(current.print_texts)),
                         ({world.ids["line"]}, {world.ids["product"]}, {world.ids["film"]},
                          {world.ids["print_text"]}))
        self.assertEqual([m.name for m in current.line_machines(world.ids["line"])],
                         ["Line 1 Bagger 1", "Line 1 Bagger 2"])
        # The next cycle sees counter 1 again and reads only changes.json
        before = len(store.fs.calls)
        data.run_once()
        read = [path for _, path in store.fs.calls[before:]]
        self.assertTrue(read)
        self.assertTrue(all(path.endswith("changes.json") for path in read), read)
        self.assertIs(data.current.machines, current.machines)

    def test_seed_can_be_sent(self):
        import hx_message
        import hx_share
        from hx_core import EventBus
        _, world = self.world(printers=[{}, {"placement": "encoder"}])
        ident = world.identity("jsmith")
        data = hx_share.DataService(store=world.store(ident), local=hx_share.LocalStore(world.paths(ident)),
                                    bus=EventBus(), identity=ident)
        data.run_once()
        d = data.current
        company = d.company
        self.assertEqual(hx_message.validate_company(company), [])
        self.assertEqual(hx_message.validate_line(d.lines[world.ids["line"]], d), [])
        for n in (1, 2):
            self.assertEqual(hx_message.validate_machine(d.machines[world.ids[f"machine{n}"]], d), [], n)
        film = d.films[world.ids["film"]]
        self.assertEqual(hx_message.validate_film(film, d), [])
        self.assertEqual(hx_message.validate_print_text(d.print_texts[world.ids["print_text"]], company), [])
        self.assertEqual(hx_message.origin_conflicts(d, company), [])
        line = d.lines[world.ids["line"]]
        self.assertEqual([f.id for f in hx_message.films_for_line(line, world.ids["product"], d)], [film.id])
        self.assertEqual([p.id for p in hx_message.print_texts_for_film(film, d)], [world.ids["print_text"]])
        self.assertTrue(ident.is_sender(company))

    def test_script(self):
        import contextlib
        import io
        out = io.StringIO()
        stop = mock.Mock(side_effect=KeyboardInterrupt)     # Ctrl+C at the script's first wait
        patched = types.SimpleNamespace(sleep=stop, time=time.time, monotonic=time.monotonic)
        with mock.patch.object(hx_fake, "time", patched), contextlib.redirect_stdout(out):
            hx_fake.main(["--printers", "2", "--encoder"])
        lines = [line for line in out.getvalue().splitlines() if not line.startswith("#")]
        values = {}
        for line in lines:
            word, rest = line.split(" ", 1)
            self.assertEqual(word, "set" if sys.platform == "win32" else "export")
            name, value = rest.split("=", 1)
            values[name] = value if sys.platform == "win32" else hx_fake.shlex.split(value)[0]
        self.assertEqual(list(values), ["STRIPES_V2", "STRIPES_V2_ROAMING", "STRIPES_V2_LOCAL", "STRIPES_V2_MACHINE",
                                        "STRIPES_HX_USER", "STRIPES_HX_COMPUTER", "STRIPES_HX_SIDS"])
        self.assertEqual((values["STRIPES_HX_USER"], values["STRIPES_HX_COMPUTER"]), ("EXAMPLE\\jsmith", "PC-JSMITH"))
        self.assertEqual(values["STRIPES_HX_SIDS"], f"{hx_fake.SENDER_SID},{hx_fake.ADMIN_SID}")
        self.assertTrue(values["STRIPES_V2"].endswith(os.path.join("share", "v2")))
        self.assertIn("2 encoder", out.getvalue())
        # Ctrl+C stopped the fakes and removed the temp folder
        self.assertFalse(os.path.exists(values["STRIPES_V2"]))
        stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()
