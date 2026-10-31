# MapleJet Hx build spec: protocol

Part 2 of 7. This file specifies how Stripes talks to MapleJet Hx printers (`hx_protocol`: Autopilot
and the web read-back) and how the fake printer, fake time server and fake world behave (`hx_fake`),
plus the parser fixtures in `testdata/hx/`. Every name, type and boundary in architecture.md applies
unchanged. §1 lists the names this file adds inside the two modules it owns.

Contents: 0 Scope · 1 Interfaces added here · 2 HTTP transport · 3 Token and scrambling ·
4 Autopilot requests and replies · 5 Query catalogue · 6 Writes and the whitelist · 7 Never sent ·
8 Retries and timeouts · 9 The per-printer request gate · 10 Web client · 11 SME reply parser ·
12 Read-back rules · 13 Nonsense counting · 14 Messages · 15 Fake printer · 16 FakeSntp, FakeWorld
and the script · 17 Fixtures · 18 Tests · 19 OPEN items · 20 Decisions made in this spec

## 0. Scope and conventions

1. Authority: the plan, then architecture.md, then this file. Facts are tagged **(seen)** when they
   were observed on a production printer (firmware 69.75.10, web page V.72.5; the sanitized capture is
   in §17), **(manual)** when they come from the Hx Autopilot User Manual of 15 May 2022, **(inferred)**
   when they were worked out from one capture, and **OPEN** when they must be confirmed on the test unit.
   The safe default given for an OPEN item is what gets built.
2. Examples use only host 192.0.2.10, UID 1109 and password 123321 (the manual's own example; its token
   is `Yzg4MzdiMjNmZjhhYWE4YTJkZGU5MTU0NzNjZTA5OTE=`), TN 326548, "Printer 1", "Line 1 Bagger 1" and
   "Example Co".
3. `hx_protocol` uses only the standard library (`socket`, `gzip`, `zlib`, `base64`, `hashlib.md5`,
   `hashlib.sha256`, `hmac`, `secrets`, `re`, `threading`, `datetime`, `dataclasses`), `hx_core`,
   `hx_model` and `i18n` (`N_`, `add_spanish`). It never imports `urllib`, `http.client` or `ssl`. HTTP is
   written by hand on a raw socket, which architecture §6.4 allows, so no proxy setting can apply and the
   bytes are the ones that were verified. `hx_fake` may also use `http.server`, `socketserver`,
   `urllib.parse` and `random`.
4. All waiting goes through the injected `Clock` (`clock.sleep`, `clock.monotonic`), so tests can use
   `FakeClock`. Socket timeouts and the gate's `acquire(timeout=...)` are the only real-time waits in
   `hx_protocol`.
5. Every `PrinterClient` method blocks. Call them from worker threads only, never from the Tk thread.
6. Nothing here adds a dependency or touches packaging. The output stays the single-file `Stripes.exe`
   (architecture §0.6).

## 1. Interfaces added here

Names that architecture §6.4 and §6.9 already define aren't repeated, except where this file adds to a
class.

### 1.1 hx_protocol

```python
# The only requests Stripes makes (§7)
AUTOPILOT_PATH = "/"                              # POST, body starts "<Cookie>"
LOGIN_PATH = "/"                                  # POST, body starts "Type=LOG"
SME_PATH = "/simple_msg.html?sme"                 # POST: Load
SME_REPEAT_PATH = "/simple_msg.html?sme_repeat"   # POST: RepeatRequest
FONT_FILE = "/sfl.xml"                            # GET: the font list. OPEN (§10.8)
IDENTITY_FILE = "/idn.xml"                        # GET: the firmware version. OPEN (§10.8)
WEB_FILES = (FONT_FILE, IDENTITY_FILE)
EDITOR_PATH = "/simple_msg.html"                  # the 2.3 MB editor page: never requested

# Limits, in bytes
MAX_HEADER_BYTES = 16_384
MAX_AUTOPILOT_REPLY = 4_096
MAX_SME_REPLY = 262_144          # a Load reply is about 29.5 KB
MAX_FILE_REPLY = 65_536
MAX_LOGIN_REPLY = 1_048_576      # read and discarded; may be cut short
MAX_SME_DEPTH = 16               # elements open at once in an SME reply, SM included (§11.1 item 3)

PREVIEW_PREFIX = "data:image/bmp;base64,"
PREVIEW_BYTES = 20_542           # 62-byte header + 128 bytes x 160 rows
STRIPES_TN_MOD = 97
STRIPES_TN_REM = 42              # every Stripes TN: int(tn) % 97 == 42
FIRMWARE_TAGS = ("FIRMWARE", "FWVER", "FW", "VERSION", "VER")   # OPEN (§10.8)
DEV_KEY = b"stripes-hx-development-key-v1"   # public; used when hx_key is absent
SCRAMBLE_PREFIX = "hx1"

REPLY_CODES = {-1: "invalid query", 0: "done", 1: "invalid command", 2: "invalid syntax",
               3: "invalid parameter", 4: "not acceptable now", 5: "not enough data",
               6: "timeout", 7: "busy"}           # manual §3
BUSY_REPLIES = ("6", "7")

WRITE_CHILDREN: dict[str, tuple[str, ...]]        # §6.2: the children each block write may carry
PRINTER_WIDE_WRITES: tuple[str, ...]              # §7: printer-wide setting commands
UNUSED_WRITES: tuple[str, ...]                    # §7: message objects Stripes doesn't use

def xml_escape(text: str) -> str          # "&" -> "&amp;", then "<" -> "&lt;", ">" -> "&gt;"
def check_write(cmd: Command) -> None     # ForbiddenCommand unless whitelisted (§6.2)
def check_query(tag: str) -> None         # ForbiddenCommand unless tag in QUERY_SPECS
def check_reply(tag: str, text: str, *, printer: str = "") -> int | str | None
    # QUERY_SPECS check of one stripped reply (§5.1): the value, else PrinterBusy or BadReply
def decode_text(raw: bytes) -> str        # strict UTF-8, else latin-1; never fails
def clock_from(date_iso: str, time_iso: str) -> datetime
    # read("DATE") and read("TIME") -> the naive printer-local datetime

def autopilot_request(uid: int, token: str, xml: str) -> bytes      # §2.3; pure
def login_request(host: str, port: int, md5_hex: str) -> bytes
def load_body(tn: str, xid: int, slot: int) -> bytes
def sme_request(host: str, port: int, cookies: dict[str, str], *, repeat: bool, body: bytes) -> bytes
def file_request(host: str, port: int, cookies: dict[str, str], path: str) -> bytes

@dataclass(kw_only=True)
class HttpReply:
    status: int
    headers: list[tuple[str, str]]        # in the order received, names as received
    body: bytes                           # gzip already undone unless gunzip=False
    complete: bool = True                 # False only when allow_cut_short cut it short
    def header(self, name: str) -> str | None: ...    # first match, case-insensitive
    def cookies(self) -> dict[str, str]: ...           # every Set-Cookie's "name=value", up to the first ";"

def http_exchange(host: str, port: int, request: bytes, *, timeout_s: float, max_body: int,
                  label: str = "", allow_cut_short: bool = False, gunzip: bool = True) -> HttpReply
    # one connection, one request, one reply (§2.2); raises NoReply or BadReply

def new_tn() -> str                       # six digits with int % 97 == 42, from secrets
def is_stripes_tn(tn: str) -> bool
def sme_envelope(body: bytes) -> tuple[str, str, str, str] | None   # (TYPE, TN, XID, MSNO) texts
def classify_reply(body: bytes, *, tn: str, xid: int) -> str
    # "match" | "stale" | "foreign" | "odd" | "busy" (§10.5)
def decode_preview(uri: str) -> bytes     # the BMP bytes; WebReadFailed unless a 1-bit 1024 x 160 BMP
def parse_fonts(body: bytes) -> list[str] # FON1, FON2, ... in order; BadReply when there is no FON1
def parse_firmware(body: bytes) -> str | None

def key_id(key: bytes) -> str             # 8 hex characters naming a scrambling key
def is_development_key(key: bytes) -> bool   # key == DEV_KEY; deploy.md's self-check refuses it in a release
def worst_case_s(call: str, timings: Timings = TIMINGS) -> float
    # §8.3: the longest one PrinterClient call can take; lock holders pass it to PrinterLock.check

class PrinterClient:                      # additions to architecture §6.4
    label: str          # the name used in messages; ClientPool sets Printer.name (default: host)
    tn: str             # new_tn() at construction; a test may set it before the first load_slot
    xid: int            # the last XID used; 0 at construction
    busy_replies: int   # "6" and "7" replies so far (kept out of nonsense, §13)
    relogins: int       # web logins after the first one
    def probe(self) -> bool | None: ...
        # TCP connect and close under the gate, no request (§9 item 7); None when the gate was busy
    # load_slot(slot, *, deadline=None), fonts(*, deadline=None), firmware(*, deadline=None):
    # architecture §6.4; the deadline rule is §10.6 step 0
    # query(tag, *, deadline=None), read(tag, *, deadline=None), command(cmd, *, deadline=None),
    # read_twice(tag, *, deadline=None), check_uid(*, deadline=None): architecture §6.4; with a deadline the
    # gate wait is bounded, and nothing is sent that couldn't end by it, after the gate too (§9 item 2)
```

### 1.2 hx_fake

```python
def bmp_1bit(rects: list[tuple[int, int, int, int]], width: int = 1024, height: int = 160) -> bytes
    # §15.4: a white 1-bit BMP with black rectangles (x1, y1, x2, y2), top-left origin, x2 and y2 exclusive
FAKE_DEFAULT_SETTINGS: tuple[tuple[str, str], ...]    # §12.3, in the printer's order

class FakePrinter:                        # additions to architecture §6.9
    DROP: object        # override result: close the connection without a reply
    SILENT: object      # override result: hold the connection and never answer
    # Knobs: plain attributes, changed at any time (handlers read them under self.lock).
    uid: int; password: str; placement: str; fonts: tuple; firmware: str   # from the constructor
    latency_s: float; mixup: bool; busy_on_overlap: bool                    # from the constructor
    offset_fn: Callable[[float], float]   # constructor keyword, default hx_time.local_utc_offset
    base_offset_s: float                  # offset_fn(clock.time()) at construction; never changes
    def printer_clock(self) -> datetime: ...
        # datetime(1970, 1, 1) + timedelta(seconds=clock.time() + base_offset_s + clock_offset_s):
        # naive, and it never follows daylight saving, like the real printer's clock (§15.3)
    drop_after_idle_s: float | None; editor_page_bytes: int; editor_stall_s: float
    hx_name: str = " Printer 1"           # the HXNA answer, with the real printer's leading space
    mixup_p: float = 0.5
    drop_after_idle_p: float = 1.0
    overload_at: int = 8
    overload_pause_s: float = 1.0
    multi_value_hang_s: float = 4.0
    web_latency_s: float = 0.0
    web_result_delay_s: float = 0.0
    web_first_reply_fresh: bool = False
    web_reply_hook: Callable[[bytes], bytes] | None = None
    single_web_session: bool = False
    write_echo: bool = False
    gzip_files: bool = False
    files: dict[str, bytes | None] = {}   # overrides for the served small files; None means 404
    # Extra state, under self.lock
    buffer: bytes                         # the one shared SME reply buffer
    sessions: set[tuple[str, str]]        # valid (Access_token, Auth_token) values
    slot_style: dict[int, str]            # "verbose" (made by Autopilot) | "compact" (another tool)
    lots: dict[int, str]                  # LOT1..LOT9 values, all ""
    rollover: tuple[int, int] | None      # the last TD's DTRH, DTRM
    cartridge: dict[str, int]
    eco: int
    printed: list[dict]                   # one entry per bag printed (§15.7)
    def set_password(self, password: str) -> None: ...   # also ends every web session
    def reset_observations(self) -> None: ...
        # clears requests, writes, violations and printed; max_in_flight = 0

class FakeWorld:                          # additions to architecture §6.9
    ids: dict[str, str]                   # §16.2
    def paths(self, identity: Identity) -> HxPaths: ...
    def time_source(self) -> TimeSource: ...
```

## 2. HTTP transport

### 2.1 Rules

1. Plain HTTP/1.1 to `Printer.host`:`Printer.port` (80 in production). One request per TCP
   connection. The client closes the socket as soon as the reply is complete. Connections are never
   reused or pipelined.
2. `socket.create_connection` only, never `urllib.request` or anything else that honours proxy settings.
3. Within a process a request is sent only while holding that printer's gate (§9), and the socket is
   closed before the gate is released.
4. Each exchange is bounded by `timings.http_timeout_s` (2.0 s), from connect to the last byte.

### 2.2 http_exchange

1. `deadline = time.monotonic() + timeout_s`.
2. `socket.create_connection((host, port), timeout=timeout_s)`, then `sendall(request)`. Any `OSError`
   raises `NoReply`.
3. Receive with `recv(65536)`, setting the socket timeout to the time left before each call. Passing
   the deadline raises `NoReply`.
4. When `\r\n\r\n` arrives, the status line must match `HTTP/1\.[01] (\d{3})( .*)?` and each header line
   `Name: value`. A header block longer than `MAX_HEADER_BYTES`, a bad status line or
   `Transfer-Encoding: chunked` raises `BadReply(tag="", reply="HTTP framing")`.
5. With `Content-Length`, read exactly that many body bytes. Without it, read until the server closes.
   A body longer than `max_body` closes the socket and raises `BadReply(reply="reply too large")`,
   unless `allow_cut_short` is set: then reading stops at `max_body` and the reply has `complete=False`.
6. The connection closing before the headers end raises `NoReply` ("connection closed without reply",
   the idle-drop symptom). Closing, or timing out, before `Content-Length` bytes arrive raises `NoReply`,
   unless `allow_cut_short` is set and the headers are complete (`complete=False`).
7. With `Content-Encoding: gzip` and `gunzip=True`, decompress with
   `zlib.decompressobj(16 + zlib.MAX_WBITS)`, at most `4 × max_body` bytes of output. A failure raises
   `BadReply(reply="bad gzip")`.
8. The socket is closed on every path (a `with` block). Return the `HttpReply`; the caller interprets
   the status.

`NoReply` and `BadReply` raised here carry the §14 messages with `printer` = `label`.

### 2.3 Request bytes

Every line ends in CRLF. `<host>` is `host` when the port is 80, else `host:port`. Bodies are UTF-8.

Autopilot, exactly as verified by the read-only probes, the stress run and the 3-hour soak (no Host
header):

```
POST / HTTP/1.1
Content-Length: <n>
Connection: keep-alive

<Cookie><Auth_token_<uid>><token></Auth_token_<uid>></Cookie><APCMD><xml></APCMD>
```

The printer answers `Connection: close` anyway, and the client closes after the reply. For UID 1109,
`<PRIN/>` gives a 118-byte body; the manual's `<PRINT>0</PRINT>` gives 127, as the manual shows.

The login, Load, repeat and file requests copy the browser's headers, in its order (seen):

```
POST / HTTP/1.1
Host: <host>
Connection: close
Accept-Encoding: gzip
Content-Type: application/x-www-form-urlencoded
Content-Length: 58

Type=LOG&pass=<32 lowercase hex characters: the md5 of the password>&remember=on
```

```
POST /simple_msg.html?sme HTTP/1.1              (or ?sme_repeat)
Host: <host>
Connection: close
Accept-Encoding: gzip
Cookie: Access_token_<uid>=<a>; Auth_token_<uid>=<b>
Content-Type: text/plain;charset=UTF-8
Content-Length: <n>

<SM><TYPE>Load</TYPE><TN><tn></TN><XID><xid></XID><MSNO><slot></MSNO></SM>
```

The Load body is 67 bytes for TN 326548, XID 4, slot 9. The repeat body is `<SM>RepeatRequest</SM>`
(22 bytes).

```
GET /sfl.xml HTTP/1.1                           (or /idn.xml)
Host: <host>
Connection: close
Accept-Encoding: gzip
Cookie: Access_token_<uid>=<a>; Auth_token_<uid>=<b>

```

The Cookie header always lists `Access_token_<uid>` first, then `Auth_token_<uid>`.

### 2.4 Replies by status

| Request | 200 | 401 | Anything else |
|---|---|---|---|
| Autopilot | the body is the answer (§4.2) | `AuthFailed`: a wrong token, or another printer (another UID) at this address. Never retried | `BadReply(reply="HTTP <status>")`, nonsense + 1 |
| Login | the cookies (§10.1) | `AuthFailed` | `AuthFailed` |
| Load, repeat | the SME body (§10.5). An HTML body (starting `<!DOCTYPE` or `<html` in any case, after gunzip) counts as 401 | log in again once (§10.2) | classified `odd` |
| File GET | the body. An HTML body counts as 401 | log in again once | 404: the file is missing (§10.8). Other: `BadReply`, nonsense + 1 |

