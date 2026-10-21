"""Label designs: what's on a label, where it goes, and the ZPL that prints it.

A design is a list of items (text line, barcode, QR code, picture). Each has a
box on the label, in printer dots. With auto layout on, the boxes are stacked
top to bottom to fill the label; otherwise they stay where the user put them.

Barcodes and QR codes are printed by the printer itself (^BC, ^BQ, ...), so they
always scan correctly. The encoders here only work out their size and draw the
on-screen preview.
"""

import base64
import copy
import io
import re
from dataclasses import dataclass, field, asdict, fields
from datetime import timedelta
from math import inf

MARGIN_IN = 0.06      # blank border around the label
ITEM_GAP_IN = 0.03    # space between stacked items
WIDTH_SAFETY = 1.08   # fudge factor on the character width estimate
HUMAN_TEXT_IN = 0.11  # height of the text printed under a barcode

KINDS = ("text", "barcode", "qr", "image")
ALIGNS = ("left", "center", "right")
SYMBOLOGIES = {"code128": "Code 128", "code39": "Code 39", "ean13": "EAN-13", "upca": "UPC-A"}

# Share of the free height each item gets in auto layout (text lines get 1).
_WEIGHTS = {"text": 1.0, "barcode": 1.4, "qr": 2.2, "image": 2.2}
_MIN_IN = {"barcode": 0.25, "qr": 0.3, "image": 0.2}
_MAX_IN = {"barcode": 1.0}


# --------------------------------------------------------------------------
# Model
# --------------------------------------------------------------------------

@dataclass
class Element:
    kind: str = "text"
    value: str = ""             # text, barcode data or QR data
    x: int = 0                  # box on the label, in dots
    y: int = 0
    w: int = 0
    h: int = 0
    size_pt: float = None       # text: fixed size, or None to fit the box
    align: str = "center"       # where the content sits inside its box
    symbology: str = "code128"  # barcode type
    show_text: bool = True      # barcode: print the digits underneath
    image_png: str = ""         # picture: base64 PNG (kept small)
    dither: bool = False        # picture: dither (photos) instead of threshold (logos)

    @classmethod
    def from_dict(cls, d):
        known = {f.name for f in fields(cls)}
        e = cls(**{k: v for k, v in d.items() if k in known})
        if e.kind not in KINDS:
            raise ValueError(f"unknown item type {e.kind!r}")
        if e.align not in ALIGNS:
            e.align = "center"
        if e.symbology not in SYMBOLOGIES:
            e.symbology = "code128"
        return e

    @property
    def title(self):
        """Short description for lists."""
        if self.kind == "text":
            return self.value or "(blank line)"
        if self.kind == "barcode":
            return f"{SYMBOLOGIES[self.symbology]}: {self.value}"
        if self.kind == "qr":
            return f"QR: {self.value}"
        return "Picture"


@dataclass
class Design:
    elements: list = field(default_factory=list)
    auto_layout: bool = True

    def to_dict(self):
        return {"elements": [asdict(e) for e in self.elements], "auto_layout": self.auto_layout}

    @classmethod
    def from_dict(cls, d):
        if "elements" not in d and "text" in d:  # recent label saved by an older version
            design = cls.from_text(d.get("text", ""), d.get("align", "center"), d.get("size_pt"))
            return design
        return cls([Element.from_dict(e) for e in d.get("elements", [])], bool(d.get("auto_layout", True)))

    @classmethod
    def from_text(cls, text, align="center", size_pt=None):
        lines = [ln.rstrip() for ln in text.splitlines()]
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return cls([Element("text", ln, size_pt=size_pt, align=align) for ln in lines])

    def simple_text(self):
        """The design as plain typed lines, if that's all it is (Simple tab); otherwise None."""
        if not self.auto_layout or any(e.kind != "text" or e.size_pt is not None or e.align != "center"
                                       for e in self.elements):
            return None
        return "\n".join(e.value for e in self.elements)

    def is_empty(self):
        return not any(e.value.strip() or e.image_png for e in self.elements)


