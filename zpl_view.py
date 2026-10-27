"""Reading typed ZPL back into marks for the on-screen preview (the ZPL tab).

Covers the commands labels are usually written with: field origins (^FO, ^FT, ^LH),
fonts (^A, ^CF), text blocks (^FB), field data (^FD, ^FH, ^FX), boxes and lines (^GB),
graphics (^GF, ASCII hex), and the barcodes Stripes knows (^BY with ^BC, ^B3, ^BE, ^BU,
^BQ). Anything else is listed as not shown, and commands that change the printer's own
settings are listed separately so the app can warn before printing them.

Each field remembers where its ^FO/^FT numbers are in the text, so dragging it on the
preview can rewrite them.
"""

import re
from dataclasses import dataclass, field

from PIL import Image

import label_design as ld
from i18n import tr, N_

# Commands that change how the printer is set up. They stay in effect after the label
# and affect what other software prints (some even save to the printer's memory).
SETTINGS_COMMANDS = {  # descriptions are shown with tr()
    "PW": N_("print width"), "LL": N_("label length"), "LS": N_("label shift"), "LT": N_("label top"),
    "MD": N_("darkness"), "PR": N_("print speed"), "MN": N_("media type"), "MT": N_("thermal type"),
    "MM": N_("print mode"), "MU": N_("units"), "PO": N_("print orientation"), "PM": N_("mirror image"),
    "ML": N_("maximum label length"), "MF": N_("media feed"), "JU": N_("saving or resetting the configuration"),
    "JZ": N_("reprint after error"), "KD": N_("display"), "KN": N_("printer name"),
    "KP": N_("front panel password"), "NC": N_("network"), "ND": N_("network address"), "NI": N_("network ID"),
    "NS": N_("network settings"), "SC": N_("serial port"), "ST": N_("printer clock"), "WI": N_("wireless"),
    "ID": N_("deleting stored objects"), "DF": N_("storing a format"), "IS": N_("storing a format"),
    "DG": N_("storing a graphic"), "SD": N_("darkness"), "TA": N_("tear-off position"), "JS": N_("backfeed"),
    "JR": N_("printer reset"), "JC": N_("media calibration"), "JL": N_("length calibration"),
    "SE": N_("encoding table"), "CC": N_("command prefix"), "CT": N_("command prefix"), "CD": N_("delimiter"),
    "JD": N_("diagnostics"), "JM": N_("dots per mm"), "WC": N_("printing the configuration label"),
}
# Recognised and safe to ignore in the preview.
_IGNORED = {"XA", "CI", "PQ", "FR", "FS", "FW", "PF", "HH", "HS", "XB", "FN", "SN", "SF", "FV", "MC", "PH", "XG"}


@dataclass
class Field:
    """One field (^FO ... ^FS) as drawn: its marks, box and where its origin is typed."""
    marks: list
    x: int
    y: int
    w: int
    h: int
    origin: tuple = None       # (x, y) exactly as typed (before ^LH)
    origin_span: tuple = None  # (start, end) of the "x,y" text in the source
    source_span: tuple = None  # (start, end) of the whole field in the source


@dataclass
class ZplView:
    marks: list = field(default_factory=list)
    fields: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    errors: list = field(default_factory=list)
    settings: list = field(default_factory=list)  # (code, what it changes)
    labels: int = 0                               # how many ^XA ... ^XZ formats


_TOKEN = re.compile(r"[\^~]([A-Za-z@][A-Za-z0-9@]?)")


def _params(text):
    return [p.strip() for p in text.split(",")] if text.strip() else []


def _int(params, i, default):
    try:
        return int(float(params[i]))
    except (IndexError, ValueError):
        return default


def _decode_fh(data, indicator="_"):
    out, i = [], 0
    while i < len(data):
        if data[i] == indicator and re.fullmatch(r"[0-9A-Fa-f]{2}", data[i + 1:i + 3]):
            out.append(chr(int(data[i + 1:i + 3], 16)))
            i += 3
        else:
            out.append(data[i])
            i += 1
    return "".join(out)