Replies seen: `HTTP/1.1 200 OK`, `Server: MapJet/v1.1`, `Connection: close`, `Content-Length`, and
`Content-Type` `text/plain` (Autopilot), `application/xml` (SME) or `text/html` with
`Content-Encoding: gzip` (login and pages). SME replies aren't gzipped.

## 3. Token and scrambling

### 3.1 Token

1. `make_token(password)` is
   `base64.b64encode(hashlib.md5(password.encode("utf-8")).hexdigest().encode("ascii")).decode("ascii")`.
   Manual example: "123321" has md5 `c8837b23ff8aaa8a2dde915473ce0991` and token
   `Yzg4MzdiMjNmZjhhYWE4YTJkZGU5MTU0NzNjZTA5OTE=`.
2. `password_md5(token)` is the base64-decoded ASCII text, which must fullmatch `[0-9a-f]{32}`; else
   `AuthFailed` (the "damaged" message). The web login sends it.
3. The password itself is never stored or logged. The admin screen (ui.md) stores
   `scramble(make_token(password), release_key())` in `Printer.token` and forgets the password.

### 3.2 Scrambling

The stored form is `hx1:<key id>:<payload>`. With `key` the release key:

1. `enc = sha256(b"stripes-hx-enc" + key).digest()`, `mac = sha256(b"stripes-hx-mac" + key).digest()`
   and `key_id(key) = sha256(b"stripes-hx-key-id" + key).hexdigest()[:8]`.
2. `password_md5(token)` must succeed (else `ValueError`, a programming error); `data =
   token.encode("ascii")`.
3. `nonce = secrets.token_bytes(16)`.
4. The keystream is `sha256(enc + nonce + i.to_bytes(4, "big")).digest()` for i = 0, 1, …, joined;
   `ct = bytes(d ^ k for d, k in zip(data, keystream))`.
5. `tag = hmac.new(mac, nonce + ct, hashlib.sha256).digest()[:16]`.
6. `payload = base64.urlsafe_b64encode(nonce + ct + tag)` with the `=` padding removed.

`unscramble(stored, key)`:

1. Split on ":" into exactly three parts, the first `hx1`; else `AuthFailed` (damaged).
2. The second part differs from `key_id(key)`: `AuthFailed` (made by a different Stripes build).
3. Decode the payload after restoring the padding. Bad base64, or fewer than 16 + 1 + 16 bytes:
   damaged.
4. Recompute the tag; `hmac.compare_digest` false: damaged.
5. Decrypt. The result must be ASCII and pass `password_md5`, else damaged. Return it.

Two scrambles of one token differ (the nonce). `hmac` and `hashlib.sha256` work without `_hashlib`,
which the exe leaves out (CPython's built-in `_sha2` and `hmac`'s pure-Python path).

### 3.3 Keys

1. `release_key()`: `import hx_key` and return `hx_key.KEY`, which must be `bytes` of at least 16 bytes
   (else `RuntimeError("hx_key.KEY must be at least 16 bytes")`). An `ImportError` returns `DEV_KEY`.
2. `DEV_KEY` is in the public source, so tokens scrambled with it are only obscured. deploy.md's
   self-check fails a release build when `is_development_key(release_key())`.
3. A token scrambled with one key can't be read with another. The administrator types the password
   again; the `AuthFailed` message says so.

### 3.4 ClientPool

1. `client(printer)` keeps one `PrinterClient` per `printer.id`, rebuilt when `host`, `port`, `uid` or
   `token` differ from the cached one. It is thread-safe (its own lock).
2. `printer.token == ""` raises `AuthFailed` "No password is saved for {printer}."
3. When `unscramble` fails it raises `AuthFailed(Msg("{printer}: {problem}", {"printer": printer.name,
   "problem": <the unscramble Msg>}))`.
4. It sets `client.label = printer.name`.

## 4. Autopilot requests and replies

### 4.1 Commands

1. One command per request. The printer silently ignores a second command in the same APCMD (seen), so
   `check_write` and `check_query` refuse any XML that isn't exactly one element.
2. A query is `<TAG/>`: the printer treats anything ending in `/>` as a query (manual §5). A write is
   `<TAG>value</TAG>` or `<TAG><CHILD>value</CHILD>…</TAG>`.
3. `hx_model.Command.set` and `Command.block` turn every value into text with `str()` and then escape it
   (architecture §5.7): `&` becomes `&amp;`, then `<` becomes `&lt;` and `>` becomes `&gt;`. Nothing else
   is escaped, and non-ASCII characters go as UTF-8 (manual §4). `hx_protocol.xml_escape` is the same
   rule, and a test checks that both agree. OPEN: whether the printer decodes the entities. §12.2 keeps
   it safe either way.

### 4.2 Reply text

1. The body is decoded with `decode_text` and stripped of whitespace on both sides. That removes HXNA's
   leading space (seen: `" Name"`).
2. An empty body after stripping is a bad reply. It wasn't seen; the printer sends `None` for an empty
   value.

### 4.3 Codes

Every Autopilot answer is HTTP 200 with bare text (seen, manual §3), so a code can look like a value.

| Reply | To a read (`read`, `query`) | To a write (`command`) |
|---|---|---|
| `-1`, invalid query | never a value: `BadReply`, nonsense + 1, asked once more | `CommandRefused(code=-1)` |
| `0` | a value if the spec allows it | done |
| `0`, then whitespace, `,` or `;` and more text | checked by the spec (fails for an int) | done; the rest is the manual's "second value" and is ignored (OPEN: exact form) |
| `1` to `5` | a value if the spec allows it, else `BadReply` | `CommandRefused(code=n)` |
| `6` timeout, `7` busy | never a value: busy, `busy_replies` + 1, asked once more, then `PrinterBusy` | `CommandRefused(code=6 or 7)`; never repeated |
| anything else | checked by the spec | `BadReply`, nonsense + 1 (the write may or may not have happened) |

So `6` and `7` are never values, even where the range would allow them (a counter, BOLD 7): such a
reading is asked again and may end as `PrinterBusy`. The values `0` to `5` pass only where the spec
allows them. A stray code can still pass there (PRIN `1`), so anything that changes a state or raises an
alert is read a second time (states.md, `read_twice`).

### 4.4 PrinterClient methods

The internal helper `_ask(xml) -> str` is always called with the gate held. It makes one
`http_exchange` (`max_body=MAX_AUTOPILOT_REPLY`), sets `last_request_at = clock.monotonic()` when the
exchange ends however it ends, then applies §2.4 and §4.2.

`query(tag, *, deadline=None) -> str` (the deadline rule of §9 item 2 applies to `query`, `read`,
`read_twice`, `check_uid` and `command`)

1. `check_query(tag)`: `ForbiddenCommand` for any tag outside `QUERY_SPECS`. Every
   `FORBIDDEN_QUERY_TAGS` tag is outside it.
2. Take the gate (§9).
3. At most two attempts of `_ask(f"<{tag}/>")`. After a `NoReply`, or a `6` or `7` (`busy_replies` + 1),
   sleep `read_retry_s` still holding the gate, then make the second attempt. Any other answer is
   returned as it is, without a spec check. With a deadline, the second attempt is made only if it can
   still end by it, checked before the sleep and again after it; otherwise the first error is raised
   (after a `NoReply` the gate is still held as long as the deadline allows).
4. After a second failure, raise the last `NoReply` or `PrinterBusy`.

`read(tag, *, deadline=None) -> int | str | None`

1. As `query`, but every answer goes through `check_reply`. A `BadReply` from the check adds 1 to
   nonsense and counts as a failed attempt, like `NoReply`.
2. **At most two requests per `read()`** in total, whatever the reasons. architecture's "re-asked once"
   for `NoReply` and busy and its "asked once more" for a failed check share this one budget.
3. After two failures, raise the last error: `NoReply`, `PrinterBusy` or `BadReply`.
4. `AuthFailed` is raised at once.

`read_twice(tag, *, deadline=None)`

1. `a = read(tag)`, release the gate, `clock.sleep(second_reading_s)`, `b = read(tag)`; each read with the
   deadline.
2. If `a != b`: nonsense + 1 and `BadReply(tag=tag, reply=f"{a} / {b}")`. Otherwise return `a`.

`check_uid(*, deadline=None)`

1. `read("HXUI")` equal to `uid`: return. Each read here is made with the deadline.
2. Otherwise sleep `second_reading_s` with the gate released and read again. The same other UID twice
   raises `WrongPrinter(expected=uid, got=str(value))`. Two different answers: nonsense + 1 and
   `BadReply`.

`command(cmd, *, deadline=None)`

1. `check_write(cmd)` before anything else (`ForbiddenCommand`).
2. Take the gate (with a deadline, §9 item 2).
3. Warm-up: if `last_request_at is None`, or `clock.monotonic() - last_request_at >= idle_warmup_s`,
   run the `read("HXUI")` algorithm with the gate held (and the deadline). A different UID raises
   `WrongPrinter`, and any failure is raised as it is. In both cases the write is not sent.
4. With a deadline: `clock.monotonic() + http_timeout_s > deadline` raises `PrinterBusy` "{printer} is
   busy with another request from this PC.", the write not sent (the warm-up read, or a frozen PC,
   used the time up). Otherwise send the write once: `_ask(cmd.xml)`.
5. `NoReply`: sleep `read_retry_s` holding the gate, then raise `NoReply` "No reply from {printer} to
   {command}. It may or may not have been done."
6. First token `0`: return. An integer: `CommandRefused(tag=cmd.tag, code=n)`. Anything else:
   nonsense + 1 and `BadReply(tag=cmd.tag, reply=text[:40])`.
7. The write is never repeated, whatever happened.

`probe()`: see §9.

## 5. Query catalogue

### 5.1 How a reply is checked (check_reply)

1. `"6"` or `"7"` raises `PrinterBusy`; `"-1"` raises `BadReply`.
2. `"None"` returns `None` when `none_ok`, else `BadReply`.
3. `int`: the text must fullmatch `-?\d{1,10}`; then it must be in `allowed` when given, else within
   `min` and `max`.
4. `str`: 1 to `max` characters (for this kind `max` is the length), no control characters, and not an
   integer from −1 to 7, so a stray code can't pass as a name or an ink.
5. `date`: `(\d{1,2})/(\d{1,2})/(\d{4})` as day/month/year (manual), a real calendar date, year 2000 to
   2050. Returned as `"YYYY-MM-DD"`.
6. `time`: `(\d{1,2}):(\d{2}):(\d{2})`, hour 0 to 23, minute and second 0 to 59. Returned as
   `"HH:MM:SS"`.
7. A failure raises `BadReply(tag=tag, reply=text[:40])` with the §14 message.

### 5.2 QUERY_SPECS

This is the complete set; `query()` and `read()` refuse anything else. `ReplySpec` arguments are
`(kind, min, max, allowed, none_ok)`.

| Tag | ReplySpec | Returns | Notes | Typical use (the owner decides) |
|---|---|---|---|---|
| HXUI | int 1..4,294,967,295 | the UID | `check_uid` and the warm-up read | every Send; poller (states.md) |
| HXNA | str, max 50, none_ok | the printer name | leading space trimmed (§4.2) | daily log |
| PRIN | int, allowed (0, 1) | 1 = printing on | a stray `0` or `1` passes the check, so state changes need a second reading | every 5 s; Send; Stop; Resume |
| MSGNAM | str, max 20 (`MESSAGE_NAME_MAX`), none_ok | the printing message's name | `None` = unnamed (seen); Stripes names carry a check letter (send.md) | every 5 s |
| MSCNT | int 0..4,294,967,295 | message counter | OPEN: does it reset on a message change? | bag count when `Company.bag_counter == "MSCNT"` |
| PMCNT | int 0..4,294,967,295 | maintenance (print) counter | rises only by believable amounts (states.md) | bag count (the default) |
| DATE | date | `"YYYY-MM-DD"` | printer-local; the printer sends dd/mm/yyyy | every 60 s, with TIME |
| TIME | time | `"HH:MM:SS"` | printer-local, 24-hour | every 60 s |
| INKT | str, max 50, none_ok | the ink name | the caller checks it is on `Company.inks` | every 60 s; the Send ink check |
| INKL | int, allowed (0, 1, 2, 3, 4) | 0 not detected, 1 normal, 2 low, 3 critical, 4 none | | every 60 s |
| PHTP | int −30..100 | head temperature, °C | | every 60 s |
| ECOMOD | int, allowed (0, 1) | economy mode | | daily |
| ENCST | int, allowed (0, 1) | encoder on | a print parameter of the printing message, not printer-wide (manual) | Send check; daily |
| ENWHD | int 20..150 | wheel diameter, mm | printer-wide, set at installation | daily, encoder machines |
| ENPPR | int 1..1,000,000 | pulses per revolution | printer-wide | daily, encoder machines |
| ENTPM | int, allowed (0, 1) | two-phase | printer-wide | daily, encoder machines |
| PLDEL | int 10..2550 | fire delay, ns | cartridge setting | daily |
| MNPL | int 50..1500 | main pulse, ns | cartridge setting | daily |
| PRPL | int 50..1000 | pre-pulse, ns | cartridge setting | daily |
| PLVO | int 7500..13000 | pulse voltage, mV | cartridge setting | daily |
| PLWR | int 20..45 | pulse warming, °C | cartridge setting | daily |
| NZRO | int, allowed (1, 2, 3) | nozzle row | cartridge setting | daily |
| DELAY | int 0..120,000 | the printing message's delay **in ms** | §5.3 | diagnostics only |
| PRNSPD | int 0..2500 | the printing message's speed, cm/min | §5.3 | diagnostics only |
| RESMOD | int, allowed (0, 1, 2, 3) | the printing message's resolution | | diagnostics only |
| BOLD, WDTH | int 1..10 | | | diagnostics only |
| QUANT | int 1..100 | | | diagnostics only |
| ROTAT, MIRR, REVRS, AUTREP | int, allowed (0, 1) | | | diagnostics only |