@dataclass
class Label:
    """Label geometry in printer dots."""
    width: int
    height: int
    dpmm: int

    @property
    def dpi(self):
        return self.dpmm * 25.4

    @property
    def margin(self):
        return round(MARGIN_IN * self.dpi)

    def dots(self, inches):
        return inches * self.dpi


# --------------------------------------------------------------------------
# Text metrics
# --------------------------------------------------------------------------

# Rough character widths for Zebra font 0 (CG Triumvirate Bold Condensed),
# as a fraction of the character height. Used to keep text inside its box.
_NARROW = set("il.,:;'!|`")
_SEMI = set("fjrtI-()[]/\\\" ")
_WIDE_LOWER = set("mw")
_WIDE_UPPER = set("MW@%")


def char_em(c):
    if c in _NARROW:
        return 0.25
    if c in _SEMI:
        return 0.33
    if c in _WIDE_UPPER:
        return 0.82
    if c in _WIDE_LOWER:
        return 0.75
    if c.isdigit():
        return 0.52
    if c.islower():
        return 0.50
    if c.isupper():
        return 0.60
    return 0.60


def text_em(s):
    return sum(char_em(c) for c in s)


def text_fit_height(text, width):
    """Tallest text height (dots) that keeps `text` within `width` dots."""
    em = text_em(text) * WIDTH_SAFETY
    return width / em if em else inf


def pt_to_dots(pt, label):
    return pt / 72 * label.dpi


# --------------------------------------------------------------------------
# Barcode encoders (for size and preview only; the printer does the real encoding)
# --------------------------------------------------------------------------

_C128 = (
    "212222 222122 222221 121223 121322 131222 122213 122312 132212 221213 "
    "221312 231212 112232 122132 122231 113222 123122 123221 223211 221132 "
    "221231 213212 223112 312131 311222 321122 321221 312212 322112 322211 "
    "212123 212321 232121 111323 131123 131321 112313 132113 132311 211313 "
    "231113 231311 112133 112331 132131 113123 113321 133121 313121 211331 "
    "231131 213113 213311 213131 311123 311321 331121 312113 312311 332111 "
    "314111 221411 431111 111224 111422 121124 121421 141122 141221 112214 "
    "112412 122114 122411 142112 142211 241211 221114 413111 241112 134111 "
    "111242 121142 121241 114212 124112 124211 411212 421112 421211 212141 "
    "214121 412121 111143 111341 131141 114113 114311 411113 411311 113141 "
    "114131 311141 411131 211412 211214 211232 2331112"
).split()
_C128_START_B, _C128_START_C, _C128_TO_B, _C128_TO_C, _C128_STOP = 104, 105, 100, 99, 106

_C39 = {
    "0": "nnnwwnwnn", "1": "wnnwnnnnw", "2": "nnwwnnnnw", "3": "wnwwnnnnn", "4": "nnnwwnnnw",
    "5": "wnnwwnnnn", "6": "nnwwwnnnn", "7": "nnnwnnwnw", "8": "wnnwnnwnn", "9": "nnwwnnwnn",
    "A": "wnnnnwnnw", "B": "nnwnnwnnw", "C": "wnwnnwnnn", "D": "nnnnwwnnw", "E": "wnnnwwnnn",
    "F": "nnwnwwnnn", "G": "nnnnnwwnw", "H": "wnnnnwwnn", "I": "nnwnnwwnn", "J": "nnnnwwwnn",
    "K": "wnnnnnnww", "L": "nnwnnnnww", "M": "wnwnnnnwn", "N": "nnnnwnnww", "O": "wnnnwnnwn",
    "P": "nnwnwnnwn", "Q": "nnnnnnwww", "R": "wnnnnnwwn", "S": "nnwnnnwwn", "T": "nnnnwnwwn",
    "U": "wwnnnnnnw", "V": "nwwnnnnnw", "W": "wwwnnnnnn", "X": "nwnnwnnnw", "Y": "wwnnwnnnn",
    "Z": "nwwnwnnnn", "-": "nwnnnnwnw", ".": "wwnnnnwnn", " ": "nwwnnnwnn", "$": "nwnwnwnnn",
    "/": "nwnwnnnwn", "+": "nwnnnwnwn", "%": "nnnwnwnwn", "*": "nwnnwnwnn",
}
_C39_WIDE = 3  # ^BY ratio