_REPEAT = {**{c: i + 1 for i, c in enumerate("GHIJKLMNOPQRSTUVWXY")},
           **{c: (i + 1) * 20 for i, c in enumerate("ghijklmnopqrstuvwxyz")}}


def decode_gf_hex(data, row_bytes):
    """^GF ASCII hex, including Zebra's run-length shorthand, as rows of bytes."""
    width = row_bytes * 2  # hex digits per row
    rows, row, count, i = [], "", 0, 0
    data = re.sub(r"\s", "", data)
    while i < len(data):
        c = data[i]
        if c in _REPEAT:
            count += _REPEAT[c]
        elif c in "0123456789ABCDEFabcdef":
            row += c * (count or 1)
            count = 0
        elif c == ",":  # rest of the row is white
            row = row.ljust(width, "0")
        elif c == "!":  # rest of the row is black
            row = row.ljust(width, "F")
        elif c == ":":  # same as the previous row
            row = rows[-1] if rows else "0" * width
        else:
            raise ValueError(tr("^GF data has an unexpected character {char}", char=repr(c)))
        i += 1
        while len(row) >= width:
            rows.append(row[:width])
            row = row[width:]
    if row:
        rows.append(row.ljust(width, "0"))
    return [bytes.fromhex(r) for r in rows]


def _graphic(params):
    """^GFA,total,total,row_bytes,data -> 1-bit image (black = 0), or raise ValueError."""
    if len(params) < 5 or params[0].upper() != "A":
        raise ValueError(tr("only ^GFA (ASCII hex) graphics can be previewed"))
    row_bytes = _int(params, 3, 0)
    if row_bytes <= 0:
        raise ValueError(tr("^GF needs the bytes per row"))
    data = ",".join(params[4:])  # commas inside the data are run-length codes
    rows = decode_gf_hex(data, row_bytes)
    img = Image.frombytes("1", (row_bytes * 8, len(rows)), b"".join(rows))
    from PIL import ImageOps
    return ImageOps.invert(img.convert("L")).convert("1")  # ZPL 1 = black; preview black = 0


class _State:
    def __init__(self):
        self.home = (0, 0)
        self.font_h, self.font_w = 30, 30   # ^CF default; Zebra's power-on default is tiny
        self.by_module, self.by_ratio, self.by_height = 2, 3.0, 10
        self.new_field()

    def new_field(self):
        self.origin = None          # (x, y, kind) in dots, before ^LH
        self.origin_span = None
        self.field_start = None
        self.fh = None              # ^FH indicator, or None
        self.fb = None              # (width, lines, spacing, justify)
        self.font = None            # (h, w) from ^A, or None for ^CF's
        self.barcode = None         # (kind, params)
        self.graphic = None         # ("box", params) or ("gf", params)
        self.data = None