Not in the catalogue, so never sent: CPPARM, ODPARAM and RS232 (multi-value queries; they hang about
4 s and return 6, seen), BEAC (returns −1, seen; `BEACON_SUPPORTED = False`), LOT1 to LOT9 (lot slots
aren't used), YEAR, MNTH, DAY, HOUR, MIN (DATE and TIME are enough), INSH, EXTDATA, ENSPD, RSBR, RSDB,
RSPRT, RSSB, ODTO and ODTE.

### 5.3 Units and normalisations

| Value | Sent as | Read back by an Autopilot query | Read back by the web Load (FCDOC) |
|---|---|---|---|
| Delay (delay machines) | `DELAY` in µs, 0..120,000,000 (manual) | the `DELAY` query reports **ms**: 284 for a stored 284,176 µs (seen). Compare it with `us // 1000` (or `round(us / 1000)`; they agree except at exactly .5 ms) | `DELAY` in µs, compared exactly. OPEN: the plan's own test item ("the delay reads back in microseconds") |
| Print position (encoder machines) | `DLYMM` in mm, 0..120,000,000 | no query exists | `DLYMM` in mm, exactly. Unused while ENCST is 0 |
| Speed | `PRNSPD`, cm/min (OPEN units); the manual's maximum is 1200, the printer reported 2500 | `PRNSPD` | `PRNSPD`, exactly (OPEN: is 2400 stored as sent?) |
| Font size | `SIZE` 1..10 | none | one lower, 0..9 (seen: 3 read 2, 4 read 3) |
| Printer name | never sent | HXNA with a leading space, trimmed | none |
| Unnamed message | none | MSGNAM `None`, returned as `None` | MSGNAM empty, `""` |
| Date and time | the TD fields (§6.1) | DATE dd/mm/yyyy and TIME hh:mm:ss, returned in ISO form | CTDAT TXT is the date as rendered when the Load ran |

DATE and TIME are two requests, so a pair read across midnight can be a day out. The confirming second
reading that states.md requires before acting removes that case. `clock_from` combines the two strings
and judges nothing.

## 6. Writes and the whitelist

### 6.1 Write catalogue

The only writes Stripes sends are `hx_model.ALLOWED_WRITE_TAGS`. Ranges are the manual's unless marked.

| Tag | Form | Children and values that `check_write` allows | Effect | Sent by (send.md) |
|---|---|---|---|---|
| CRTMSG | set | the name: 0 to 20 characters, no control characters (empty is allowed, for the end-of-test empty message) | starts a new design, clearing the previous one | build; end of test |
| CTEXT | block | OBJID 10000..20000; POS `x,y`; TXT up to 250 characters; FONTIND 1..6; SIZE 1..10; MARG `x,y`; PREF and POSF up to 100 characters; XRID and YRID 10000..20000; SXAS and SYAS 0..1 | adds a text item | build |
| CTDAT | block | as CTEXT without TXT, plus FRMT 1 to 100 characters, DAY 0..999, MON 0..999, YEAR 0..99 | adds a date item | build |
| CPPARM | block | DELAY 0..120,000,000; PRNSPD 1..2500 (OPEN: the manual says up to 1200); ENCSCL 0..100; DLYMM 0..120,000,000; BOLD 1..10; WDTH 1..10; QUANT 1..100; QDELAY 0..120,000,000; QDLYMM 0..120,000,000; ENCST, ROTAT, MIRR, REVRS, AUTREP 0..1; RESMOD 0..3 | the design's print parameters | build |
| SAVEMSG | set | 9 or 10 only (`hx_model.SLOTS`) | stores the design in the slot and leaves a TYPE Save result in the web buffer (§10.5) | build; end of test |
| PRINTMSG | set | 9 or 10 only | switches the printing message | switch; test steps |
| PRINT | set | 0 or 1 | printing off or on | switch; Stop; Resume; end of test |
| TD | block | DAY 1..31; MNTH 1..12; YEAR 2000..2050; HOUR 0..24; MIN 0..59; DTRH 0..24; DTRM 0..59 (the manual says HOUR and DTRH 1..24 and DTRM 1..59: OPEN, §19) | sets the printer's clock and its date rollover; there is no seconds field | set clock; Set clocks; the daylight-saving clock set |

`POS` and `MARG` must fullmatch `-?\d{1,5},-?\d{1,5}`; integers must fullmatch `-?\d{1,10}`. Text lengths
count characters before escaping.

### 6.2 check_write

1. `cmd.write` must be true and `cmd.tag` in `ALLOWED_WRITE_TAGS`.
2. `cmd.xml` must fullmatch `<TAG>(.*)</TAG>` (DOTALL) for its own tag: exactly one element, so no
   second command can ride along.
3. Set forms (CRTMSG, SAVEMSG, PRINTMSG, PRINT): the inner text contains no `<` and passes the value
   check above.
4. Block forms (CTEXT, CTDAT, CPPARM, TD): the inner text must be entirely a sequence of `<C>v</C>`
   leaves (`v` without `<`), each `C` in `WRITE_CHILDREN[tag]`, none repeated, each value in range.
5. Any failure raises `ForbiddenCommand(Msg("Stripes tried to send a command it never sends:
   {command}.", {"command": cmd.tag}), tag=cmd.tag)`. A range error is a programming error too, because
   `hx_message` validates first (send.md).

```python
_ITEM = ("OBJID", "POS", "FONTIND", "SIZE", "MARG", "PREF", "POSF", "XRID", "YRID", "SXAS", "SYAS")
WRITE_CHILDREN = {
    "CTEXT": _ITEM + ("TXT",),
    "CTDAT": _ITEM + ("FRMT", "DAY", "MON", "YEAR"),
    "CPPARM": ("DELAY", "PRNSPD", "ENCSCL", "DLYMM", "BOLD", "WDTH", "QUANT", "QDELAY", "QDLYMM",
               "ENCST", "RESMOD", "ROTAT", "MIRR", "REVRS", "AUTREP"),
    "TD": ("DAY", "MNTH", "YEAR", "HOUR", "MIN", "DTRH", "DTRM"),
}
```

`LANG` is left out on purpose: its meaning is unknown (§11.3).

### 6.3 Replies to writes

Every write in the test was answered `0` on the first try (CRTMSG, CTEXT, CTDAT, CLOT, SAVEMSG; seen).
The manual says a one-parameter setting command may answer `0` followed by the new value. The client
accepts both (§4.3: the first token is `0`). PRINT, PRINTMSG, TD and CPPARM have never been sent to a
real printer: OPEN.

## 7. Never sent

| What | Why | Enforced by |
|---|---|---|
| SETPPARAM ("set print parameters of the printing message") | changes the message printing now | `check_write` (not whitelisted); a fake violation; the send tests |
| CRT, DEV, ENC, RS232PRM, AUTONOT, ODPARAM (set), RSTPMC, HXNA (set), EXBECMD, ECOMOD (set), LOT1 to LOT9 (set), together `PRINTER_WIDE_WRITES` (SETPPARAM is in it too) | printer-wide settings: cartridge, device and sensor, encoder, serial port, auto-notify, online data, counter reset, printer name, beacon output mode, economy mode, lot slots | `check_write`; fake violation "printer-wide command <TAG>"; a test sends each through `command()` and asserts `ForbiddenCommand` and no request |
| CLOT, CCONT, CSHIF, CEXTN, CBARC, together `UNUSED_WRITES` | message objects not in the first version: lot slots, counters, shifts, external data, barcodes | `check_write`; fake violation "write not whitelisted <TAG>" |
| SAVEMSG or PRINTMSG with a slot other than 9 or 10 | Stripes writes only its own two slots | `check_write`; fake violation "write outside slots 9 and 10" |
| SAVEMSG to the printing slot while printing is on | the idle-slot rule (send.md) | send.md; fake violation "SAVEMSG to the printing slot" |
| Two commands in one APCMD | the second is ignored without an error | `check_write`, `check_query`; fake violation |
| The queries CPPARM, ODPARAM, RS232 | they hang about 4 s and return 6 | `check_query`; fake violation "multi-value query <TAG>" |
| Any query outside `QUERY_SPECS` (BEAC, LOTn, …) | not needed; keeps the surface small | `check_query`; fake violation "query not in catalogue <TAG>" |
| `GET /simple_msg.html` (the editor page) | 2.3 MB; downloading it stalled the printer for about 5 s | no builder exists; fake violation "editor page requested" |
| Any other web path, and any SME TYPE except Load | not needed | only the §2.3 builders exist; fake violation "web file requested <path>" |
| An automatic repeat of a write | the first one may have been done | `command()` sends once (§4.4) |

## 8. Retries, timeouts and the warm-up read

### 8.1 Rules

| Situation | What the client does |
|---|---|
| A read gets no reply (refused, timed out, or closed without a reply) | sleeps `read_retry_s` (0.5 s) holding the gate, then asks once more |
| A read gets `6` or `7` | the same |
| A read fails its sense check, or gets `-1` | throws it away (nonsense + 1), then the same |
| The first request after a quiet spell | no special case for reads: the rules above cover the idle drop. The soak's 5 failures in 9,536 requests were all the first request after about 4.7 s of quiet, and a retry worked |
| A write after `idle_warmup_s` (3.0 s) of quiet, or as the client's first request | a checked HXUI read first (§4.4). If it fails, the write isn't sent |
| A write gets no reply, a code, or nonsense | raises; never repeated |
| A Load gets no reply | sleeps `read_retry_s` and sends one new Load with the next XID (§10.6) |
| A repeat gets no reply, or `6`/`7` | counts as one of the repeats; carries on within the cap |
| A web request gets 401 or the login page | logs in again, once per `load_slot`, `fonts` or `firmware` call, then starts that call's request over (§10.2) |
| The login gets no reply | sleeps `read_retry_s` and tries once more (logging in changes nothing on the printer) |
| Any `NoReply` | the gate stays held for `read_retry_s` before anything else is sent: the printer may still be working on the request |
| Autopilot 401, `WrongPrinter`, `ForbiddenCommand` | never retried |

"Quiet" means since this client's last exchange ended (`last_request_at`). Other PCs' requests aren't
known, but under the lock and poller-assignment rules no other PC talks to the printer meanwhile.

### 8.2 Timings used

These are `hx_model.Timings` fields (architecture §5.1); nothing here adds or changes one.

| Field | Default | Used for |
|---|---|---|
| `http_timeout_s` | 2.0 | each exchange, connect to last byte |
| `read_retry_s` | 0.5 | the wait before a second attempt; holding the gate after `NoReply`; the gap before each `sme_repeat` |
| `idle_warmup_s` | 3.0 | the warm-up read before a write |
| `gate_wait_s` | 30.0 | the longest wait for this process's gate |
| `web_repeat_max` | 5 | repeats per Load |
| `web_repeat_window_s` | 3.0 | no repeat starts later than this after the Load was sent |
| `second_reading_s` | 1.0 | `read_twice`, `check_uid` |

### 8.3 Durations callers can rely on (default timings)

| Call | Typical (seen) | Worst case |
|---|---|---|
| `read`, `query` | 80 ms (67–128 ms) | 2 × (2.0 + 0.5) = 5.0 s |
| `command` | 80 ms; 160 ms with the warm-up read | 5.0 + 2.0 + 0.5 = 7.5 s |
| `read_twice`, `check_uid` after a mismatch | 1.2 s | 11 s |
| `load_slot` | 2.1–2.3 s: the Load 0.8–0.9 s, the 0.5 s gap, one repeat 0.8–0.9 s; about 1 s more when it logs in | about 30 s without a deadline (a login, a dropped Load, the 3 s window, one re-login and a second round); with a deadline, the deadline (§10.6 step 0) |
| `fonts`, `firmware` | about 0.2 s (OPEN) | about 15 s |

`worst_case_s(call, timings)` returns these worst cases from the timings, so they scale with
`Timings.scaled`. With `h = http_timeout_s`, `r = read_retry_s`:

| `call` | Value | Default |
|---|---|---|
| `"read"`, `"query"`, `"probe"`, `"login"` | `2 × (h + r)` | 5.0 s |
| `"command"` | `2 × (h + r) + h + r` | 7.5 s |
| `"read_twice"`, `"check_uid"` | `4 × (h + r) + second_reading_s` | 11.0 s |
| `"load_slot"` | `2 × web_repeat_window_s + 2 × h` | 10.0 s |
| `"fonts"`, `"firmware"` | `6 × (h + r)` | 15.0 s |

Any other name raises `ValueError`. For the three web calls (`load_slot`, `fonts`, `firmware`) the
value is the budget a caller gives the call as a deadline: lock holders call them with `deadline =
clock.monotonic() + worst_case_s(call)`, or the lock's lease end when that is earlier (send.md §10.5;
the poller with its own, earlier deadline, states.md §6.4), so a web call can never run longer. Lock
holders give every Autopilot call the lease end as its deadline too. A web call cut short by its deadline raises `WebReadFailed`
(`load_slot`) or `BadReply` (`fonts`, `firmware`, "didn't send its font list"), which is a failed read:
the machine goes red in a Send, and the poller's message check counts a failed Load (states.md §6.5).

One PC can poll 12 printers on separate threads because each printer still sees one request at a time
(architecture decision 2).

## 9. The per-printer request gate

1. `GateRegistry.gate(printer_key)` returns one `threading.Lock` per `f"{host}:{port}"`, created on first
   use under the registry's own lock. The app uses `PROCESS_GATES`; each simulated PC in a test gets its
   own `GateRegistry()`.
2. Every public `PrinterClient` method takes the gate with `lock.acquire(timeout=gate_wait_s)` (real
   time). A timeout raises `PrinterBusy` "{printer} is busy with another request from this PC."
   `query`, `read`, `read_twice`, `check_uid`, `command` and `login` take an optional `deadline` (a
   `clock.monotonic()` time), as the web calls do (§10.6 step 0). With one, the call may start no later
   than `latest = deadline − worst_case_s(call)` (for `load_slot`, `fonts` and `firmware`, `deadline −
   http_timeout_s`; `read_twice` and `check_uid` apply it to each of their reads): the gate is taken with
   `timeout=min(gate_wait_s, max(0.0, latest − clock.monotonic()))`, and a gate acquired when
   `clock.monotonic() > latest` is released at once. Either way nothing is sent; `query`, `read`,
   `command` and `login` raise `PrinterBusy` "{printer} is busy with another request from this PC.", the
   other web calls their §10.6 step 0 error. After the gate, the deadline is checked again before every exchange, as
   web calls do: the write after a command's warm-up read, a read's second attempt (§4.4). So a caller
   with a deadline never waits for the gate past it, and never sends a request that could still be
   running after it, even when its PC froze in between (the poller, states.md §6.4 step 7; lock
   holders, whose deadline is the lock's lease end, send.md §10.5).
3. What one acquisition covers:
   - `query`, `read`: both attempts and the wait between them.
   - `command`: the warm-up read and the write.
   - `load_slot`: the login, the Load, every repeat and the gaps, because any other request in between
     could replace the printer's shared reply buffer.
   - `fonts`, `firmware`, `login`: the login if needed and the requests.
   - `read_twice`, `check_uid`: each read separately; the gate is free during `second_reading_s`.
4. The gate is a non-reentrant `Lock`. Public methods never call other public methods while holding
   it; they share internal `_..._locked` helpers.
5. An exchange's socket is closed before the gate is released. After a `NoReply` the gate is held a
   further `read_retry_s` (§8.1).
6. `PrinterClient` state (`tn`, `xid`, the cookies, the counters, `last_request_at`) changes only while
   holding the gate (architecture §7.1, rule 5).
7. `probe()` takes the gate with `acquire(blocking=False)`. When the gate is busy it returns `None` at
   once, without connecting (the caller counts nothing, storage.md §12.3). Otherwise it calls
   `socket.create_connection((host, port), timeout=http_timeout_s)`, closes at once and releases the
   gate. It returns `True` when it connected and `False` when the connect failed, sends no bytes and
   leaves `last_request_at` alone. It exists so the reachability check (architecture §11) never
   overlaps a request from this PC; storage.md only probes printers that no PC is polling. OPEN: that
   a bare connect doesn't disturb a printing printer.
8. Across PCs, one request at a time comes from the printer lock and poller assignment (architecture
   §7.3, storage.md, states.md), not from this gate.

## 10. Web client

### 10.1 Login and cookies

1. `login()`, or the first `load_slot`, `fonts` or `firmware` call, sends `login_request(host, port,
   password_md5(token))` read with `max_body=MAX_LOGIN_REPLY`, `allow_cut_short=True` and `gunzip=False`.
   Only the status line and the `Set-Cookie` headers matter; the gzipped page is discarded unread.
2. Success is status 200 with both `Access_token_<uid>` and `Auth_token_<uid>` among the cookies (seen:
   two `Set-Cookie` headers without attributes). Both values are kept in memory only, in that order, and
   never written to disk or a log.
3. Anything else (no cookies, a 401, another status) forgets any cookies and raises `AuthFailed`
   "{printer} refused the password, or another printer is at its address." OPEN: what the printer
   answers to a wrong password. A reply `http_exchange` can't frame (a bad status line or header
   line, chunked, a header block too long: `BadReply("HTTP framing")`) is "anything else" too: it
   raises `AuthFailed`, never `BadReply`, so `login()` raises only `AuthFailed` or `NoReply` (and
   `PrinterBusy` for the gate) and the poller shows `login_failed` (red) instead of dropping the
   cycle unjudged. It also counts as nonsense (§13 item 7).
4. `NoReply`: one more try after `read_retry_s`, then `NoReply`.
5. The first successful login doesn't count in `relogins`; each later one adds 1.

### 10.2 Logging in again

1. A Load, repeat or file request answered 401, or 200 with an HTML body (§2.4), means the session has
   gone. Forget the cookies, log in again and start that request over (a Load starts again with a new
   XID and a fresh repeat count and window).
2. At most once per `load_slot`, `fonts` or `firmware` call. A second 401 raises `AuthFailed`.
3. OPEN: whether two web clients (two PCs) keep separate sessions. If the printer keeps only one, PCs
   taking turns log in again each time, and nothing else changes.

### 10.3 Load and Thumbnail

1. The Load body is `<SM><TYPE>Load</TYPE><TN>{tn}</TN><XID>{xid}</XID><MSNO>{slot}</MSNO></SM>`, with
   `slot` 0..10. Slot 0 means the message printing now, and the reply carries its real slot (seen: 1).
   `load_slot` raises `ValueError` for any other slot number.
2. The repeat body is `<SM>RepeatRequest</SM>`, sent to `SME_REPEAT_PATH`. Repeats don't take a new XID.
3. A Thumbnail (`<TYPE>Thumbnail</TYPE>` with the same fields) returns only the PRV part (seen).
   Stripes never sends it: a Load returns the same picture plus the design for the same round trip. The
   parser and the classifier still recognise Thumbnail replies that other clients leave behind.
4. Stripes sends no other TYPE.

### 10.4 TN and XID

1. `tn = new_tn()`, once per `PrinterClient`: `str(97 * k + 42)` with `k = 1031 + secrets.randbelow(9278)`,
   so it is six digits from 100,049 to 999,918 and `is_stripes_tn` holds. The web editor also picks a
   random six-digit TN once per session (seen). OPEN: that the printer echoes any TN.
2. `xid` starts at 0. Each Load, including a re-sent one, first does `xid += 1`. The printer echoes TN
   and XID in its result (seen).
3. A reply matches when its TYPE is `Load`, its TN equals `self.tn` and its XID equals the Load's XID
   written in decimal.
4. `is_stripes_tn(tn)`: six digits, the first not 0, and `int(tn) % 97 == 42`. A random browser TN
   passes about one time in 97. It tells another Stripes PC's leftover result (expected) from another web
   client's (something else is talking).

### 10.5 Reply classes (classify_reply)