_EAN_L = "0001101 0011001 0010011 0111101 0100011 0110001 0101111 0111011 0110111 0001011".split()
_EAN_G = "0100111 0110011 0011011 0100001 0011101 0111001 0000101 0010001 0001001 0010111".split()
_EAN_R = "1110010 1100110 1101100 1000010 1011100 1001110 1010000 1000100 1001000 1110100".split()
_EAN_PARITY = "LLLLLL LLGLGG LLGGLG LLGGGL LGLLGG LGGLLG LGGGLL LGLGLG LGLGGL LGGLGL".split()


def ean_check_digit(digits12):
    total = sum(int(d) * (3 if i % 2 else 1) for i, d in enumerate(digits12))
    return str((10 - total % 10) % 10)


def _widths_to_modules(widths):
    """'2122..' bar/space widths -> list of booleans (True = bar), one per module."""
    out, bar = [], True
    for w in widths:
        out += [bar] * int(w)
        bar = not bar
    return out


def _code128_values(data):
    """Symbol values for data, using subset C (two digits per symbol) for runs of digits."""
    n = len(data)

    def digit_run(at):
        j = at
        while j < n and data[j].isdigit():
            j += 1
        return j - at

    first = digit_run(0)
    subset = "C" if first >= 4 or (first == n and n >= 2) else "B"
    values = [_C128_START_C if subset == "C" else _C128_START_B]
    i = 0
    while i < n:
        run = digit_run(i)
        if subset == "C":
            if run >= 2:
                values.append(int(data[i:i + 2]))
                i += 2
                continue
            values.append(_C128_TO_B)
            subset = "B"
        if run >= 6 or (run >= 4 and i + run == n):
            if run % 2:  # odd run: one digit in B first
                values.append(ord(data[i]) - 32)
                i += 1
            values.append(_C128_TO_C)
            subset = "C"
            continue
        values.append(ord(data[i]) - 32)
        i += 1
    checksum = (values[0] + sum(pos * v for pos, v in enumerate(values[1:], 1))) % 103
    return values + [checksum, _C128_STOP]


def barcode_problem(symbology, data):
    """Why the data can't be printed as this barcode, or None if it's fine."""
    if not data:
        return "Barcode is empty."
    if symbology == "code128":
        if any(not 32 <= ord(c) <= 126 for c in data):
            return "Code 128 supports letters, digits and common symbols only."
    elif symbology == "code39":
        if any(c not in _C39 or c == "*" for c in data.upper()):
            return "Code 39 supports A-Z, 0-9, space and - . $ / + % only."
    elif symbology in ("ean13", "upca"):
        n = 12 if symbology == "ean13" else 11
        name = SYMBOLOGIES[symbology]
        if not data.isdigit() or len(data) not in (n, n + 1):
            return f"{name} needs {n} digits (or {n + 1} with the check digit)."
        full = data if symbology == "ean13" else "0" + data
        if len(data) == n + 1 and ean_check_digit(full[:12]) != full[12]:
            return f"{name} check digit is wrong; it should be {ean_check_digit(full[:12])}."
    return None


def barcode_modules(symbology, data):
    """The barcode as a list of modules (True = bar), quiet zones not included."""
    if symbology == "code128":
        return _widths_to_modules("".join(_C128[v] for v in _code128_values(data)))
    if symbology == "code39":
        out = []
        for c in "*" + data.upper() + "*":
            widths = "".join(str(_C39_WIDE) if e == "w" else "1" for e in _C39[c])
            out += _widths_to_modules(widths) + [False]  # gap between characters
        return out[:-1]
    # EAN-13 / UPC-A (UPC-A is EAN-13 with a leading 0)
    digits = data if symbology == "ean13" else "0" + data
    digits = digits[:12] + ean_check_digit(digits[:12])
    bits = "101"
    parity = _EAN_PARITY[int(digits[0])]
    for i, d in enumerate(digits[1:7]):
        bits += (_EAN_L if parity[i] == "L" else _EAN_G)[int(d)]
    bits += "01010"
    for d in digits[7:]:
        bits += _EAN_R[int(d)]
    bits += "101"
    return [b == "1" for b in bits]