def parse(text, label):
    """Read ZPL text into a ZplView: marks to draw, fields to drag, and what to warn about."""
    view = ZplView()
    st = _State()
    unknown, rotated = [], False
    tokens = list(_TOKEN.finditer(text))
    in_label = False
    for n, m in enumerate(tokens):
        code = m.group(1).upper()
        prefix = text[m.start()]
        end = tokens[n + 1].start() if n + 1 < len(tokens) else len(text)
        arg_start = m.end()
        # ^A takes its font letter as part of the command: ^A0N,30,30 -> font "0", "N,30,30"
        if code[0] == "A" and len(code) == 2:
            arg_text = text[arg_start:end]
            font_params = _params(arg_text)
            if font_params and font_params[0][:1].upper() not in ("", "N"):
                rotated = True
            st.font = (_int(font_params, 1, st.font_h), _int(font_params, 2, _int(font_params, 1, st.font_w)))
            continue
        args = text[arg_start:end]
        params = _params(args)

        if prefix == "~":
            if code in SETTINGS_COMMANDS:
                view.settings.append((f"~{code}", SETTINGS_COMMANDS[code]))
            continue
        if code == "XA":
            in_label = True
            view.labels += 1
            continue
        if code == "XZ":
            in_label = False
            if view.labels >= 1:
                rest = text[m.end():]
                if "^XA" in rest.upper():
                    view.warnings.append(tr("Only the first label (^XA ... ^XZ) is shown in the preview."))
                    # still note settings commands further on
                    for later in _TOKEN.finditer(rest):
                        c = later.group(1).upper()
                        if c in SETTINGS_COMMANDS:
                            view.settings.append((("~" if rest[later.start()] == "~" else "^") + c,
                                                  SETTINGS_COMMANDS[c]))
                    break
            continue
        if code in SETTINGS_COMMANDS:
            view.settings.append((f"^{code}", SETTINGS_COMMANDS[code]))
            continue
        if code == "LH":
            st.home = (_int(params, 0, 0), _int(params, 1, 0))
        elif code == "CF":
            st.font_h = _int(params, 1, st.font_h)
            st.font_w = _int(params, 2, st.font_h)
        elif code in ("FO", "FT"):
            if st.field_start is None:
                st.field_start = m.start()
            nums = re.match(r"\s*(-?\d+)\s*,\s*(-?\d+)", args)
            st.origin = (_int(params, 0, 0), _int(params, 1, 0), code)
            if nums:
                st.origin_span = (arg_start + nums.start(1), arg_start + nums.end(2))
        elif code == "FB":
            just = (params[3].upper() if len(params) > 3 and params[3] else "L")[:1]
            st.fb = (_int(params, 0, 0), max(1, _int(params, 1, 1)), _int(params, 2, 0), just)
        elif code == "FH":
            st.fh = (args.strip()[:1] or "_")
        elif code == "FX":
            pass  # comment
        elif code == "BY":
            st.by_module = max(1, _int(params, 0, st.by_module))
            try:
                st.by_ratio = float(params[1]) if len(params) > 1 and params[1] else st.by_ratio
            except ValueError:
                pass
            st.by_height = _int(params, 2, st.by_height)
        elif code in ("BC", "B3", "BE", "BU", "BQ"):
            if params and params[0][:1].upper() not in ("", "N"):
                rotated = True
            st.barcode = (code, params)
        elif code == "GB":
            st.graphic = ("box", params)
        elif code == "GF":
            st.graphic = ("gf", params)
        elif code == "FD":
            st.data = args
            if st.field_start is None:
                st.field_start = m.start()
        elif code == "FS":
            _emit(view, st, label, field_end=end)
            st.new_field()
        elif code in _IGNORED:
            pass
        else:
            if f"^{code}" not in unknown:
                unknown.append(f"^{code}")
    if in_label and view.labels:
        view.warnings.append(tr("The label isn't closed with ^XZ."))
    if not view.labels and text.strip():
        view.errors.append(tr("ZPL labels start with ^XA and end with ^XZ."))
    if rotated:
        view.warnings.append(tr("Rotated fields are shown unrotated in the preview."))
    if unknown:
        view.warnings.append(tr("Not shown in the preview:") + " " + " ".join(unknown[:12]) +
                             (" ..." if len(unknown) > 12 else ""))
    return view