The printer keeps one reply buffer for every client. A Load's own reply is normally the previous result
in that buffer, possibly another client's (seen: every Load needed one repeat).

| Class | When | Seen as | Counts |
|---|---|---|---|
| `busy` | the body, stripped, is `6` or `7` | not seen on SME (OPEN) | `busy_replies` + 1 |
| `match` | the envelope's TYPE is `Load`, its TN ours and its XID the current Load's | the repeat's reply | none |
| `stale` | TN empty, any TYPE: a result Autopilot left, such as `<TYPE>Save</TYPE><TN></TN><XID></XID><MSNO>9</MSNO>` after SAVEMSG 9 | §17.6 C | none |
| `stale` | TN ours and XID a smaller number: our own previous Load | our own previous result | none |
| `stale` | TN not ours but `is_stripes_tn(TN)`: another Stripes PC's leftover | multi-PC | none |
| `foreign` | TN not empty and not a Stripes TN: another web client (a browser, Hx-Manager) used the printer | TN 834854, XID 2, TYPE Load, MSNO 1 | nonsense + 1 |
| `odd` | anything else: no `<SM>` envelope; TN ours with an XID that isn't a number, is larger, or is equal on a TYPE other than Load; an HTTP status other than 200 or 401 | none | nonsense + 1 |

`sme_envelope(body)`: after leading whitespace the body must start with `<SM>` and contain `</SM>`.
It returns the texts of the first `<TYPE>`, `<TN>`, `<XID>` and `<MSNO>` leaves that come before any
`<FCDOC>` or `<PRV>`, decoded with `decode_text`. A missing TN, XID or MSNO gives `""`; a missing TYPE
gives `None`.

### 10.6 load_slot(slot, *, deadline=None)

0. `deadline` is a `clock.monotonic()` time, or None for no limit. With a deadline, before every
   exchange of the call (the login, a Load, a re-sent Load, a repeat, a login again) and before every
   `read_retry_s` sleep, the client checks that `clock.monotonic() + http_timeout_s (+ read_retry_s for
   a sleep) <= deadline`; if not, it starts nothing more and raises `WebReadFailed` "{printer} didn't
   send back the message in slot {slot} in time." (counted like any `WebReadFailed`, not as nonsense).
   Waiting for the gate counts too (§9 item 2): it is bounded by the deadline, and a gate acquired too
   late is released at once with the same error, so the call returns by the deadline.
1. `ValueError` unless 0 ≤ slot ≤ 10. Take the gate.
2. If there are no cookies, log in (§10.1).
3. `xid += 1`, `started = clock.monotonic()`, send the Load (`sme_request(repeat=False)`,
   `max_body=MAX_SME_REPLY`).
   - `NoReply`: if this was the first Load of the call, sleep `read_retry_s` and repeat step 3;
     otherwise raise `NoReply`.
   - 401 or HTML: log in again (§10.2) and repeat step 3. A second time raises `AuthFailed`.
4. Classify the reply. `match` goes to step 7. Anything else is counted (§10.5) and the call carries on.
5. `repeats = 0`, then loop:
   1. `clock.sleep(read_retry_s)`.
   2. If `repeats == web_repeat_max`, or `clock.monotonic() - started >= web_repeat_window_s`, stop and
      raise `WebReadFailed` "{printer} didn't send back the message in slot {slot} in time."
   3. `repeats += 1` and send the repeat. `NoReply`: next turn. 401 or HTML: log in again and go back to
      step 3 (once, as above). Otherwise classify it: `match` goes to step 7; anything else is counted
      and the loop goes on.
6. With default timings and the printer's 0.8 s replies, about two repeats fit in the window. With no
   fake latency and a `FakeClock`, exactly 5 do.
7. Parse it fully with `parse_sme_reply(body)`. A `WebReadFailed` there adds 1 to nonsense and is
   raised again with this client's label.
8. Check that `design` isn't `None`, and that MSNO equals `slot` when `slot` isn't 0, or is 1..10 when it
   is. Otherwise nonsense + 1 and `WebReadFailed` "{printer} sent back slot {got} instead of slot
   {slot}." (or the "doesn't recognise" message when MSNO is missing or 0).
9. Set `design.read_at = clock.time()`, `design.tn = self.tn` and `design.xid` to the matched XID, and
   return it.

Logging in again restarts the window and the repeat count, once. The gate is held from step 1 to the
end.

### 10.7 What the caller gets

A `MessageDesign` built as §11.3 describes: the values as the printer stored them (SIZE one lower, the
date rendered, OBJIDs assigned). `hx_protocol` normalises nothing; `hx_message.compare` and
`fingerprint` (send.md) apply the rules in §12.

### 10.8 fonts() and firmware()

The plan requires each printer's font list ("refuses to send if it differs") and its firmware version
(the daily check). No Autopilot query returns either; discovery found the font list (FON1 CourierN to
FON6 LinearBeam) among the web page's small files. architecture §6.4 lists these two GETs
(`WEB_FILES`) as the only web requests besides the login, Load and repeat, and architecture §12.1
records them as a plan deviation for the owner's sign-off. They are made only under a held lock (a
Send's or test's machine check) or by the assigned poller right after its lock check (the daily
check), never by anything else. The fake's "web file requested <path>" violation exempts exactly
`WEB_FILES` (§15.5 items 9 and 10).

0. `fonts(*, deadline=None)` and `firmware(*, deadline=None)` follow `load_slot`'s deadline rule
   (§10.6 step 0) for each exchange and sleep; past it they raise as for a 404 (`fonts`: `BadReply`;
   `firmware`: `None`, logged as "firmware unknown").
1. `fonts()`: take the gate, log in if needed, `GET FONT_FILE` (`max_body=MAX_FILE_REPLY`, gunzip on).
   `NoReply`: once more after `read_retry_s`. 401 or HTML: log in again once. A 404, or a body without
   font tags, raises `BadReply(tag="FONTS", reply=...)` "{printer} didn't send its font list." (not
   counted as nonsense). A 200 returns `parse_fonts(body)`.
2. `parse_fonts`: `decode_text`, then every `<FON([1-9])>([^<]*)</FON\1>`, values stripped. It returns
   FON1, FON2, … up to the first missing index (an empty value also ends the list). No FON1 raises
   `BadReply`. The caller compares the list with `Company.standard_fonts`: a shorter, longer or different
   list fails the machine check (red), as does any error here.
3. `firmware()`: as `fonts()` with `IDENTITY_FILE`, except that a 404 returns `None`. `parse_firmware`
   returns the stripped text, at most 40 characters, of the first leaf whose tag appears in
   `FIRMWARE_TAGS` (in that order), else `None`. The daily check logs a change; `None` is logged as
   "firmware unknown" and never blocks a Send.
4. OPEN: which file holds the fonts and which tag the firmware (sfl.xml and idn.xml are small files the
   printer serves), whether they need the cookies, and that fetching them doesn't disturb printing. If
   another source turns out to be right, only `FONT_FILE`, `IDENTITY_FILE`, the two parse functions and
   the fake's files change.

## 11. SME reply parser

### 11.1 parse_sme_reply(body) -> SmeReply

1. It works on bytes. A UTF-8 byte order mark and whitespace before `<SM>` are skipped, and whitespace
   after `</SM>` is ignored. Anything else raises `WebReadFailed` "{printer} sent back a message
   Stripes doesn't recognise." The parser doesn't know the printer, so it uses `""`, and `load_slot`
   raises again with its label.
2. Tokens: tags matching `<(/?)([A-Za-z][A-Za-z0-9_]*)\s*(/?)>` and the bytes between them. A `<` that
   doesn't start such a tag raises `WebReadFailed`, except one leading `<?xml …?>` declaration, which is
   skipped.
3. At most `MAX_SME_DEPTH` (16) elements are open at once, SM included; one more raises
   `WebReadFailed`. A real Load reply goes 4 deep (SM, FCDOC, an item, a leaf). Before this limit a
   reply nested 1,000 levels deep (36 KB) raised `RecursionError` instead.
4. An element whose next token (skipping whitespace-only text) is an opening tag is a container.
   Otherwise it is a leaf, and its value is **every byte between its opening and closing tags,
   unchanged**: spaces are kept, so `<PREF>Packed: </PREF>` is `"Packed: "`. `<X/>` is a leaf with the
   value `""`. A closing tag that doesn't match raises `WebReadFailed`, and so does text other than
   whitespace between the tags of a container.
5. Whitespace between tags is ignored: CRLF (seen), LF, and blank lines (seen inside CTDAT and CLOT).
6. Each leaf value is decoded with `decode_text`: strict UTF-8, else latin-1. So AMPM's single byte
   0xFF (seen) becomes `"\xff"`, and a UTF-8 `ñ` stays `ñ`. **No XML entity is decoded** (§12.2 says
   why).
7. SM-level leaves: TYPE (required, else `WebReadFailed`), TN and XID (`""` when missing), MSNO (`None`
   when missing or empty, otherwise it must be an integer). Other SM-level leaves are ignored. The
   containers are FCDOC and PRV, each at most once.
8. The result is `SmeReply(type, tn, xid, msno, design)`. The design is built (§11.3) only when TYPE is
   `Load`; otherwise it is `None`, and PRV isn't checked.

### 11.2 The two FCDOC forms

| | Verbose (made through Autopilot; the slot 9 capture) | Compact (made by another tool; the slot 1 capture) |
|---|---|---|
| Layout | one tag per line, CRLF, a blank line inside CTDAT and CLOT | the whole FCDOC on one line, then CRLF before `</FCDOC>` |
| Top level | VER 1, W 256, H 160, MSGNAM, the items, CPPARM, H 160 again | MSGNAM, the items, CPPARM, H 160 |
| Item fields | all of them, including BOUND, OBJTYP and two LANGs in CTDAT | only some; DAY, MON and YEAR may be empty |

The parser treats both the same way: nothing depends on line breaks.

### 11.3 FCDOC and PRV into MessageDesign