def barcode_human_text(symbology, data):
    if symbology == "ean13":
        return data[:12] + ean_check_digit(data[:12])
    if symbology == "upca":
        return data[:11] + ean_check_digit(("0" + data)[:12])
    if symbology == "code39":
        return data.upper()
    return data


# --------------------------------------------------------------------------
# QR codes and pictures
# --------------------------------------------------------------------------

def qr_matrix(data):
    """QR modules as rows of booleans (True = dark), matching ZPL ^BQ error level M."""
    import segno
    qr = segno.make_qr(data, error="m", boost_error=False)
    return [[bool(v) for v in row] for row in qr.matrix]


def load_picture(path, max_side=800):
    """Read an image file and return it as base64 PNG, shrunk to a sensible size."""
    from PIL import Image
    with Image.open(path) as img:
        img.load()
        img = _flatten(img)
        img.thumbnail((max_side, max_side))
        buf = io.BytesIO()
        img.save(buf, "PNG")
    return base64.b64encode(buf.getvalue()).decode("ascii")


def _flatten(img):
    """Grayscale with transparent areas made white."""
    from PIL import Image
    img = img.convert("RGBA")
    white = Image.new("RGBA", img.size, (255, 255, 255, 255))
    return Image.alpha_composite(white, img).convert("L")


def picture_bitmap(image_png, width, height, dither=False):
    """The picture fitted into width x height dots, as a 1-bit PIL image (0 = black)."""
    from PIL import Image
    img = Image.open(io.BytesIO(base64.b64decode(image_png)))
    img = _flatten(img)
    scale = min(width / img.width, height / img.height)
    size = (max(1, int(img.width * scale)), max(1, int(img.height * scale)))
    img = img.resize(size, Image.LANCZOS)
    if dither:
        return img.convert("1")  # Floyd-Steinberg
    return img.point(lambda v: 255 if v >= 128 else 0).convert("1", dither=Image.NONE)


def bitmap_to_gfa(bitmap):
    """1-bit PIL image -> ZPL ^GFA graphic field (black pixels print)."""
    from PIL import ImageOps
    inverted = ImageOps.invert(bitmap.convert("L")).convert("1")  # ZPL: 1 = black
    row_bytes = (bitmap.width + 7) // 8
    data = inverted.tobytes().hex().upper()
    total = row_bytes * bitmap.height
    return f"^GFA,{total},{total},{row_bytes},{data}"


# --------------------------------------------------------------------------
# Layout
# --------------------------------------------------------------------------