def _emit(view, st, label, field_end):
    """Turn the field collected so far into marks."""
    if st.origin is None and st.graphic is None and st.data is None:
        return
    ox, oy, kind = st.origin or (0, 0, "FO")
    x, y = ox + st.home[0], oy + st.home[1]
    marks = []
    data = st.data or ""
    if st.fh:
        data = _decode_fh(data, st.fh)

    if st.graphic and st.graphic[0] == "box":
        p = st.graphic[1]
        t = max(1, _int(p, 2, 1))
        w, h = max(t, _int(p, 0, t)), max(t, _int(p, 1, t))
        color = (p[3].upper() if len(p) > 3 and p[3] else "B")[:1]
        mk = ld.Mark("box", x, y, w, h, text=color, module=t)
        marks.append(mk)
    elif st.graphic and st.graphic[0] == "gf":
        try:
            bitmap = _graphic(st.graphic[1])
            marks.append(ld.Mark("image", x, y, bitmap.width, bitmap.height, bitmap=bitmap))
        except ValueError as e:
            view.warnings.append(tr("Graphic not shown: {error}.", error=e))
    elif st.barcode:
        code, p = st.barcode
        if code == "BQ":
            mag = max(1, min(10, _int(p, 2, 2)))
            # field data starts with error correction + input mode, e.g. "QA," or "MA,"
            payload = re.sub(r"^[HQML][AM],", "", data, flags=re.I)
            if payload:
                matrix = ld.qr_matrix(payload)
                side = mag * len(matrix)
                marks.append(ld.Mark("qr", x, y if kind == "FO" else y - side, side, side,
                                     module=mag, pattern=matrix))
        else:
            sym = {"BC": "code128", "B3": "code39", "BE": "ean13", "BU": "upca"}[code]
            hi = {"BC": 1, "B3": 2, "BE": 1, "BU": 1}[code]
            fi = {"BC": 2, "B3": 3, "BE": 2, "BU": 2}[code]
            h = _int(p, hi, st.by_height)
            show = (p[fi].upper() if len(p) > fi and p[fi] else "Y") == "Y"
            payload = re.sub(r">[0-9:;5-8<=]", "", data) if sym == "code128" else data
            problem = ld.barcode_problem(sym, payload)
            if problem:
                view.errors.append(problem)
            else:
                modules = ld.barcode_modules(sym, payload)
                module = st.by_module
                width = module * len(modules)
                top = y if kind == "FO" else y - h
                marks.append(ld.Mark("bars", x, top, width, max(1, h), module=module, pattern=modules))
                if show:
                    th = max(10, module * 9)
                    marks.append(ld.Mark("text", x, top + h + 2, width, th,
                                         text=ld.barcode_human_text(sym, payload), text_h=th, align="center"))
    elif data:
        fh, fw = st.font or (st.font_h, st.font_w)
        fh = max(1, fh)
        if st.fb and st.fb[0] > 0:
            width, max_lines, spacing, just = st.fb
            align = {"C": "center", "R": "right"}.get(just, "left")
            lines = _wrap(data.replace("\\&", "\n"), width, fh)[:max_lines]
            top = y if kind == "FO" else y - fh
            # the printer puts ^FB-centered text left of center; show it where it prints
            left = x - round(ld.FB_CENTER_SHIFT * fh) if align == "center" else x
            for i, line in enumerate(lines):
                marks.append(ld.Mark("text", left, top + i * (fh + spacing), width, fh,
                                     text=line, text_h=fh, align=align))
        else:
            width = max(1, int(ld.text_em(data) * fw))
            top = y if kind == "FO" else y - fh  # ^FT places the baseline
            marks.append(ld.Mark("text", x, top, width, fh, text=data, text_h=fh, align="left"))

    if not marks:
        return
    x1 = min(mk.x for mk in marks)
    y1 = min(mk.y for mk in marks)
    x2 = max(mk.x + mk.w for mk in marks)
    y2 = max(mk.y + mk.h for mk in marks)
    view.marks.extend(marks)
    view.fields.append(Field(marks, x1, y1, x2 - x1, y2 - y1,
                             origin=(ox, oy) if st.origin else None, origin_span=st.origin_span,
                             source_span=(st.field_start or 0, field_end)))
    if x2 > label.width or y2 > label.height or x1 < 0 or y1 < 0:
        msg = tr("Something goes past the edge of the label.")
        if msg not in view.warnings:
            view.warnings.append(msg)


def _wrap(text, width, h):
    """Split text into lines that fit `width` dots at font height h (^FB wraps by words)."""
    lines = []
    for para in text.split("\n"):
        words, line = para.split(" "), ""
        for word in words:
            trial = f"{line} {word}".strip()
            if line and ld.text_em(trial) * h > width:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
    return lines


def with_copies(text, copies):
    """The ZPL to send for `copies` labels: adds ^PQ before the last ^XZ unless the text has its own."""
    if copies <= 1 or re.search(r"\^PQ", text, re.I):
        return text
    i = text.upper().rfind("^XZ")
    return text if i < 0 else f"{text[:i]}^PQ{copies}\n{text[i:]}"