| MessageDesign field | From |
|---|---|
| `slot` | MSNO (a Load reply carries the real slot) |
| `name` | FCDOC's MSGNAM when there is an FCDOC, else PRV's MSGNAM, else `""`. When both are present and differ: `WebReadFailed` |
| `empty` | `True` when the reply has no FCDOC. An empty slot has no FCDOC, BPIX 0 and an empty MSGNAM (seen) |
| `items` | each container directly inside FCDOC except CPPARM, in reply order, as `DesignItem(tag, pairs)` with every leaf `(tag, value)` in order, repeats kept (CTDAT has LANG 17742, then LANG 4). A container nested inside an item adds its leaves as `("<NESTED>.<LEAF>", value)` |
| `settings` | CPPARM's leaves; a repeated child keeps its last value |
| `width`, `height` | W and the last H, as integers; `None` when absent; `WebReadFailed` when present but not an integer |
| `black_pixels` | PRV's BPIX, an integer of at least 0 (required) |
| `preview_bmp` | `decode_preview` of PRV's BMP (required) |
| `read_at`, `tn`, `xid` | 0.0, the reply's TN, the reply's XID as an integer (0 if it isn't one); `load_slot` then sets them (§10.6) |

A Load without PRV, BPIX or BMP raises `WebReadFailed`. Other FCDOC-level leaves are ignored.

Inferred meanings, recorded for `compare` and the fake (none of them changes the parser): OBJID: objects
sent without one get 1, 2, … in order, and a sent OBJID is kept. BOUND: `x1,y1,x2,y2` in printhead dots.
OBJTYP: 0 for text, 1 for date and lot. CTDAT's TXT: the date as rendered when the Load ran. CLOT's TXT:
the lot's value. LANG 17742 is 0x454E, "EN"; the plain LANG 4 is unknown. HORFLG 1 and AMPM's 0xFF are
unknown.

### 11.4 The preview

1. The BMP leaf is a data URI: `data:image/bmp;base64,`, then **a space** (seen), then base64; 27,415
   characters in all.
2. `decode_preview` requires the `PREVIEW_PREFIX` start, removes every ASCII whitespace character from
   the rest, and decodes with `base64.b64decode(..., validate=True)`.
3. The bytes must be this BMP, exactly as seen (the header starts `42 4D 3E 50 00 00 00 00 00 00 3E 00 00
   00 28 00 00 00 00 04 00 00 A0 00 00 00 01 00 01 00`, and its base64 starts `Qk0+UAAA`):

   | Offset | Field | Required |
   |---|---|---|
   | 0 | signature | `BM` |
   | 2 | file size, uint32 little-endian | 20,542, the length of the bytes |
   | 10 | offset of the pixel data | 62 |
   | 14 | header size | 40 |
   | 18 | width, int32 | 1024 |
   | 22 | height, int32 | 160 or −160 |
   | 26 | planes | 1 |
   | 28 | bits per pixel | 1 |
   | 30 | compression | 0 |
   | 54 | palette | index 0 black `00 00 00 00`, index 1 white `FF FF FF 00` (seen; not enforced) |

   Anything else raises `WebReadFailed`. Rows are stored bottom-up, 128 bytes each, and a 1 bit is
   white.
4. BPIX is taken as the reply gives it (inferred: the number of black pixels). It isn't checked against
   the picture.

### 11.5 Expected parse of the fixtures

The files are listed in §17.

| Fixture | type, tn, xid, msno | design |
|---|---|---|
| `sme_load_slot9.bin` | Load, "326548", "4", 9 | slot 9, name "STRIPES TEST", empty False, width 256, height 160, 3 items (CTEXT, CTDAT, CLOT), 24 settings, black_pixels 2564, a 20,542-byte preview |
| `sme_load_slot0_compact.bin` | Load, "326548", "6", 1 | slot 1, name "", empty False, width None, height 160, 1 item (CTDAT), 16 settings, black_pixels 3492 |
| `sme_save_stale.bin` | Save, "", "", 9 | None |
| `sme_load_slot9_empty.bin` | Load, "326548", "2", 9 | slot 9, name "", empty True, no items, no settings, black_pixels 0, an all-white preview |
| `sme_thumbnail_slot9.bin` | Thumbnail, "326548", "3", 9 | None |
| `sme_load_other_client.bin` | Load, "834854", "2", 1 | the same as the compact fixture |

`sme_load_slot9.bin`, item by item:

- CTEXT, 20 pairs: `LANG 4`, `TXT "STRIPES TEST 123"`, `FONTIND 1`, `SIZE 2`, `POS "10,10"`,
  `BOUND "10,10,218,33"`, `POSF ""`, `PREF ""`, `ROTAT 0`, `WCOEFF 0`, `GROUP 0`, `OBJTYP 0`, `COLR 0`,
  `XRID 0`, `YRID 0`, `SXAS 0`, `SYAS 0`, `OBJID 10001`, `RP 0`, `MARG "0,0"`.
- CTDAT, 31 pairs: `FRMT "Y/M/D"`, `AMPM "\xff"`, `DAY 0`, `DWEEK 0`, `HOUR 0`, `HORFLG 1`, `LANG 17742`,
  `MIN 0`, `MON 0`, `SEC 0`, `YEAR 0`, then the 20 common pairs as for CTEXT but with `TXT "2026/10/29"`,
  `POS "10,60"`, `BOUND "10,60,192,83"`, `PREF "PKD:"`, `OBJTYP 1` and `OBJID 1`. `value("LANG") == "4"`.
- CLOT, 21 pairs: `INDX 1`, then the common pairs with `TXT ""`, `FONTIND 2`, `SIZE 3`, `POS "10,100"`,
  `BOUND "10,100,66,136"`, `PREF "LOT:"`, `OBJTYP 1` and `OBJID 2`.
- The settings equal `FAKE_DEFAULT_SETTINGS` (§12.3).

`sme_load_slot0_compact.bin`: the CTDAT pairs are, in order, `OBJID 1`, `POS "0,0"`, `PREF "Packed: "`,
`FRMT "M/D/y"`, `DAY ""`, `MON ""`, `YEAR ""`, `POSF ""`, `FONTIND 1`, `SIZE 4`, `MARG "0,0"`, `RP 0`,
`LANG 0`. The settings are `ROTAT 0`, `MIRR 0`, `REVRS 0`, `AUTREP 0`, `DELAY 0`, `DLYMM 0`,
`PRNSPD 2500`, `BOLD 1`, `WDTH 1`, `QUANT 1`, `QDELAY 0`, `QDLYMM 0`, `RESMOD 0`, `RESNAM UltraFine`,
`ENCST 0`, `ENCSCL 100`.

## 12. Read-back rules: what is compared with what was sent

`hx_message.compare(sent, readback)` (send.md) implements these rules, and `hx_fake` reproduces them. A
test in test_hx_send builds every kind of message on the fake and asserts that `compare` returns `[]`.
They apply to messages Stripes built (the verbose form).

### 12.1 The message

| Read back | Must be |
|---|---|
| MSNO (`design.slot`) | the slot written (9 or 10); for a running-message read, the slot expected |
| `name` (the parser has already made FCDOC and PRV agree) | the CRTMSG name, exactly |
| `empty` | False |
| `width`, `height` | 256 and 160 (`CANVAS`), when present |
| the items | the same number and the same tags, in the order sent |
| `black_pixels` | more than 0 when any item has a non-empty TXT or PREF: a blank picture of a message with text fails |
| `preview_bmp` | present (the parser guarantees the format). It is judged by the sender, not by code |

### 12.2 Items

| Field | Rule |
|---|---|
| TXT (CTEXT) | exactly the text sent, compared with the raw read-back: no trimming, no entity decoding |
| PREF, POSF | as sent; `""` when not sent |
| SIZE | the size sent, minus 1 |
| FONTIND | as sent |
| POS, MARG | as sent when sent; not compared when not sent (a relative item's POS) |
| XRID, YRID, SXAS, SYAS | as sent; `"0"` when not sent |
| OBJID | as sent when sent; not compared when not sent |
| FRMT, DAY, MON, YEAR (CTDAT) | as sent; DAY, MON and YEAR are `"0"` when not sent |
| ROTAT, WCOEFF, GROUP, COLR, RP (item level) | `"0"`: Stripes never sends them, and another value could change the print |
| HOUR, MIN, SEC, DWEEK (CTDAT) | `"0"` |
| TXT (CTDAT) | not compared: it is the date of the read. send.md may check it against the expected rendering |
| BOUND | not compared with what was sent (it is worked out by the printer), but checked against the print area: send.md §8.1 rule 6 fails an item whose BOUND `x1,y1,x2,y2` runs outside 0..1024 by 0..H (OPEN: BOUND units and whether the 256-wide canvas clips) |
| LANG, AMPM, HORFLG, OBJTYP | not compared (bookkeeping) |
| any other field | a difference: an unknown field means the format has changed, and an unrecognised read fails (plan) |

Why there is no entity decoding: if the printer stores `R&amp;D` as it was sent, it prints `R&amp;D`,
and the raw read-back `R&amp;D` differs from `R&D`, so the check fails, correctly. If the printer
decodes the entity and answers a raw `R&D`, they match. If it decodes it and escapes it again in the
reply, the check fails falsely, which is safe. Only decoding in the parser could let a wrong print pass.
OPEN: try `& < > ñ` on the test unit.

### 12.3 Print settings

`FAKE_DEFAULT_SETTINGS` is the CPPARM a message gets for everything not sent (seen on the slot 9 message,
which was built without any CPPARM), in the printer's order:

```
ROTAT 0, BOLD 1, WDTH 1, QUANT 1, DELAY 0, QDELAY 0, AUTREP 0, REVRS 0, DRAFT 0, MIRR 0, SLANT 0,
ANG 30, SWDLY 0, SWSPD 250, WPDLY 0, PRNSPD 1000, ENCST 0, RESMOD 0, DLYMM 0, QDLYMM 0, ENCO 0,
RESO 640, RESNAM UltraFine, ENCSCL 100
```

| Setting | Rule |
|---|---|
| each child sent in CPPARM | read back exactly as sent: DELAY in µs, DLYMM in mm, PRNSPD as sent (OPEN items in §5.3) |
| settable but not sent: QDELAY, QDLYMM, ENCSCL, and whichever of DLYMM and DELAY wasn't sent | the default above |
| not settable through Autopilot: DRAFT, SLANT, ANG, SWDLY, SWSPD, WPDLY, ENCO | the default above ("must match the printer's default, or the check fails", plan) |
| RESNAM | the name for RESMOD: 0 `UltraFine`, 1 `Fine`, 2 `Normal`, 3 `Draft` (OPEN: the spelling of 1 to 3) |
| RESO | not compared (OPEN: it varies with the resolution; 640 at UltraFine was seen, the manual shows 600) |
| a child missing from the read-back | a difference |
| a child not in the list above | a difference |

### 12.4 Drift and the running message

The poller's drift check compares `fingerprint(design)` (send.md), which leaves out CTDAT's TXT
(architecture decision 22). This file only guarantees that identical replies give identical
`MessageDesign`s, whatever their line endings.

## 13. Nonsense counting

`PrinterClient.nonsense` counts replies thrown away because they don't make sense. The poller turns its
growth into "Something else is talking to printer X" (states.md). It goes up by 1 for:

1. each Autopilot read reply that fails `check_reply`, including `-1`;
2. `read_twice` or `check_uid` getting two different answers;
3. a write answered with something that isn't an integer code;
4. an HTTP status other than 200 or 401;
5. a web reply classified `foreign` or `odd`;
6. a matched Load that fails the full parse or the slot check;
7. a login reply with broken HTTP framing (it raises `AuthFailed`, §10.1 rule 3).

It doesn't count `NoReply` (states.md counts that towards "unreachable"), `6` and `7` (counted in
`busy_replies`, apart, so states.md can weigh them: with one request at a time, busy also hints at
another client), expected stale replies, or logging in again (`relogins`).

## 14. Messages

`hx_protocol` defines `ES` and calls `i18n.add_spanish(ES)` (architecture §10.6). Values: `printer` is
`PrinterClient.label` (a record name, never translated), `command` and `query` are tags, `reply` is at
most 40 characters of the printer's answer, and `slot`, `got`, `expected` and `code` are numbers. None
of them is bag text.

| English (N_) | Spanish | Raised as |
|---|---|---|
| No reply from {printer}. | Sin respuesta de {printer}. | NoReply |
| No reply from {printer} to {command}. It may or may not have been done. | Sin respuesta de {printer} a {command}. Puede que se haya hecho o no. | NoReply, after a write |
| {printer} is busy. | {printer} está ocupada. | PrinterBusy |
| {printer} is busy with another request from this PC. | {printer} está ocupada con otra solicitud de este PC. | PrinterBusy, gate timeout |
| {printer} refused the password, or another printer is at its address. | {printer} rechazó la contraseña, o hay otra impresora en su dirección. | AuthFailed |
| No password is saved for {printer}. | No hay ninguna contraseña guardada para {printer}. | AuthFailed |
| A saved printer password was made by a different Stripes build. Enter it again. | Una contraseña de impresora guardada se creó con otra compilación de Stripes. Escríbala de nuevo. | AuthFailed, from unscramble |
| A saved printer password is damaged. Enter it again. | Una contraseña de impresora guardada está dañada. Escríbala de nuevo. | AuthFailed, from unscramble |
| {printer}: {problem} | {printer}: {problem} | AuthFailed, ClientPool wrapping one of the two above |
| {printer} gave an answer to {query} that doesn't make sense: {reply} | {printer} dio una respuesta a {query} que no tiene sentido: {reply} | BadReply |
| {printer} gave two different answers to {query}. | {printer} dio dos respuestas distintas a {query}. | BadReply |
| {printer} refused {command} (code {code}). | {printer} rechazó {command} (código {code}). | CommandRefused |
| {printer} is not the expected printer: UID {got} answered instead of {expected}. | {printer} no es la impresora esperada: respondió el UID {got} en lugar del {expected}. | WrongPrinter |
| {printer} didn't send back the message in slot {slot} in time. | {printer} no devolvió a tiempo el mensaje de la ranura {slot}. | WebReadFailed |
| {printer} sent back a message Stripes doesn't recognise. | {printer} devolvió un mensaje que Stripes no reconoce. | WebReadFailed |
| {printer} sent back slot {got} instead of slot {slot}. | {printer} devolvió la ranura {got} en lugar de la ranura {slot}. | WebReadFailed |
| {printer} didn't send its font list. | {printer} no envió su lista de fuentes. | BadReply, fonts |
| Stripes tried to send a command it never sends: {command}. | Stripes intentó enviar un comando que nunca envía: {command}. | ForbiddenCommand |

## 15. Fake printer (hx_fake.FakePrinter)

### 15.1 Server

1. `start()` creates `http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)` with `daemon_threads =
   True`, runs `serve_forever` on a daemon thread named `hx-fake-<port>`, sets `port` to the bound port and
   returns `self`.
2. The handler overrides `handle()` and reads the raw request itself (the request line, the headers up to
   the blank line, then `Content-Length` bytes), so `requests[i]["raw"]` holds exactly the bytes that
   arrived. It writes raw response bytes (no `send_response`, so no Date header) and always closes the
   connection after one response.
3. `stop()` releases any silent or stalled handler, then calls `shutdown()` and `server_close()`. New
   connections are then refused, like a printer that is switched off.
4. Time: the injected `clock` gives the printer's date and time, the quiet time for the idle drop,
   `web_result_delay_s` and the `requests` time stamps. Real time (`time.sleep`, `time.monotonic`) gives
   `latency_s`, `web_latency_s`, `multi_value_hang_s`, the editor stall, the overload pause and the
   silent waits, because those must really overlap.
5. `self.lock` guards all state. A handler never sleeps while holding it.

### 15.2 Handling one request, in order

0. A connection that the client closes before sending any byte (a bare connect: the reachability
   probe, protocol §9 item 7) ends here: it isn't appended to `requests`, isn't counted in flight or as
   activity for the idle drop, and adds no violation.
1. Read the request (§15.1) and append `{"t": clock.monotonic(), "kind", "tag", "body", "raw", "reply":
   None, "status": None}` to `requests`. `kind` is `autopilot`, `login`, `sme`, `sme_repeat`, `get` or
   `bad`; `tag` is the command tag, the SME TYPE or the path.
2. Editor stall: while another handler is sending the editor page, wait (real time) until it has
   finished.
3. Silent (`go_silent(True)`, or an override returning `SILENT`): not counted in flight. Wait until the
   client closes the connection, `go_silent(False)` or `stop()`, then close without a reply.
4. An overload pause is running: close without a reply (not counted).
5. `in_flight += 1` and `max_in_flight = max(max_in_flight, in_flight)`. If `in_flight >= 2`, append
   "two requests in flight" to `violations` and mark this request (the newcomer) as overlapping; the
   requests already in flight are answered normally. If `in_flight >= overload_at`, start an overload
   pause of `overload_pause_s` and drop this request; every request still in flight is dropped too when
   it reaches step 11 (the real printer stopped answering all of them).
6. Idle drop: if `drop_after_idle_s` is set, `clock.monotonic() - last_activity >= drop_after_idle_s`
   and `rng.random() < drop_after_idle_p`, drop the request. A dropped request still counts as activity.
   `last_activity` starts at the construction time.
7. `last_activity = clock.monotonic()`.
8. Sleep `latency_s`, plus `web_latency_s` for a login, SME or file request. Then, if `in_flight_hook` is
   set, call it with the request's record (still counted in flight, before its reply is made): a test
   can hold one request there until a second arrives, to make an overlap without relying on timing.
9. Compute the reply under the lock (§15.3 to §15.5). For an Autopilot request marked overlapping: with
   `mixup` and `rng.random() < mixup_p`, answer with the reply most recently computed for a different
   request, when there is one; otherwise, with `busy_on_overlap`, answer `7`; otherwise answer
   normally.
10. `in_flight -= 1` **before** writing, so a client holding its whole reply can never cause a false
    overlap with its next request.
11. If an overload pause started while this request was in flight, drop it. Otherwise write the reply,
    record `reply` and `status`, and close.

To drop a request: `in_flight -= 1` if it was counted, then close the socket without writing anything.
The client sees "connection closed without reply". `rng` is `random.Random(seed)`.

### 15.3 Autopilot model

1. Authentication: the body must fullmatch
   `\s*<Cookie><Auth_token_(\d+)>([^<]*)</Auth_token_\1></Cookie><APCMD>(.*)</APCMD>\s*` (DOTALL) with
   the UID equal to `uid` and the token equal to `make_token(password)`. Otherwise answer 401 with the
   gzipped login page.
2. The first element inside APCMD is the command: `<TAG/>` is a query and `<TAG>…</TAG>` a write.
   Anything unparseable answers `2`. Text other than whitespace after the first element adds the
   violation "two commands in one request" and is ignored.
3. `override(tag, reply)` is checked first. A string is the answer; for a write, the write is carried out
   only when the string's first token is `0`. A callable is called with no arguments each time and may
   return a string, `None` (behave normally), `DROP` (carry out a write, then close without a reply) or
   `SILENT`. `override(tag, None)` removes it.
4. Queries. CPPARM, ODPARAM and RS232 add the violation "multi-value query <TAG>", wait
   `multi_value_hang_s` and answer `6`. A tag outside `QUERY_SPECS` adds the violation "query not in
   catalogue <TAG>" and then answers `-1` for BEAC, the lot's value for LOT1 to LOT9 and `-1` for
   anything else. Catalogue queries:

   | Query | The fake answers |
   |---|---|
   | HXUI | `uid` |
   | HXNA | `hx_name` (default `" Printer 1"`, with the leading space) |
   | PRIN | `1` or `0` from `printing` |
   | MSGNAM | the current slot's name; `None` when it is unnamed or the slot is empty |
   | MSCNT, PMCNT | `counters` |
   | DATE, TIME | the printer clock `printer_clock()` (§1.2: the clock's time plus `base_offset_s` plus `clock_offset_s`, naive, never following daylight saving or the host's time zone), as `dd/mm/yyyy` and `hh:mm:ss`, zero-padded |
   | INKT | `ink_name`; `None` when it is `""` |
   | INKL, PHTP | `ink_level`, `head_temp_c` |
   | ECOMOD | `eco` |
   | ENCST | the current message's ENCST, else `encoder["ENCST"]` |
   | ENWHD, ENPPR, ENTPM | `encoder` |
   | PLDEL, MNPL, PRPL, PLVO, PLWR, NZRO | `cartridge` (default 1210, 450, 220, 9500, 27, 2: the manual's examples) |
   | DELAY | the current message's DELAY // 1000: ms, like the real printer |
   | PRNSPD, RESMOD, BOLD, WDTH, QUANT, ROTAT, MIRR, REVRS, AUTREP | the current message's settings, else `FAKE_DEFAULT_SETTINGS` |

5. Writes are appended to `writes`. A tag in `PRINTER_WIDE_WRITES` adds the violation "printer-wide
   command <TAG>", answers `0` and changes nothing. Any other tag outside `ALLOWED_WRITE_TAGS` adds the
   violation "write not whitelisted <TAG>"; CLOT and CCONT are still taken into the design (so the
   capture's real CLOT sequence can be replayed, §18), and other tags answer `1`. Then:

   | Write | The fake | Errors |
   |---|---|---|
   | CRTMSG name | a new design in progress: the name, no items, the default settings, next automatic OBJID 1 | a name over 20 characters: `3` |
   | CTEXT, CTDAT, CLOT | adds an item (§15.4) | no design in progress: `5`; a child the manual doesn't list for that object, FONTIND outside 1..len(fonts), SIZE outside 1..10, other ranges as the manual: `3` |
   | CPPARM | merges the children into the design's settings; RESNAM follows RESMOD | the §6.1 ranges, with PRNSPD up to 2500: `3` |
   | SAVEMSG n | stores the design as `slots[n]` with `slot_style[n] = "verbose"`, ends the design, and sets the buffer to a TYPE Save result (§15.4). n not 9 or 10 adds the violation "write outside slots 9 and 10"; n equal to the current slot while printing adds "SAVEMSG to the printing slot". Both are still carried out | n outside 1..10: `3`; no design: `5` |
   | PRINTMSG n | `current_slot = n`; MSCNT goes back to 0 when the slot changes (OPEN). n not 9 or 10 adds the violation | n outside 1..10: `3`; an empty slot: `4` (OPEN) |
   | PRINT v | `printing = (v == 1)` | v not 0 or 1: `3` |
   | TD | sets `clock_offset_s` so that `printer_clock()` shows the fields given with seconds 00 (missing fields keep the printer's current values; HOUR 24 means 0) and keeps DTRH and DTRM in `rollover` | DAY 1..31, MNTH 1..12, YEAR 2000..2050, HOUR 0..24, MIN 0..59, DTRH 0..24, DTRM 0..59 and a real date, else `3` |

   A write that was carried out answers `0`, or `0 <value>` for PRINT, PRINTMSG and SAVEMSG when
   `write_echo` is set.

### 15.4 Messages and rendering

1. `slots` holds `MessageDesign`s. An item is stored as its specific fields followed by the common
   fields:

   ```
   common (CTEXT):  LANG 4, TXT, FONTIND, SIZE, POS, BOUND, POSF, PREF, ROTAT 0, WCOEFF 0, GROUP 0,
                    OBJTYP 0, COLR 0, XRID, YRID, SXAS, SYAS, OBJID, RP 0, MARG
   CTDAT:           FRMT, AMPM "\xff", DAY, DWEEK 0, HOUR 0, HORFLG 1, LANG 17742, MIN 0, MON, SEC 0,
                    YEAR, then the common fields with OBJTYP 1
   CLOT:            INDX, then the common fields with OBJTYP 1
   ```

   Values sent are kept as text, except SIZE, which is stored as the size sent minus 1. Defaults for
   fields not sent: FONTIND 1, SIZE 0 (as if 1 was sent), POS "0,0", MARG "0,0", PREF, POSF and TXT "",
   XRID, YRID, SXAS and SYAS 0, DAY, MON and YEAR 0, INDX 1, and OBJID the next automatic number (1, 2,
   …, counting only objects sent without one).
2. CTDAT's TXT is rendered at each Load from FRMT on the printer clock (`printer_clock()`) plus the
   offsets (years, then months with the day kept within the month's length, then days). `Y` is the
   4-digit year, `y` the 2-digit year, `M` the 2-digit month, `m` JAN to DEC, `D` the 2-digit day,
   `H` the 2-digit hour, `U` the minute and `E` the second; any other character is copied. CLOT's
   TXT is `lots[INDX]`.
3. BOUND is computed at each render. `x1,y1` is POS, `x2 = x1 + chars × round(CW[font] × size)` and
   `y2 = y1 + round(CH[font] × size)`, where `size` is the size sent (stored + 1), `chars = len(PREF +
   TXT + POSF)`, `CW = {1: 13/3, 2: 3.5, 3: 3.0, 4: 3.5, 5: 4.0, 6: 4.0}` and `CH = {1: 23/3, 2: 9.0,
   3: 9.0, 4: 9.0, 5: 9.0, 6: 9.0}`. This crude metric was chosen so the slot 9 capture's BOUNDs come out
   exactly: 10,10,218,33; 10,60,192,83; 10,100,66,136.
4. The preview is `bmp_1bit([each item's BOUND])`, and BPIX is its count of black pixels. A compact item
   without BOUND gets the same rectangle from POS and the metric.
5. `bmp_1bit(rects, width, height)` writes the §11.4 header (file size `62 + width / 8 × height`, height
   positive), the palette (black, then white) and white pixels, then turns each rectangle's pixels
   black (bit cleared). Pixel (x, y) is bit `7 - x % 8` of byte `x // 8` in stored row
   `height - 1 - y`. Rectangles are clipped to the picture, and overlaps count once.
6. Verbose rendering. Every line ends in CRLF; nothing is indented; values are UTF-8 except AMPM, which
   is written as the single byte 0xFF.

   ```
   <SM>
   <TYPE>Load</TYPE>
   <TN>{tn}</TN>
   <XID>{xid}</XID>
   <MSNO>{slot}</MSNO>
   <FCDOC>
   <VER>1</VER>
   <W>256</W>
   <H>160</H>
   <MSGNAM>{name}</MSGNAM>
   for each item: <{ITEM}>, one line per specific field, an empty line (CTDAT and CLOT only),
                  one line per common field, </{ITEM}>
   <CPPARM>
   one line per setting, in stored order
   </CPPARM>
   <H>160</H>
   </FCDOC><PRV>
   <BPIX>{n}</BPIX>
   <MSGNAM>{name}</MSGNAM>
   <BMP>data:image/bmp;base64, {base64}</BMP>
   </PRV>
   </SM>
   ```

7. Compact rendering (slot 1 at the start, or any slot whose `slot_style` is `"compact"`): the same
   first five lines, then one line `<FCDOC><MSGNAM>{name}</MSGNAM>` followed by each item as `<TAG>`,
   its stored pairs without line breaks and `</TAG>`, then `<CPPARM>…</CPPARM><H>160</H>`; then the
   lines from `</FCDOC><PRV>` on, as above.
8. An empty slot: the first five lines, then `<PRV>`, `<BPIX>0</BPIX>`, `<MSGNAM></MSGNAM>`, the all-white
   BMP line, `</PRV>` and `</SM>`.
9. A Thumbnail result has TYPE Thumbnail and only the PRV block after the first five lines. A Save result
   (after SAVEMSG n) has TYPE Save, `<TN></TN>`, `<XID></XID>`, MSNO n and then slot n's PRV block.
10. At construction: `slots[1]` is the compact message of the slot 1 capture, unnamed (one CTDAT with
    `PREF "Packed: "`, `FRMT "M/D/y"`, FONTIND 1 and SIZE 4 as stored, and that capture's settings with
    PRNSPD 2500 and ENCST `1` if `placement == "encoder"` else `0`); `current_slot = 1`; `printing =
    True`; slots 2 to 10 empty. So the idle slot is 9 at the start, as on the real printer. Counters:
    PMCNT 100000, MSCNT 0. `ink_name "Example Black"`, `ink_level 1`, `head_temp_c 25`, `eco 0`,
    `clock_offset_s 0.0`, `base_offset_s = offset_fn(clock.time())` (so the fake's clock starts at the
    local time of the same offset function a test gives its `TimeSource`, whatever the host's time
    zone), `encoder = {"ENCST": 1 if placement == "encoder" else 0, "ENWHD": 56,
    "ENPPR": 5000, "ENTPM": 1}`.

### 15.5 Web model

1. Login (POST / with `Type=LOG`): form-decode with `urllib.parse.parse_qs`. A `pass` equal to
   `md5(password).hexdigest()` makes a new session `(f"{rng.getrandbits(128):032x}",
   f"{rng.getrandbits(128):032x}")`, which replaces all others when `single_web_session` is set and is
   added to `sessions` otherwise. The answer is 200 with `Server: MapJet/v1.1`, `Connection: close`,
   `Content-Type: text/html`, `Content-Encoding: gzip`, `Set-Cookie: Access_token_<uid>=<a>`,
   `Set-Cookie: Auth_token_<uid>=<b>` and `Content-Length`, and a gzipped small main page. A wrong
   password gets 200 with the gzipped login page and no cookies (OPEN).
2. SME and file requests need a session: the Cookie header must name a pair in `sessions`
   (`Access_token_<uid>` and `Auth_token_<uid>`). Otherwise they get 401 with the gzipped login page.
3. The buffer starts as a Save result for slot 1 with empty TN and XID, so a first Load gets an expected
   stale reply.
4. Load or Thumbnail (POST `?sme`): parse
   `<SM><TYPE>(\w+)</TYPE><TN>(\d*)</TN><XID>(\d*)</XID><MSNO>(\d+)</MSNO></SM>`; anything else adds the
   violation "bad SME request" and answers the buffer. Promote a pending result if its time has come.
   The answer is the buffer as it is now. Then build the result (§15.4; slot 0 means `current_slot`) and
   pass it through `web_reply_hook` if that is set. Then:
   - with `web_first_reply_fresh`, the buffer becomes the result and the answer is the result itself;
   - otherwise, with `web_result_delay_s == 0`, the buffer becomes the result right after the answer is
     formed;
   - otherwise the result waits as pending until `clock.monotonic()` is `web_result_delay_s` later
     (`float("inf")` means never).
5. Repeat (POST `?sme_repeat`, body `<SM>RepeatRequest</SM>`): promote a pending result if it is due,
   and answer the buffer.
6. Any other TYPE answers the buffer and changes nothing.
7. SME answers are 200 with `Server: MapJet/v1.1`, `Connection: close`, `Content-Length` and
   `Content-Type: application/xml`, not gzipped.
8. `leave_in_buffer(body)` sets the buffer (another client's leftover). `expire_web_session()` empties
   `sessions`.
9. Files: `GET /sfl.xml` and `GET /idn.xml` need a session. The body is `files[path]` when the key is
   present (`None` answers 404); otherwise sfl.xml is `<SFL>`, then `<FON{i}>{name}</FON{i}>` for each
   font, then `</SFL>`, and idn.xml is `<IDN>`, `<HXUI>{uid}</HXUI>`, `<FW>{firmware}</FW>`, `</IDN>`,
   with CRLF after each element (an assumed format; OPEN). With `gzip_files` they are sent with
   `Content-Encoding: gzip`. A 404 has `Content-Length: 0`.
10. `GET /simple_msg.html` adds the violation "editor page requested" and answers 200 `text/html` with
    `editor_page_bytes` of filler HTML, written in 64 KB pieces spread evenly over `editor_stall_s`
    (real time), while every other request waits (§15.2 step 2). `GET /` with a session answers a small
    main page, without one 401. Any other path or method adds the violation "web file requested <path>"
    and answers 404.

### 15.6 Quirks and knobs

| Real behaviour (source) | The fake | Knob |
|---|---|---|
| 401 and a gzipped login page without a valid UID and token (seen) | §15.3 | `password`, `set_password` |
| One command per request; the rest ignored (seen) | §15.3 step 2 | none |
| Codes in 200 bodies, looking like values (seen, manual) | §15.3; any answer through `override` | `override` |
| Busy from 2 overlapping requests (stress test) | `7` to overlapping Autopilot requests | `busy_on_overlap` |
| Mixed-up replies with 2 or more in flight: 39 in the stress test, none in about 12,700 sent one at a time | another request's reply | `mixup`, `mixup_p`; tests force the overlap with `in_flight_hook` |
| Stopped answering at 8 and at 32 overlapping requests, back within about 1 s (stress test) | drops everything for `overload_pause_s` | `overload_at`, `overload_pause_s` |
| About 2 at once and about 16 a second in total; 80 ms per reply (stress test) | a thread per connection plus latency | `latency_s` |
| The first request after about 4.7 s of quiet sometimes closed without a reply (soak: 5 in 9,536) | drops it | `drop_after_idle_s`, `drop_after_idle_p` |
| Multi-value queries hang about 4 s and answer 6 (seen) | the same | `multi_value_hang_s` |
| The 2.3 MB editor page stalls the printer about 5 s (seen) | the same; stalls every other request | `editor_page_bytes`, `editor_stall_s` |
| One shared web reply buffer; the first reply is the previous result (seen) | the same | `web_result_delay_s`, `web_first_reply_fresh` |
| SAVEMSG leaves TYPE Save with empty TN and XID (seen) | the same | none |
| Another web client's leftover (seen: TN 834854) | none | `leave_in_buffer` |
| A web session ends: 401 or the login page (assumed) | the same | `expire_web_session`, `single_web_session` |
| An SME request takes about 0.8 s (seen) | none | `web_latency_s` |
| SIZE reads back one lower (seen) | stored as sent minus 1 | none |
| Defaults for anything not sent (seen) | `FAKE_DEFAULT_SETTINGS` | none |
| The DELAY query answers in ms (seen) | µs // 1000 | none |
| HXNA's leading space (seen) | `" Printer 1"` | `hx_name` |
| An unnamed message gives MSGNAM None (seen) | the same | none |
| CRLF, blank lines, the 0xFF in AMPM, the space in the data URI (seen) | the same | none |
| Verbose and compact FCDOC (seen) | `slot_style` | `slot_style` |
| The clock runs fast and drifts (seen: 2 h 11 min fast, gaining about 10 s a day) | a fixed offset | `clock_offset_s` |
| BEAC refused with −1 (seen) | the same, plus a violation | none |
| Switched off; a Wi-Fi drop | `stop()` (refused); `go_silent()` (timeouts) | none |
| Counters rise with prints | `print_bags(n)` | `counters` |
| Encoder and delay modes | `printed` records ENCST and DLYMM (mm) or DELAY (µs) for each bag | `placement`, `encoder` |

### 15.7 Controls and observation

- `print_bags(n)`: only while `printing` is on and the current slot has items. For each bag, PMCNT and
  MSCNT go up by 1 and `printed` gets `{"slot", "name", "encoder": ENCST == "1", "position_mm":
  int(DLYMM) when encoder else None, "delay_us": int(DELAY) when not encoder else None, "at":
  clock.time()}`.
- `go_silent(silent=True)`, `override(tag, reply)`, `expire_web_session()`, `leave_in_buffer(body)`,
  `set_password(password)`, `reset_observations()`.
- `printer_record(**fields)` returns `Printer(id=new_id("pr"), name="Printer 1", host="127.0.0.1",
  port=self.port, uid=self.uid, token=scramble(make_token(self.password), release_key()))` with
  `fields` overriding; only after `start()`.
- `requests`, `writes`, `max_in_flight`, `violations`, `printed` and `buffer` are read under `self.lock`,
  or after the client calls have returned.

### 15.8 Violations, exact texts

"two requests in flight" · "editor page requested" · "web file requested <path>" · "printer-wide
command <TAG>" · "write not whitelisted <TAG>" · "write outside slots 9 and 10" · "SAVEMSG to the
printing slot" · "multi-value query <TAG>" · "query not in catalogue <TAG>" · "two commands in one
request" · "bad SME request".

Every test that drives Stripes code against a fake ends with `assertEqual(fake.violations, [])`, unless
it provokes one on purpose.

## 16. FakeSntp, FakeWorld and the script

### 16.1 FakeSntp

1. A UDP socket on 127.0.0.1, port 0; a daemon thread answers each datagram of at least 48 bytes.
2. The answer is 48 bytes: LI 0, VN copied from the request, mode 4; stratum 2; poll copied; precision
   −20; root delay and root dispersion 0; reference ID `b"FAKE"`; reference, receive and transmit
   timestamps all `time.time() + offset_s` in NTP form (seconds since 1900, which is Unix time +
   2,208,988,800, then a 32-bit fraction); originate timestamp = the request's transmit timestamp
   (bytes 40 to 47).
3. With `silent` set, it doesn't answer. `offset_s` and `silent` may change at any time; `requests`
   counts the datagrams.
4. It uses real time only: it stands for another machine.

### 16.2 FakeWorld

1. `FakeWorld(tmpdir, *, printers=(), clock, timings)`: the share root is `tmpdir/share/v2`; `fs =
   FlakyFileOps()`; one `FakePrinter(clock=clock, **d).start()` for each dict in `printers` (a dict may
   also hold `name`, default `"Printer <n>"`, removed before the call); an empty list means one delay
   printer; `sntp = FakeSntp().start()`.
2. The seeded records are written with `hx_share.write_record(store, record)` (storage.md §15: the
   record file at its §2 path, no revision bump, no log) through a store of the identity
   `identity("admin", admin=True, sender=False)`, the company file first (the same file the folder
   script seeds), with revision 1 and `updated_by "EXAMPLE\\admin"`. Then `config/hx/changes.json` is
   written with counter 1 (storage.md §4.2), so the first data cycle loads fully and the next ones see
   no change:

   | Record | Values |
   |---|---|
   | Company | time_server `f"127.0.0.1:{sntp.port}"` (send.md §9.2 `parse_host` reads the port); countries USA (aliases US, U.S.A., United States), Canada (CAN), Mexico (MEX); inks Example Black (plastic, black), Example White (plastic, white), Example Paper Black (paper, black); admin_group_sid `S-1-5-21-1000-2000-3000-1101`; sender_group_sid `S-1-5-21-1000-2000-3000-1102`; everything else default |
   | Line | "Line 1", code L1, plastic, notify_user `EXAMPLE\jsmith` |
   | Printer n | `fake.printer_record(name=...)` |
   | Machine n | "Line 1 Bagger n", code `B<n>`, the fake's placement, print_speed 2400; encoder machines also head_distance_mm 412.0, encoder_wheel_mm 56.0 and encoder_ppr 5000 |
   | Product | "Example Co 2lb Onion", customer "Example Co" |
   | Print text | "Packed on + origin", code PKO, active; items i1 packed_date `MM/DD/YY` with before "Packed on ", i2 origin placed below, i3 lot with before "Lot " placed right |
   | Film | code EXCO1, artwork Rev C, active, back, plastic, black, length 610, position 150, delay 480, font 2, size 4, resolution 0, bold 1, width 1, print texts [the one above], which is also the default |

3. `ids` is `{"company": "company", "line": ..., "product": ..., "film": ..., "print_text": ...,
   "printer1": ..., "machine1": ..., ...}`.
4. `identity(name, *, admin=False, sender=True, pid=1)` returns `Identity(user=f"EXAMPLE\\{name}",
   computer=f"PC-{name.upper()}", sids=<the chosen group SIDs>, pid=pid)`.
5. `store(identity)` returns `ShareStore(share_root, identity=identity, clock=clock, fs=self.fs.view(),
   timings=timings, version="test")`: each simulated PC gets its own view, so its not-found cache and
   `calls` are its own (storage.md §16). `paths(identity)` returns an `HxPaths` with roaming and local
   under `tmpdir/pcs/<identity.key>/` and machine under `tmpdir/pcs/<computer>/machine`.
6. `time_source()` returns `TimeSource([f"127.0.0.1:{sntp.port}"], clock=clock)`, the same host the
   seeded company names, so a `TimeSource` built by `make_services` from the company record reaches the
   fake too.
7. `close()` stops every fake. The test owns `tmpdir`.

### 16.3 The script

`python hx_fake.py [--printers N] [--encoder] [--mixup]` makes a temp folder and a `FakeWorld` with N
printers (encoder or delay; `mixup`, `latency_s=0.08` and `web_latency_s=0.8` like the real printer),
prints `STRIPES_V2`, `STRIPES_V2_ROAMING`, `STRIPES_V2_LOCAL`, `STRIPES_V2_MACHINE`,
`STRIPES_HX_USER=EXAMPLE\jsmith`, `STRIPES_HX_COMPUTER=PC-JSMITH` and `STRIPES_HX_SIDS=<sender SID>,<admin
SID>` as `set NAME=value` lines (`export NAME=value` off Windows), and runs until Ctrl+C. The seeded
company names the fake time server as `127.0.0.1:<port>`, so the app's Sends use it.

## 17. Fixtures (testdata/hx)

Everything here is written by `testdata/hx/make_fixtures.py`, standard library only and self-contained
(it imports no `hx_` module, so a bug in the code can't change a fixture). It is run once from the repo
root and committed with its output. Everything is sanitized: host 192.0.2.10, UID 1109, password 123321,
no real token, cookie, address or name. The real pictures weren't kept, so every BMP is synthetic, with
the real header (§11.4): all white, plus one black rectangle giving the captured BPIX (2564: x 10 to 650,
y 10 to 13; 3492: x 0 to 872, y 0 to 3; 0: none).

### 17.1 SME replies

Raw bytes with CRLF line endings, each ending `</SM>` and CRLF.

| File | Content |
|---|---|
| `sme_load_slot9.bin` | §17.6 A exactly: TN 326548, XID 4, MSNO 9, the verbose FCDOC, the single byte 0xFF in AMPM, the blank lines inside CTDAT and CLOT, BPIX 2564, the BMP data URI with its space. 29,482 bytes long, the captured Content-Length |
| `sme_load_slot0_compact.bin` | §17.6 B: TN 326548, XID 6, MSNO 1, the one-line compact FCDOC, BPIX 3492 |
| `sme_save_stale.bin` | §17.6 C: TYPE Save, empty TN and XID, MSNO 9, PRV with BPIX 2564 and MSGNAM STRIPES TEST |
| `sme_load_slot9_empty.bin` | §17.6 D: TN 326548, XID 2, MSNO 9, PRV only, BPIX 0, empty MSGNAM, an all-white BMP |
| `sme_thumbnail_slot9.bin` | §17.6 E: TYPE Thumbnail, TN 326548, XID 3 (not recorded in the capture; chosen), MSNO 9, PRV with BPIX 2564 and MSGNAM STRIPES TEST |
| `sme_load_other_client.bin` | §17.6 F, another client's stale reply: TYPE Load, TN 834854, XID 2, MSNO 1, then the compact FCDOC and PRV of `sme_load_slot0_compact.bin` |

### 17.2 HTTP replies

Full response bytes.

| File | Content |
|---|---|
| `http_autopilot_ok.bin` | `HTTP/1.1 200 OK`, `Server: MapJet/v1.1`, `Connection: close`, `Content-Length: 1`, `Content-Type: text/plain`, a blank line, `0` |
| `http_autopilot_busy.bin` | the same with the body `7` |
| `http_autopilot_none.bin` | the same with `Content-Length: 4` and the body `None` |
| `http_unauthorized.bin` | `HTTP/1.1 401 Unauthorized`, Server, Connection, `Content-Type: text/html`, `Content-Encoding: gzip`, Content-Length, then `gzip.compress(LOGIN_HTML, mtime=0)` |
| `http_login_ok.bin` | `HTTP/1.1 200 OK`, Server, Connection, `Content-Type: text/html`, `Content-Encoding: gzip`, `Set-Cookie: Access_token_1109=fedcba9876543210fedcba9876543210`, `Set-Cookie: Auth_token_1109=0123456789abcdef0123456789abcdef`, Content-Length, then `gzip.compress(MAIN_HTML, mtime=0)` |

`LOGIN_HTML = b'<!DOCTYPE html><html><body><form method="post"><input type="password"
name="pass"></form><p>V.72.5</p></body></html>'` and `MAIN_HTML = b'<!DOCTYPE
html><html><body><p>Hx</p></body></html>'`. Tests compare the decompressed bodies, never the compressed
bytes, which may differ between zlib builds.

### 17.3 Golden requests

| File | Builder call |
|---|---|
| `req_autopilot_prin.bin` | `autopilot_request(1109, "Yzg4MzdiMjNmZjhhYWE4YTJkZGU5MTU0NzNjZTA5OTE=", "<PRIN/>")` (a 118-byte body) |
| `req_autopilot_print1.bin` | the same with `"<PRINT>1</PRINT>"` (127 bytes) |
| `req_login.bin` | `login_request("192.0.2.10", 80, "c8837b23ff8aaa8a2dde915473ce0991")` |
| `req_load_slot9.bin` | `sme_request("192.0.2.10", 80, COOKIES, repeat=False, body=load_body("326548", 4, 9))` |
| `req_repeat.bin` | `sme_request("192.0.2.10", 80, COOKIES, repeat=True, body=b"<SM>RepeatRequest</SM>")` |
| `req_get_fonts.bin` | `file_request("192.0.2.10", 80, COOKIES, "/sfl.xml")` |

`COOKIES = {"Access_token_1109": "fedcba9876543210fedcba9876543210", "Auth_token_1109":
"0123456789abcdef0123456789abcdef"}`. make_fixtures.py writes these from its own copy of the §2.3
formats, and a test checks that the `hx_protocol` builders produce the same bytes.

### 17.4 Assumed web files

`assumed_sfl.xml` and `assumed_idn.xml` hold the formats the fake serves (§15.5 item 9), with the
standard fonts and firmware 69.75.10. They are called "assumed" because they weren't captured (OPEN).

### 17.5 README.md

Says where each file came from; that the BMPs, the cookies and the two assumed files are synthetic; that
nothing in the folder may hold a real address, UID, name, token or password; and how to regenerate the
files.

### 17.6 Captured content

The sanitized capture the `.bin` fixtures are built from. Each line below is one line of the file and
ends in CRLF; an empty line is CRLF alone; the file ends with `</SM>` and CRLF. `{FF}` is the single
byte 0xFF. `{BMP n}` is `data:image/bmp;base64, ` (with the space) followed by the base64 of the
synthetic BMP with n black pixels (§17, first paragraph). Everything else is ASCII. Built this way, A is
exactly 29,482 bytes.

**A. `sme_load_slot9.bin`** (29,482 bytes)

```
<SM>
<TYPE>Load</TYPE>
<TN>326548</TN>
<XID>4</XID>
<MSNO>9</MSNO>
<FCDOC>
<VER>1</VER>
<W>256</W>
<H>160</H>
<MSGNAM>STRIPES TEST</MSGNAM>
<CTEXT>
<LANG>4</LANG>
<TXT>STRIPES TEST 123</TXT>
<FONTIND>1</FONTIND>
<SIZE>2</SIZE>
<POS>10,10</POS>
<BOUND>10,10,218,33</BOUND>
<POSF></POSF>
<PREF></PREF>
<ROTAT>0</ROTAT>
<WCOEFF>0</WCOEFF>
<GROUP>0</GROUP>
<OBJTYP>0</OBJTYP>
<COLR>0</COLR>
<XRID>0</XRID>
<YRID>0</YRID>
<SXAS>0</SXAS>
<SYAS>0</SYAS>
<OBJID>10001</OBJID>
<RP>0</RP>
<MARG>0,0</MARG>
</CTEXT>
<CTDAT>
<FRMT>Y/M/D</FRMT>
<AMPM>{FF}</AMPM>
<DAY>0</DAY>
<DWEEK>0</DWEEK>
<HOUR>0</HOUR>
<HORFLG>1</HORFLG>
<LANG>17742</LANG>
<MIN>0</MIN>
<MON>0</MON>
<SEC>0</SEC>
<YEAR>0</YEAR>

<LANG>4</LANG>
<TXT>2026/10/29</TXT>
<FONTIND>1</FONTIND>
<SIZE>2</SIZE>
<POS>10,60</POS>
<BOUND>10,60,192,83</BOUND>
<POSF></POSF>
<PREF>PKD:</PREF>
<ROTAT>0</ROTAT>
<WCOEFF>0</WCOEFF>
<GROUP>0</GROUP>
<OBJTYP>1</OBJTYP>
<COLR>0</COLR>
<XRID>0</XRID>
<YRID>0</YRID>
<SXAS>0</SXAS>
<SYAS>0</SYAS>
<OBJID>1</OBJID>
<RP>0</RP>
<MARG>0,0</MARG>
</CTDAT>
<CLOT>
<INDX>1</INDX>

<LANG>4</LANG>
<TXT></TXT>
<FONTIND>2</FONTIND>
<SIZE>3</SIZE>
<POS>10,100</POS>
<BOUND>10,100,66,136</BOUND>
<POSF></POSF>
<PREF>LOT:</PREF>
<ROTAT>0</ROTAT>
<WCOEFF>0</WCOEFF>
<GROUP>0</GROUP>
<OBJTYP>1</OBJTYP>
<COLR>0</COLR>
<XRID>0</XRID>
<YRID>0</YRID>
<SXAS>0</SXAS>
<SYAS>0</SYAS>
<OBJID>2</OBJID>
<RP>0</RP>
<MARG>0,0</MARG>
</CLOT>
<CPPARM>
<ROTAT>0</ROTAT>
<BOLD>1</BOLD>
<WDTH>1</WDTH>
<QUANT>1</QUANT>
<DELAY>0</DELAY>
<QDELAY>0</QDELAY>
<AUTREP>0</AUTREP>
<REVRS>0</REVRS>
<DRAFT>0</DRAFT>
<MIRR>0</MIRR>
<SLANT>0</SLANT>
<ANG>30</ANG>
<SWDLY>0</SWDLY>
<SWSPD>250</SWSPD>
<WPDLY>0</WPDLY>
<PRNSPD>1000</PRNSPD>
<ENCST>0</ENCST>
<RESMOD>0</RESMOD>
<DLYMM>0</DLYMM>
<QDLYMM>0</QDLYMM>
<ENCO>0</ENCO>
<RESO>640</RESO>
<RESNAM>UltraFine</RESNAM>
<ENCSCL>100</ENCSCL>
</CPPARM>
<H>160</H>
</FCDOC><PRV>
<BPIX>2564</BPIX>
<MSGNAM>STRIPES TEST</MSGNAM>
<BMP>{BMP 2564}</BMP>
</PRV>
</SM>
```

**B. `sme_load_slot0_compact.bin`** (the FCDOC line is one line)

```
<SM>
<TYPE>Load</TYPE>
<TN>326548</TN>
<XID>6</XID>
<MSNO>1</MSNO>
<FCDOC><MSGNAM></MSGNAM><CTDAT><OBJID>1</OBJID><POS>0,0</POS><PREF>Packed: </PREF><FRMT>M/D/y</FRMT><DAY></DAY><MON></MON><YEAR></YEAR><POSF></POSF><FONTIND>1</FONTIND><SIZE>4</SIZE><MARG>0,0</MARG><RP>0</RP><LANG>0</LANG></CTDAT><CPPARM><ROTAT>0</ROTAT><MIRR>0</MIRR><REVRS>0</REVRS><AUTREP>0</AUTREP><DELAY>0</DELAY><DLYMM>0</DLYMM><PRNSPD>2500</PRNSPD><BOLD>1</BOLD><WDTH>1</WDTH><QUANT>1</QUANT><QDELAY>0</QDELAY><QDLYMM>0</QDLYMM><RESMOD>0</RESMOD><RESNAM>UltraFine</RESNAM><ENCST>0</ENCST><ENCSCL>100</ENCSCL></CPPARM><H>160</H>
</FCDOC><PRV>
<BPIX>3492</BPIX>
<MSGNAM></MSGNAM>
<BMP>{BMP 3492}</BMP>
</PRV>
</SM>
```

**C. `sme_save_stale.bin`**

```
<SM>
<TYPE>Save</TYPE>
<TN></TN>
<XID></XID>
<MSNO>9</MSNO>
<PRV>
<BPIX>2564</BPIX>
<MSGNAM>STRIPES TEST</MSGNAM>
<BMP>{BMP 2564}</BMP>
</PRV>
</SM>
```

**D. `sme_load_slot9_empty.bin`**

```
<SM>
<TYPE>Load</TYPE>
<TN>326548</TN>
<XID>2</XID>
<MSNO>9</MSNO>
<PRV>
<BPIX>0</BPIX>
<MSGNAM></MSGNAM>
<BMP>{BMP 0}</BMP>
</PRV>
</SM>
```

**E. `sme_thumbnail_slot9.bin`**

```
<SM>
<TYPE>Thumbnail</TYPE>
<TN>326548</TN>
<XID>3</XID>
<MSNO>9</MSNO>
<PRV>
<BPIX>2564</BPIX>
<MSGNAM>STRIPES TEST</MSGNAM>
<BMP>{BMP 2564}</BMP>
</PRV>
</SM>
```

**F. `sme_load_other_client.bin`**: B with its TN and XID lines replaced:

```
<TN>834854</TN>
<XID>2</XID>
```

**G. The five Autopilot writes** that made the slot 9 message, in order (each the APCMD content of one request; each was answered `0`). The fidelity test (§18) replays them:

```
<CRTMSG>STRIPES TEST</CRTMSG>
<CTEXT><OBJID>10001</OBJID><POS>10,10</POS><TXT>STRIPES TEST 123</TXT><FONTIND>1</FONTIND><SIZE>3</SIZE><MARG>0,0</MARG></CTEXT>
<CTDAT><POS>10,60</POS><PREF>PKD:</PREF><FRMT>Y/M/D</FRMT><DAY>0</DAY><MON>0</MON><YEAR>0</YEAR><FONTIND>1</FONTIND><SIZE>3</SIZE><MARG>0,0</MARG></CTDAT>
<CLOT><POS>10,100</POS><PREF>LOT:</PREF><INDX>1</INDX><FONTIND>2</FONTIND><SIZE>4</SIZE><MARG>0,0</MARG></CLOT>
<SAVEMSG>9</SAVEMSG>
```

## 18. Tests (test_hx_protocol.py)

`unittest`, in the style of the existing test files. Socket tests start a `FakePrinter(...)` with
`addCleanup(fake.stop)` and use a fresh `GateRegistry()`. Single-threaded ones share a `FakeClock()`
(auto-advance) with the fake; threaded ones use `TIMINGS.scaled(0.1)`, and the silent-printer test
`TIMINGS.scaled(0.05)`. No test spends more than about 0.5 s of real time waiting.

| Class | Test | Asserts |
|---|---|---|
| TokenTests | manual example | `make_token("123321") == "Yzg4MzdiMjNmZjhhYWE4YTJkZGU5MTU0NzNjZTA5OTE="`; `password_md5` of it is `c8837b23ff8aaa8a2dde915473ce0991` |
| | round trip | `unscramble(scramble(t, k), k) == t` for `DEV_KEY` and a random 32-byte key; two scrambles differ; the result starts `hx1:` + `key_id(k)` + `:` |
| | another key, damage | another key raises `AuthFailed` with the "different Stripes build" text; one changed payload character, or `""`, raises it with the "damaged" text; `password_md5("abc")` raises `AuthFailed`; `scramble("abc", k)` raises `ValueError` |
| | release key | `sys.modules["hx_key"] = None` (patched) gives `DEV_KEY`; a module with `KEY = b"short"` raises `RuntimeError` |
| | ClientPool | an empty token raises `AuthFailed`; the same record gives the same client; a changed token gives a new one; `label` is the record's name |
| CommandTests | escaping | `Command.block("CTEXT", [("TXT", "R&D <1>")]).xml == "<CTEXT><TXT>R&amp;D &lt;1&gt;</TXT></CTEXT>"`, and `xml_escape` agrees |
| | whitelist | a valid example of every `ALLOWED_WRITE_TAGS` tag passes `check_write`; every `PRINTER_WIDE_WRITES` and `UNUSED_WRITES` tag, SAVEMSG 1, PRINTMSG 8, PRINT 2, CPPARM with SLANT, CTEXT with SIZE 11, CTEXT with LANG, a smuggled second command (`<PRINT>1</PRINT><PRINT>0</PRINT>` as one `Command`) and a `Command.query` raise `ForbiddenCommand` |
| | through the client | each refused case through `command()` raises before any request (`fake.requests == []`); so do `query()` of CPPARM, ODPARAM, RS232, BEAC, LOT1 and SETPPARAM |
| ReplyTests | table | PRIN "1" → 1, "0" → 0, "2" BadReply, "7" PrinterBusy; MSGNAM "None" → None, "1" BadReply, 21 characters BadReply; DATE "29/10/2026" → "2026-10-29", "31/02/2026" and "1" BadReply; TIME "21:11:41" → "21:11:41", "24:00:00" BadReply; MSCNT "436336" → 436336; INKL "5" BadReply; DELAY "284" → 284; HXUI "1109" → 1109 |
| | codes never values | for every `QUERY_SPECS` tag: "-1" BadReply, "6" and "7" PrinterBusy |
| | decoding | `decode_text(b"\xff") == "\xff"`; `decode_text("ñ".encode()) == "ñ"`; `read("HXNA")` of a fake answering `" Printer 1"` is `"Printer 1"` |
| ParserTests | each fixture | the §11.5 table and details, field by field |
| | robustness | the slot 9 fixture with CRLF turned into LF parses to an equal `MessageDesign`; the data URI without its space parses; `hx_fake.bmp_1bit([(10, 10, 651, 14)])` equals the fixture's decoded BMP |
| | failures | the fixture cut in half, an 800 × 160 BMP, a 4-bit BMP, a Load without PRV, different FCDOC and PRV names, stray text inside CTEXT, and `<MSNO>x</MSNO>` all raise `WebReadFailed` |
| | nesting depth | the slot 9 fixture with A nested inside CTEXT: 4 levels give the pair `("A.A.A.A.B", "1")`, 12 levels (B at depth 16) still parse, 13 and 1,500 levels (40 KB) raise `WebReadFailed` |
| | classify | with `tn="326548"`: the slot 9 fixture at xid 4 is `match`, at xid 5 `stale`; the Save fixture `stale`; the other-client fixture `foreign`; the thumbnail at xid 3 `odd`; `b"7"` `busy`; `b"<html>"` `odd`. `is_stripes_tn(new_tn())` 100 times; `is_stripes_tn("326548")` and `is_stripes_tn("834854")` are False |
| RequestTests | golden | each §17.3 builder call equals its file, byte for byte |
| | on the wire | the fake's recorded `raw` for `read("PRIN")` equals `autopilot_request(...)`, and for a Load equals `sme_request("127.0.0.1", fake.port, ...)` |
| AutopilotTests | read | `read("PRIN") == 1` with one request |
| | authentication | a wrong token raises `AuthFailed` after exactly one request |
| | idle drop | shared `FakeClock`, `drop_after_idle_s=3.0`: read, `clock.advance(5)`, read gives the value; 3 requests in all; nonsense 0 |
| | busy | `override("PRIN", "7")` raises `PrinterBusy` after 2 requests and `busy_replies == 2`; a callable giving "7" then "1" returns 1 after 2 requests |
| | nonsense | `override("PRIN", "Example")` raises `BadReply` after 2 requests; nonsense 2 |
| | warm-up | `FakeClock`: the first `command(PRINT 0)` sends HXUI then PRINT; at once again, PRINT only; after `advance(3.0)`, HXUI then PRINT |
| | warm-up, wrong UID | fake UID 1110, client UID 1109: `WrongPrinter`, and no PRINT arrives |
| | a write is never repeated | `override("PRINT", FakePrinter.DROP)` raises `NoReply` with the "may or may not" text after exactly one PRINT; `override("PRINT", "3")` raises `CommandRefused(code=3)` after one PRINT; `"7"` raises `CommandRefused(code=7)` after one PRINT |
| | echo | with `write_echo = True`, `command(PRINT 1)` succeeds |
| | read_twice, check_uid | an override alternating "0" and "1" makes `read_twice("PRIN")` raise `BadReply` with nonsense + 1; fake UID 1110 makes `check_uid()` raise `WrongPrinter(expected=1109, got="1110")` after two reads |
| | silent | `go_silent()` with `TIMINGS.scaled(0.05)`: `NoReply` after two attempts; `max_in_flight <= 1` |
| | deadline before every exchange | `FakeClock`: an HXUI warm-up answer that moves the clock 6 s makes `command(PRINT 0, deadline=now + 7.5)` raise `PrinterBusy` with only the HXUI sent; a first answer "7" that moves the clock 3 s makes `read`, `query` and `check_uid` with `deadline=now + 5` raise `PrinterBusy` after one request, and so does a 3 s jump in the gap before the retry; `read_twice` and `check_uid` work with an 11 s deadline, and `read_twice` with 5.5 s sends only its first read |
| GateTests | one in flight | `latency_s=0.005`; 8 threads, each doing 5 mixed reads and one `load_slot(0)`, through one `ClientPool`: every result right, `max_in_flight == 1`, `violations == []`. Again with two `PrinterClient`s sharing one `GateRegistry` |
| | gate timeout | `Timings(gate_wait_s=0.1)`; another thread holds the gate: `PrinterBusy` |
| | probe | `probe()` on a running fake is True, the fake's `requests` stay empty, `max_in_flight` stays 0 and there is no violation; while another thread holds the gate it returns None at once; on a stopped fake it is False |
| | worst cases | `worst_case_s` gives 5.0, 7.5, 11.0, 10.0 and 15.0 for read, command, read_twice, load_slot and fonts at the default timings, halves with `TIMINGS.scaled(0.5)`, and raises `ValueError` for `"other"` |
| WebTests | first load | build a two-item message in slot 9 with `command()`, then `load_slot(9)`: the requests after the writes are login, Load and one repeat (the Save leftover came first); the design is what was built; nonsense 0 |
| | stale kinds | `leave_in_buffer(<the other-client fixture>)`: the load works and nonsense is 1. Two loads in a row: the second's first reply is our own previous result, nonsense 0 |
| | cap | `web_result_delay_s = float("inf")` with `FakeClock`: `WebReadFailed` after exactly 1 Load and 5 repeats; with `Timings(read_retry_s=1.0)`, after 2 repeats |
| | deadline | `FakeClock`: `load_slot(9, deadline=clock.monotonic() + 1.0)` raises `WebReadFailed` before any request; with `web_result_delay_s = float("inf")` and `deadline = clock.monotonic() + 3.5`, it stops after the Load and the repeats that fit (none starts later than `deadline - http_timeout_s`) |
| | gate deadline | another thread holds the client's gate (the test takes `gates.gate(f"{host}:{port}")` on a second thread): `read("PRIN", deadline=clock.monotonic() + 5.3)` waits at most about 0.3 s (5.3 minus `worst_case_s("read")` 5.0, default timings, `SystemClock`) and raises `PrinterBusy` with no request in the fake's `requests`; a gate freed in time lets it through; `command(..., deadline=...)` and `load_slot(0, deadline=...)` behave the same (the latter with `WebReadFailed`) |
| | fresh | `web_first_reply_fresh = True`: no repeat is sent |
| | logging in again | after `expire_web_session()` the next load gets 401, logs in and succeeds, `relogins == 1`; after `set_password("other")` it raises `AuthFailed` after one login attempt |
| | dropped Load | after one `load_slot`, `drop_after_idle_s=3.0` and `clock.advance(5)`: the next `load_slot`'s first Load is dropped, a second Load with the next XID succeeds |
| | wrong slot | a `web_reply_hook` changing `<MSNO>9</MSNO>` to `<MSNO>10</MSNO>`: `WebReadFailed`, nonsense 1 |
| | nested too deep | a `web_reply_hook` nesting 1,500 levels inside slot 1's CTDAT: `load_slot(0)` raises `WebReadFailed` with the label, nonsense 1, and the next `load_slot(0)` works |
| | slot 0 | `load_slot(0).slot == 1` on a fresh fake |
| | fonts, firmware | `fonts() == list(STANDARD_FONTS)`, also with `gzip_files`; `files["/sfl.xml"] = None` raises `BadReply`; `firmware() == "69.75.10"`; `files["/idn.xml"] = None` gives `None` |
| FakePrinterTests | verbose fidelity | replay the five writes of §17.6 G (with CLOT) as raw requests to a delay fake on `FakeClock()` (29 Oct 2026), then a raw Load with TN 326548, XID 4, MSNO 9 and a raw repeat: the repeat's body equals `sme_load_slot9.bin` once both have their BPIX value and BMP payload replaced by placeholders; `violations == ["write not whitelisted CLOT"]` |
| | compact fidelity | a fresh delay fake's slot 0 load (raw, TN 326548, XID 6) equals `sme_load_slot0_compact.bin` the same way |
| | overlap | `latency_s=0.05`: a raw PRIN, then another 0.01 s later: `max_in_flight == 2`, "two requests in flight" in `violations`, the first answers `1` and the second `7`. With `mixup=True` and `mixup_p=1.0`, a PRIN then a MSGNAM 0.01 s later: the MSGNAM answer is `1`, the PRIN's reply |
| | overload | `overload_at=3`, `overload_pause_s=0.2`, 3 raw requests at once: all closed without a reply; a request 0.3 s later is answered |
| | multi-value | raw `<CPPARM/>` with `multi_value_hang_s=0.1`: "6" after at least 0.1 s, and the violation |
| | editor page | raw `GET /simple_msg.html` with `editor_page_bytes=200_000` and `editor_stall_s=0.2`: the violation, and a PRIN sent meanwhile is answered only after the stall |
| | settings, DELAY, TD | a message saved with CPPARM DELAY 284176, then PRINTMSG: `<DELAY/>` answers 284; TD 29/10/2026 08:45: TIME answers "08:45:00"; TD with HOUR 25 answers "3" |
| FakeWorldTests | seed | the store reads the seeded company, line, machine, printer, film and print text; `config/hx/changes.json` has counter 1; `company.time_server == f"127.0.0.1:{world.sntp.port}"` and `world.time_source().now(fresh=True).source == "server"`; two `world.store(...)` calls get different `fs` views; `world.printers[0].printer_record()`'s token unscrambles with `release_key()` |

test_hx_time covers SNTP against `FakeSntp`, and test_hx_send covers `compare` against the fake (§12).

## 19. OPEN items

| Item | Built now | To confirm on the test unit |
|---|---|---|
| Font list source (a plan deviation for the owner's sign-off, architecture §12.1) | `GET /sfl.xml`, `<FONn>` tags; any error fails the machine check | the file, the tags, whether cookies are needed, no effect on printing |
| Firmware source | `GET /idn.xml`, the first of `FIRMWARE_TAGS`, else `None` | the file and the tag |
| Replies to PRINT, PRINTMSG, TD, CPPARM | a first token of `0` means done | the exact text |
| FCDOC DELAY units | µs, compared exactly | that a sent 480,000 reads back 480,000 |
| PRNSPD above 1200 | allowed up to 2500, read back exactly | that 2400 is accepted, stored and read back; the units |
| TD ranges, midnight, seconds | HOUR 0..24 and DTRM 0..59 allowed; there is no seconds field | which HOUR and DTRM values are accepted and what the seconds become (send.md times the TD) |
| `& < >` and `ñ` | escaped when sent, compared raw | how they print and read back |
| Busy codes on SME | classified `busy` | whether they happen |
| Login | both cookies required, the page discarded | the reply to a wrong password, the page size, the session lifetime, two PCs' sessions |
| PRINTMSG to an empty slot; MSCNT on a message change | the fake answers `4`; it resets MSCNT | real behaviour; which counter counts bags (architecture §11) |
| Load of slot 0 while printing is off | the selected message is assumed | what it returns |
| OBJID numbering and item defaults (WCOEFF, GROUP, …) for other fonts and sizes | `compare` expects the defaults seen | the same with every font and size |
| RESNAM for RESMOD 1 to 3; RESO | Fine, Normal, Draft; RESO ignored | the spellings |
| The printer echoes any TN | Stripes TNs are 42 mod 97 | |
| A timed-out request keeps running on the printer | the gate is held `read_retry_s` after `NoReply` | |
| A bare TCP connect (`probe`) | allowed | no effect on a printing printer |
| What BPIX means | not checked | |
| What a relative item's POS reads back as | POS isn't compared when not sent | |
| The beacon | never queried or switched (`BEACON_SUPPORTED = False`) | architecture §11 |

## 20. Decisions made in this spec

1. **Raw sockets for all printer HTTP**, with the Autopilot bytes exactly as verified (`Content-Length`,
   `Connection: keep-alive`, no Host) and the web requests exactly as the browser sent them. No library
   that could pick up a proxy.
2. **`query()` refuses any tag outside `QUERY_SPECS`**, not only the three multi-value queries, so the
   set of queries Stripes can send is a short, tested list.
3. **The write whitelist covers shape, children and values**, not just the outer tag: exactly one
   element, only the listed children, values in the manual's ranges, SAVEMSG and PRINTMSG only to 9 or
   10. `LANG` isn't allowed.
4. **At most two requests per `read()`**, shared between no reply, busy and a failed sense check.
5. **The gate stays held for `read_retry_s` after any `NoReply`**, because the printer may still be
   processing the request that timed out.
6. **`-1`, `6` and `7` are never values**, even where a range would allow them; `6` and `7` are busy
   and asked again, `-1` is nonsense.
7. **A write succeeds when the first token of its answer is `0`**, which accepts both the seen `0` and
   the manual's "0 then the value". Any other integer is `CommandRefused`, anything else `BadReply`.
8. **The warm-up read must return this printer's UID**; a mismatch is `WrongPrinter` and the write isn't
   sent.
9. **The gap before each `sme_repeat` is `read_retry_s`** (0.5 s, as the browser waited), so tests
   scale it with the other timings and no `Timings` field was added.
10. **Stripes TNs satisfy `int(tn) % 97 == 42`**, so a leftover from another Stripes PC is expected and a
    leftover from any other web client counts as "something else is talking".
11. **Only Load is sent**; Thumbnail replies are understood but never requested.
12. **A Load that gets no reply is sent again once with a new XID**, and a session that has gone is
    logged into again once per call.
13. **The parser is strict and decodes no XML entities**; each value is strict UTF-8, else latin-1, so
    the 0xFF byte survives and no wrong print can pass by decoding.
14. **FCDOC and PRV message names must agree**, and the preview must be exactly the 1-bit 1024 × 160 BMP
    seen. BPIX isn't checked against the pixels.
15. **The font list and firmware come from two small web files** (`/sfl.xml`, `/idn.xml`), which
    architecture §6.4 now lists as the only additions to the web requests; architecture §12.1 records
    them as a plan deviation for the owner's sign-off. Any font-list failure fails the machine check; a
    missing firmware version only logs "unknown".
16. **Busy replies are counted apart from nonsense** (`busy_replies`), so states.md can weigh them
    separately.
17. **Scrambling uses a SHA-256 keystream with a 16-byte HMAC-SHA-256 tag and a key id**, so a token
    from another build gives a clear message, and damage is always detected.
18. **`release_key()` raises for a malformed `hx_key.KEY`** rather than silently using the development
    key.
19. **The login reads only the status and cookies** and discards the page, cut short if it is long.
20. **Cookies live only in memory**, in the `PrinterClient`.
21. **TD's HOUR 0..24 and DTRM 0..59 are allowed** by both the client and the fake, pending the test
    unit, so send.md's midnight handling isn't blocked by the manual's unclear ranges.
22. **The read-back rules in §12** (unknown fields fail; OBJID, POS and MARG are compared only when
    sent; BOUND, LANG, AMPM, HORFLG, OBJTYP and RESO are ignored; item-level ROTAT, WCOEFF, GROUP, COLR
    and RP must be 0; a blank picture of a message with text fails).
23. **The fake's preview draws boxes, not text** (a standard-library 1-bit BMP writer), with a crude font
    metric that reproduces the capture's BOUNDs exactly, so fake replies are deterministic everywhere.
24. **The fake reads the injected clock** for dates, idle drops and web result delays, and uses real time
    for latency, hangs and stalls.
25. **The fake counts a request in flight from when it has been read until just before its reply is
    written**; silent and dropped requests aren't counted, so a client that waits correctly never shows
    a false overlap.
26. **`check_uid` confirms a mismatch with a second read**, and `read_twice` releases the gate between
    its reads.
27. **Fixtures are generated by a self-contained script** with synthetic BMPs that keep the real header
    and BPIX values, plus golden request files that pin the wire format.
28. **Reply size limits**: 4 KB Autopilot, 256 KB SME, 64 KB files, 1 MB login (cut short allowed), 16 KB
    of headers.
29. **FakeWorld seeds records with `hx_share.write_record`** at storage.md's paths plus a
    `changes.json` (counter 1), so it doesn't depend on `DataService` running and data cycles don't
    reload in full every time; each `store()` gets its own `fs.view()`; the company names the fake
    time server with its port.
30. **`PrinterClient.probe()`** gives the reachability check a way to touch a printer that still
    respects the gate: it never waits for the gate (None when busy), and the fake ignores a connection
    that sends nothing.
31. **`load_slot` takes a deadline** and `worst_case_s` gives every call's worst case, so lock holders
    and the poller can prove a request ends before another PC may judge their lock or `poll.json`
    stale (architecture §7.3 rule 8). `query`, `read` and `command` take one too, and with a deadline
    the wait for this PC's gate is bounded by it (§9 item 2), so the poller never sits behind a local
    lock holder past its bound and then sends late. `read_twice` and `check_uid` take one as well, and
    after the gate it is checked before every exchange (the write after the warm-up, a read's retry), so
    a lock holder that passes its lease end as the deadline sends nothing once the lease is over, even
    after its PC froze mid-call (send.md §10.5).
32. **SME nesting is capped at `MAX_SME_DEPTH` (16)**, four times what a real reply uses, so a broken
    or hostile reply is `WebReadFailed` (nonsense) like any other, never a `RecursionError`.