def auto_arrange(design, label):
    """Stack the items top to bottom, sharing the label's height between them."""
    els = design.elements
    if not els:
        return
    m = label.margin
    avail_w, avail_h = label.width - 2 * m, label.height - 2 * m
    gap = round(label.dots(ITEM_GAP_IN))
    free = avail_h - gap * (len(els) - 1)

    fixed = {}
    for i, e in enumerate(els):
        if e.kind == "text" and e.size_pt:
            fixed[i] = pt_to_dots(e.size_pt, label)
    free -= sum(fixed.values())
    flex = [i for i in range(len(els)) if i not in fixed]
    total_weight = sum(_WEIGHTS[els[i].kind] for i in flex)
    unit = max(0.0, free / total_weight) if total_weight else 0.0

    # Auto-sized text lines all share one size, small enough for the widest line.
    auto_text = [i for i in flex if els[i].kind == "text"]
    body = min([unit] + [text_fit_height(els[i].value, avail_w) for i in auto_text if els[i].value.strip()])
    others = [i for i in flex if els[i].kind != "text"]
    if others:  # give room the text didn't need to the codes and pictures
        rest = free - body * len(auto_text)
        other_weight = sum(_WEIGHTS[els[i].kind] for i in others)
        unit = max(0.0, rest / other_weight)

    heights = []
    for i, e in enumerate(els):
        if i in fixed:
            h = fixed[i]
        elif e.kind == "text":
            h = body
        else:
            h = unit * _WEIGHTS[e.kind]
            h = max(h, label.dots(_MIN_IN[e.kind]))
            if e.kind in _MAX_IN:
                h = min(h, label.dots(_MAX_IN[e.kind]))
            if e.kind == "qr":
                h = min(h, avail_w)
        heights.append(max(1, int(h)))

    total = sum(heights) + gap * (len(els) - 1)
    y = m + max(0, (avail_h - total) // 2)
    for e, h in zip(els, heights):
        e.x, e.y, e.w, e.h = m, int(y), avail_w, h
        y += h + gap


# --------------------------------------------------------------------------
# Automatic fields: {date}, {julian+3}, {count} of {copies}... filled in at print time
# --------------------------------------------------------------------------

# name: (what it gives, example). Date fields take a day offset: {date+3}, {julian-1}.
FIELDS = {
    "date": ("Today's date", "10/21/2026"),
    "shortdate": ("Today's date, short year", "10/21/26"),
    "longdate": ("Today's date, written out", "Oct 21, 2026"),
    "julian": ("Day of the year (Julian date)", "294"),
    "year": ("Year", "2026"),
    "yy": ("Year, 2 digits", "26"),
    "month": ("Month number", "10"),
    "day": ("Day of the month", "21"),
    "weekday": ("Day of the week", "Wed"),
    "week": ("Week of the year", "43"),
    "time": ("Time printed", "4:42 PM"),
    "count": ("Which copy this is: 1, 2, 3...", "1"),
    "copies": ("How many copies are printing", "10"),
}
_NOT_DATES = {"time", "count", "copies"}
_FIELD = re.compile(r"\{([A-Za-z]+)([+-]\d{1,4})?\}")


def _field_value(name, offset, now, count, copies):
    if name == "count":
        return str(count)
    if name == "copies":
        return str(copies)
    if name == "time":
        return now.strftime("%I:%M %p").lstrip("0")
    d = now + timedelta(days=offset)
    return {
        "date": f"{d.month:02d}/{d.day:02d}/{d.year}",
        "shortdate": f"{d.month:02d}/{d.day:02d}/{d.year % 100:02d}",
        "longdate": f"{d:%b} {d.day}, {d.year}",
        "julian": f"{d.timetuple().tm_yday:03d}",
        "year": str(d.year),
        "yy": f"{d.year % 100:02d}",
        "month": f"{d.month:02d}",
        "day": f"{d.day:02d}",
        "weekday": f"{d:%a}",
        "week": f"{d.isocalendar()[1]:02d}",
    }[name]


def fill_fields(text, now, count=1, copies=1):
    """Replace the automatic fields in text. Anything in braces that isn't one is left alone."""
    def sub(m):
        name, offset = m.group(1).lower(), m.group(2)
        if name not in FIELDS or (offset and name in _NOT_DATES):
            return m.group(0)
        return _field_value(name, int(offset or 0), now, count, copies)
    return _FIELD.sub(sub, text)


def unknown_fields(design):
    """Things in braces that look like fields but aren't, e.g. {dat}."""
    found = []
    for e in design.elements:
        for m in _FIELD.finditer(e.value if e.kind != "image" else ""):
            name = m.group(1).lower()
            if (name not in FIELDS or (m.group(2) and name in _NOT_DATES)) and m.group(0) not in found:
                found.append(m.group(0))
    return found


def uses_count(design):
    """Whether each copy differs ({count}), so copies have to be sent one by one."""
    return any(e.kind != "image" and re.search(r"\{count\}", e.value, re.I) for e in design.elements)


def filled(design, now, count=1, copies=1):
    """A copy of the design with the automatic fields filled in."""
    out = copy.deepcopy(design)
    for e in out.elements:
        if e.kind != "image":
            e.value = fill_fields(e.value, now, count, copies)
    return out


# --------------------------------------------------------------------------
# Rendering: the concrete marks for each item, used by both preview and ZPL
# --------------------------------------------------------------------------

@dataclass
class Mark:
    kind: str           # text, bars, qr, image
    x: int
    y: int
    w: int
    h: int
    element: Element = None
    text: str = ""      # text (also a barcode's human-readable line)
    text_h: int = 0
    align: str = "center"
    module: int = 1     # bars/qr: dots per module
    pattern: list = None   # bars: modules; qr: matrix
    bitmap: object = None  # image: 1-bit PIL image


@dataclass
class Rendered:
    marks: list
    warnings: list
    errors: list   # problems that would print something wrong


def _place(box_x, box_w, content_w, align):
    if align == "left":
        return box_x
    if align == "right":
        return box_x + box_w - content_w
    return box_x + (box_w - content_w) // 2


def render(design, label):
    if design.auto_layout:
        auto_arrange(design, label)
    marks, warnings, errors = [], [], []

    for n, e in enumerate(design.elements, 1):
        if e.w <= 0 or e.h <= 0:
            continue
        if e.x < 0 or e.y < 0 or e.x + e.w > label.width or e.y + e.h > label.height:
            warnings.append(f"Item {n} goes past the edge of the label.")

        if e.kind == "text":
            if not e.value.strip():
                continue
            fit = text_fit_height(e.value, e.w)
            if e.size_pt:
                h = int(pt_to_dots(e.size_pt, label))
                if h > fit + 0.5:
                    warnings.append(f"Line {n} is probably too wide for its space.")
                if h > e.h + 0.5:
                    warnings.append(f"Line {n} is taller than its space.")
            else:
                h = int(min(e.h, fit))
            if h < label.dots(0.06):
                warnings.append(f"Line {n} is very small and may be hard to read.")
            h = max(1, h)
            marks.append(Mark("text", e.x, e.y + (e.h - h) // 2, e.w, h, e, e.value, h, e.align))

        elif e.kind == "barcode":
            problem = barcode_problem(e.symbology, e.value)
            if problem:
                errors.append(f"Item {n}: {problem}")
                continue
            modules = barcode_modules(e.symbology, e.value)
            module = min(10, e.w // len(modules))
            if module < 1:
                errors.append(f"Item {n}: the barcode is too long to fit; shorten it or make its space wider.")
                continue
            width = module * len(modules)
            human = barcode_human_text(e.symbology, e.value)
            text_h = int(min(label.dots(HUMAN_TEXT_IN), e.h * 0.3, text_fit_height(human, width))) \
                if e.show_text else 0
            bar_h = e.h - (text_h + 2 if text_h else 0)
            if bar_h < label.dots(0.15):
                warnings.append(f"Item {n}: the barcode is very short and may not scan.")
            x = _place(e.x, e.w, width, e.align)
            marks.append(Mark("bars", x, e.y, width, max(1, bar_h), e, module=module, pattern=modules))
            if text_h:
                marks.append(Mark("text", x, e.y + bar_h + 2, width, text_h, e, human, text_h, "center"))

        elif e.kind == "qr":
            if not e.value:
                errors.append(f"Item {n}: QR code is empty.")
                continue
            matrix = qr_matrix(e.value)
            module = min(10, min(e.w, e.h) // len(matrix))
            if module < 1:
                errors.append(f"Item {n}: the QR code holds too much to fit; shorten it or make its space bigger.")
                continue
            side = module * len(matrix)
            marks.append(Mark("qr", _place(e.x, e.w, side, e.align), e.y + (e.h - side) // 2,
                              side, side, e, module=module, pattern=matrix))

        elif e.kind == "image":
            if not e.image_png:
                continue
            bitmap = picture_bitmap(e.image_png, e.w, e.h, e.dither)
            marks.append(Mark("image", _place(e.x, e.w, bitmap.width, e.align), e.y + (e.h - bitmap.height) // 2,
                              bitmap.width, bitmap.height, e, bitmap=bitmap))
    return Rendered(marks, warnings, errors)


def tighten(design, rendered):
    """Shrink each item's box to what it actually shows.

    Used when switching from auto to manual layout, so items can be dragged
    around by what you see rather than by full-width rows. Nothing moves.
    """
    bounds = {}
    for mk in rendered.marks:
        x, w = mk.x, mk.w
        if mk.kind == "text":
            w = min(mk.w, int(text_em(mk.text) * mk.text_h * WIDTH_SAFETY) + 4)
            x = _place(mk.x, mk.w, w, mk.align)
        box = (x, mk.y, x + w, mk.y + mk.h)
        old = bounds.get(id(mk.element))
        bounds[id(mk.element)] = box if old is None else (
            min(old[0], box[0]), min(old[1], box[1]), max(old[2], box[2]), max(old[3], box[3]))
    for e in design.elements:
        if id(e) in bounds:
            x1, y1, x2, y2 = bounds[id(e)]
            e.x, e.y, e.w, e.h = x1, y1, x2 - x1, y2 - y1


# --------------------------------------------------------------------------
# ZPL
# --------------------------------------------------------------------------

def zpl_escape(s):
    # ^FH turns _XX into the byte 0xXX, which lets us print ^ ~ and _ safely.
    s = "".join(c for c in s if c >= " ")
    return s.replace("_", "_5F").replace("^", "_5E").replace("~", "_7E")


def build_zpl(rendered, shift_x=0, shift_y=0, copies=1, border=None):
    """The label format. It only places content: no printer settings (width,
    length, media, darkness...) are sent, because those stay in effect on the
    printer and would change how other software's labels print.

    border: (width, height) in dots to draw a box around the label (test labels).
    """
    def pos(x, y):
        return f"^FO{max(0, x + shift_x)},{max(0, y + shift_y)}"

    out = ["^XA"]
    if any(ord(c) > 126 for mk in rendered.marks for c in mk.text):
        out.append("^CI28")  # UTF-8, only when needed: it also stays in effect
    if border:
        out.append(f"{pos(0, 0)}^GB{border[0]},{border[1]},3^FS")
    for mk in rendered.marks:
        e = mk.element
        if mk.kind == "text":
            just = {"left": "L", "center": "C", "right": "R"}[mk.align]
            out.append(f"{pos(mk.x, mk.y)}^A0N,{mk.text_h},{mk.text_h}"
                       f"^FB{mk.w},1,0,{just},0^FH^FD{zpl_escape(mk.text)}^FS")
        elif mk.kind == "bars":
            data = zpl_escape(e.value)
            if e.symbology == "code128":
                code = f"^BY{mk.module}^BCN,{mk.h},N,N,N,A"
            elif e.symbology == "code39":
                code = f"^BY{mk.module},{_C39_WIDE}^B3N,N,{mk.h},N,N"
                data = zpl_escape(e.value.upper())
            elif e.symbology == "ean13":
                code = f"^BY{mk.module}^BEN,{mk.h},N,N"
                data = e.value[:12]  # the printer adds the check digit
            else:
                code = f"^BY{mk.module}^BUN,{mk.h},N,N,N"
                data = e.value[:11]
            out.append(f"{pos(mk.x, mk.y)}{code}^FH^FD{data}^FS")
        elif mk.kind == "qr":
            out.append(f"{pos(mk.x, mk.y)}^BQN,2,{mk.module}^FH^FDMA,{zpl_escape(e.value)}^FS")
        elif mk.kind == "image":
            out.append(f"{pos(mk.x, mk.y)}{bitmap_to_gfa(mk.bitmap)}^FS")
    out.append(f"^PQ{int(copies)}")
    out.append("^XZ")
    return "\n".join(out) + "\n"
