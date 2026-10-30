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
