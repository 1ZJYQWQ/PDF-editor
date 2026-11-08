
import hashlib
import re
import zlib

import pdffont

WHITESPACE = b"\x00\t\n\x0c\r "
DELIMITERS = b"()<>[]{}/%,;"
SKIP_CHARS = b",;"

def tokenize(data):
    tokens = []
    i = 0
    n = len(data)
    while i < n:
        ch = data[i:i + 1]

        if ch in WHITESPACE:
            i += 1
            continue

        if ch in SKIP_CHARS:
            i += 1
            continue

        if ch == b"%":
            j = data.find(b"\n", i)
            i = n if j < 0 else j + 1
            continue

        if ch in b"[]{}":
            tokens.append(ch)
            i += 1
            continue

        if ch == b"/":
            j = i + 1
            while j < n and data[j:j + 1] not in WHITESPACE and \
                    data[j:j + 1] not in DELIMITERS:
                j += 1
            tokens.append(data[i:j])
            i = j
            continue

        if ch == b"(":
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                c = data[j:j + 1]
                if c == b"\\":
                    j += 2
                    continue
                if c == b"(":
                    depth += 1
                elif c == b")":
                    depth -= 1
                j += 1
            tokens.append(data[i:j])
            i = j
            continue

        if ch == b"<" and data[i:i + 2] != b"<<":
            j = data.find(b">", i)
            j = n if j < 0 else j + 1
            tokens.append(data[i:j])
            i = j
            continue

        if data[i:i + 2] == b"<<":
            tokens.append(b"<<")
            i += 2
            continue
        if data[i:i + 2] == b">>":
            tokens.append(b">>")
            i += 2
            continue

        j = i
        while j < n and data[j:j + 1] not in WHITESPACE and \
                data[j:j + 1] not in DELIMITERS:
            j += 1
        if j == i:
            j = i + 1
        tokens.append(data[i:j])
        i = j
    return tokens

def parse_object(tokens, pos):
    if pos >= len(tokens):
        return None, pos

    tok = tokens[pos]

    if tok == b"<<":
        pos += 1
        out = {}
        while pos < len(tokens) and tokens[pos] != b">>":
            key = tokens[pos]
            if not key.startswith(b"/"):
                pos += 1
                continue
            key = decode_name(key)
            val, pos = parse_object(tokens, pos + 1)
            out[key] = val
        return out, pos + 1

    if tok == b"[":
        pos += 1
        out = []
        while pos < len(tokens) and tokens[pos] != b"]":
            val, pos = parse_object(tokens, pos)
            out.append(val)
        return out, pos + 1

    if tok.startswith(b"/"):
        return Name(decode_name(tok)), pos + 1

    if tok.startswith(b"(") or tok.startswith(b"<"):
        return tok, pos + 1

    if tok == b"true":
        return True, pos + 1
    if tok == b"false":
        return False, pos + 1
    if tok == b"null":
        return None, pos + 1

    if _is_number(tok) and pos + 2 < len(tokens):
        if _is_int(tok) and _is_int(tokens[pos + 1]) and tokens[pos + 2] == b"R":
            return Ref(int(tok), int(tokens[pos + 1])), pos + 3

    if _is_number(tok):
        return _to_number(tok), pos + 1

    return tok, pos + 1

def _is_number(b):
    try:
        float(b)
        return True
    except (ValueError, TypeError):
        return False

def _is_int(b):
    return bool(re.fullmatch(rb"[+-]?\d+", b or b""))

def _to_number(b):
    return int(b) if _is_int(b) else float(b)

class Name(str):
    __slots__ = ()

class Ref(tuple):
    __slots__ = ()

    def __new__(cls, num, gen):
        return super().__new__(cls, (num, gen))

    @property
    def num(self):
        return self[0]

    @property
    def gen(self):
        return self[1]

def decode_name(raw):
    if not raw.startswith(b"/"):
        return raw.decode("latin-1")
    body = raw[1:]
    out = bytearray()
    i = 0
    while i < len(body):
        if body[i:i + 1] == b"#" and i + 2 < len(body):
            try:
                out.append(int(body[i + 1:i + 3], 16))
                i += 3
                continue
            except ValueError:
                pass
        out.append(body[i])
        i += 1
    return bytes(out).decode("latin-1")

def encode_name(s):
    out = bytearray(b"/")
    for byte in s.encode("latin-1", "replace"):
        if byte < 0x21 or byte > 0x7E or byte in b"()<>[]{}/%#":
            out += b"#%02X" % byte
        else:
            out.append(byte)
    return bytes(out)

OBJ_RE = re.compile(rb"(\d+)\s+(\d+)\s+obj\b")

class PDFDocument:

    def __init__(self):
        self.objects = {}
        self.trailer = {}
        self.root_ref = None
        self.pages_ref = None
        self.max_obj = 0

    @staticmethod
    def is_stream(val):
        return (isinstance(val, tuple) and len(val) == 2
                and isinstance(val[0], dict) and isinstance(val[1], bytes))

    def stream_data(self, ref):
        val = self.objects.get(tuple(ref)) if isinstance(ref, Ref) else None
        return val[1] if self.is_stream(val) else None

    @classmethod
    def load(cls, data):
        doc = cls()
        doc._parse_objects(data)
        doc._expand_objstm()
        doc._find_root(data)
        return doc

    def _expand_objstm(self):
        for val in list(self.objects.values()):
            if not self.is_stream(val):
                continue
            dic, data = val
            if dic.get("Type") != "ObjStm":
                continue
            n = dic.get("N")
            first = dic.get("First")
            if not (isinstance(n, int) and isinstance(first, int)):
                continue
            if n <= 0 or first <= 0 or first > len(data):
                continue
            head = data[:first].split()
            pairs = []
            for i in range(0, min(len(head) - 1, n * 2), 2):
                try:
                    pairs.append((int(head[i]), int(head[i + 1])))
                except ValueError:
                    pairs = []
                    break
            for i, (onum, off) in enumerate(pairs):
                start = first + off
                if i + 1 < len(pairs):
                    end = first + pairs[i + 1][1]
                else:
                    end = len(data)
                if not (0 <= start < end <= len(data)):
                    continue
                try:
                    obj, _ = parse_object(tokenize(data[start:end]), 0)
                except Exception:
                    continue
                if (onum, 0) in self.objects:
                    continue
                self.objects[(onum, 0)] = obj
                self.max_obj = max(self.max_obj, onum)

    def _parse_objects(self, data):
        for m in OBJ_RE.finditer(data):
            num = int(m.group(1))
            gen = int(m.group(2))
            body_start = m.end()
            end = data.find(b"endobj", body_start)
            if end < 0:
                end = len(data)
            body = data[body_start:end]

            sm = re.search(rb"\bstream\r?\n?", body)
            head = body[:sm.start()] if sm else body
            tokens = tokenize(head)
            value, _ = parse_object(tokens, 0)

            if sm:
                sdata = self._read_stream(body, sm, value, data, body_start)
                if isinstance(value, dict):
                    value = dict(value)
                    value.pop("Filter", None)
                    value.pop("DecodeParms", None)
                    value["Length"] = len(sdata)
                    self.objects[(num, gen)] = (value, sdata)
                    self.max_obj = max(self.max_obj, num)
                    continue

            self.objects[(num, gen)] = value
            self.max_obj = max(self.max_obj, num)

    @staticmethod
    def _read_stream(body, sm, value, full_data, body_start):
        raw_start = sm.end()
        if body[raw_start:raw_start + 1] == b"\r":
            raw_start += 1
        if body[raw_start:raw_start + 1] == b"\n":
            raw_start += 1

        length = None
        if isinstance(value, dict):
            length = value.get("Length")
            if isinstance(length, Ref):
                length = None

        if isinstance(length, int) and length >= 0:
            raw = body[raw_start:raw_start + length]
        else:
            e = body.find(b"endstream", raw_start)
            raw = body[raw_start:e] if e >= 0 else body[raw_start:]
            if raw.endswith(b"\r\n"):
                raw = raw[:-2]
            elif raw.endswith(b"\n"):
                raw = raw[:-1]

        filters = value.get("Filter") if isinstance(value, dict) else None
        fname = None
        if isinstance(filters, Name):
            fname = filters
        elif isinstance(filters, list) and filters:
            first = filters[0]
            fname = first if isinstance(first, Name) else None

        if fname in ("FlateDecode", "Fl"):
            try:
                return zlib.decompress(raw)
            except zlib.error:
                try:
                    return zlib.decompressobj().decompress(raw)
                except zlib.error:
                    return raw
        return raw

    def _find_root(self, data):
        idx = data.rfind(b"trailer")
        while idx >= 0:
            end = data.find(b"startxref", idx)
            chunk = data[idx:end if end > 0 else idx + 4096]
            tm = re.search(rb"trailer\s*", chunk)
            if tm:
                tokens = tokenize(chunk[tm.end():])
                obj, _ = parse_object(tokens, 0)
                if isinstance(obj, dict):
                    self.trailer.update(obj)
                    if "Root" in obj:
                        break
            idx = data.rfind(b"trailer", 0, idx)

        if not isinstance(self.trailer.get("Root"), Ref):
            last = None
            for val in self.objects.values():
                if self.is_stream(val):
                    dic = val[0]
                    if dic.get("Type") == "XRef" and isinstance(dic.get("Root"), Ref):
                        last = dic
            if last is not None:
                for k in ("Root", "Info", "ID", "Size"):
                    if k in last:
                        self.trailer.setdefault(k, last[k])
                if not isinstance(self.trailer.get("Root"), Ref):
                    self.trailer["Root"] = last["Root"]

        root = self.trailer.get("Root")
        if isinstance(root, Ref):
            self.root_ref = root
        else:
            self.root_ref = self._find_catalog()

        if self.root_ref and self.root_ref in self.objects:
            cat = self.objects[self.root_ref]
            if isinstance(cat, dict) and isinstance(cat.get("Pages"), Ref):
                self.pages_ref = cat["Pages"]
            else:
                self.pages_ref = self._find_pages()
        else:
            self.pages_ref = self._find_pages()

    def _find_catalog(self):
        for (num, gen), val in self.objects.items():
            if isinstance(val, dict) and val.get("Type") == "Catalog":
                return Ref(num, gen)
        return None

    def _find_pages(self):
        for (num, gen), val in self.objects.items():
            if isinstance(val, dict) and val.get("Type") == "Pages" \
                    and "Parent" not in val:
                return Ref(num, gen)
        for (num, gen), val in self.objects.items():
            if isinstance(val, dict) and val.get("Type") == "Pages":
                return Ref(num, gen)
        return None

    def get(self, ref):
        if isinstance(ref, Ref):
            return self.objects.get(tuple(ref))
        return ref

    def resolve(self, ref):
        seen = 0
        while isinstance(ref, Ref) and seen < 32:
            ref = self.objects.get(tuple(ref))
            seen += 1
        return ref

    def new_obj(self, value):
        self.max_obj += 1
        self.objects[(self.max_obj, 0)] = value
        return Ref(self.max_obj, 0)

    def page_refs(self):
        out = []
        self._walk_pages(self.pages_ref, out, set())
        return out

    def _walk_pages(self, ref, out, visited):
        key = tuple(ref) if isinstance(ref, Ref) else None
        if key and key in visited:
            return
        if key:
            visited.add(key)

        node = self.resolve(ref)
        if not isinstance(node, dict):
            return
        ntype = node.get("Type")
        if ntype == "Page":
            out.append(ref if isinstance(ref, Ref) else None)
            return
        kids = self.resolve(node.get("Kids"))
        if isinstance(kids, list):
            for kid in kids:
                if isinstance(kid, Ref):
                    self._walk_pages(kid, out, visited)

    def page_dict(self, ref):
        return self.resolve(ref)

    def inherited(self, ref, key, default=None):
        seen = 0
        node = self.resolve(ref)
        while isinstance(node, dict) and seen < 32:
            if key in node:
                return node[key]
            parent = node.get("Parent")
            if not parent:
                break
            node = self.resolve(parent)
            seen += 1
        return default

def serialize_value(val, out):
    if val is None:
        out += b"null"
    elif val is True:
        out += b"true"
    elif val is False:
        out += b"false"
    elif isinstance(val, Name):
        out += encode_name(str(val))
    elif isinstance(val, Ref):
        out += b"%d %d R" % (val.num, val.gen)
    elif isinstance(val, (int, float)):
        if isinstance(val, float):
            if val == int(val) and abs(val) < 1e15:
                out += b"%d" % int(val)
            else:
                out += (b"%.6f" % val).rstrip(b"0").rstrip(b".")
        else:
            out += b"%d" % val
    elif isinstance(val, bytes):
        if val.startswith(b"(") or val.startswith(b"<"):
            out += val
        else:
            out += _escape_literal(val)
    elif isinstance(val, str):
        out += _escape_literal(val.encode("latin-1", "replace"))
    elif isinstance(val, list):
        out += b"["
        for i, item in enumerate(val):
            if i:
                out += b" "
            serialize_value(item, out)
        out += b"]"
    elif isinstance(val, dict):
        out += b"<<"
        for k, v in val.items():
            out += encode_name(k) + b" "
            serialize_value(v, out)
            out += b" "
        out += b">>"
    else:
        out += b"null"

def _escape_literal(raw):
    out = bytearray(b"(")
    for byte in raw:
        if byte in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([byte])
        elif byte == 0x0A:
            out += b"\\n"
        elif byte == 0x0D:
            out += b"\\r"
        elif byte == 0x09:
            out += b"\\t"
        elif byte < 0x20 or byte > 0x7E:
            out += b"\\%03o" % byte
        else:
            out.append(byte)
    out += b")"
    return bytes(out)

def build_pdf(doc, extra_streams, out_path):
    out = bytearray()
    out += b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n"

    for (pnum, pgen), content in extra_streams.items():
        if not content:
            continue
        sdict = {
            "Length": len(content),
        }
        stream_ref = doc.new_obj((sdict, content))

        page = doc.objects.get((pnum, pgen))
        if not isinstance(page, dict):
            continue
        old = page.get("Contents")
        if old is None:
            page["Contents"] = stream_ref
        elif isinstance(old, list):
            page["Contents"] = list(old) + [stream_ref]
        else:
            page["Contents"] = [old, stream_ref]

    offsets = {}
    for (num, gen), val in sorted(doc.objects.items()):
        offsets[(num, gen)] = len(out)
        out += b"%d %d obj\n" % (num, gen)
        if isinstance(val, tuple) and len(val) == 2 and isinstance(val[0], dict):
            sdict, sdata = val
            serialize_value(sdict, out)
            out += b"\nstream\n"
            out += sdata
            out += b"\nendstream"
        else:
            serialize_value(val, out)
        out += b"\nendobj\n"

    xref_pos = len(out)
    count = doc.max_obj + 1
    out += b"xref\n"
    out += b"0 %d\n" % count
    out += b"0000000000 65535 f \n"
    for i in range(1, count):
        off = offsets.get((i, 0))
        if off is None:
            out += b"0000000000 65535 f \n"
        else:
            out += b"%010d 00000 n \n" % off

    trailer = {
        "Size": count,
        "Root": doc.root_ref,
    }
    if isinstance(doc.trailer.get("Info"), Ref):
        trailer["Info"] = doc.trailer["Info"]
    out += b"trailer\n"
    serialize_value(trailer, out)
    out += b"\nstartxref\n%d\n%%%%EOF\n" % xref_pos

    with open(out_path, "wb") as fp:
        fp.write(out)
    return len(out)

class ContentBuilder:

    CID_ALIAS = "CJK"

    def __init__(self):
        self.ops = []
        self.fonts = {}
        self.cid_ref = None
        self.cid_subset = None
        self.cid_used = False
        self.missing_chars = set()

    def set_cid_font(self, font_ref, subset):
        self.cid_ref = font_ref
        self.cid_subset = subset

    def _font_alias(self, base_font):
        for alias, val in self.fonts.items():
            if isinstance(val, dict) and str(val.get("BaseFont")) == base_font:
                return alias
        n = sum(1 for v in self.fonts.values() if isinstance(v, dict)) + 1
        alias = "F%d" % n
        self.fonts[alias] = {
            "Type": Name("Font"),
            "Subtype": Name("Type1"),
            "BaseFont": Name(base_font),
            "Encoding": Name("WinAnsiEncoding"),
        }
        return alias

    def rect(self, x, y, w, h, rgb, fill=True, stroke=False, line_width=1.0):
        r, g, b = rgb
        if fill:
            self.ops.append(f"{r:.4f} {g:.4f} {b:.4f} rg")
        if stroke:
            self.ops.append(f"{r:.4f} {g:.4f} {b:.4f} RG")
            self.ops.append(f"{line_width:.4f} w")
        self.ops.append(f"{x:.3f} {y:.3f} {w:.3f} {h:.3f} re")
        if fill and stroke:
            self.ops.append("B")
        elif fill:
            self.ops.append("f")
        else:
            self.ops.append("S")

    def ellipse(self, x, y, w, h, rgb, fill=True, stroke=False, line_width=1.0):
        k = 0.5523
        rx, ry = w / 2.0, h / 2.0
        cx, cy = x + rx, y + ry
        r, g, b = rgb
        if fill:
            self.ops.append(f"{r:.4f} {g:.4f} {b:.4f} rg")
        if stroke:
            self.ops.append(f"{r:.4f} {g:.4f} {b:.4f} RG")
            self.ops.append(f"{line_width:.4f} w")
        p = []
        p.append(f"{cx + rx:.3f} {cy:.3f} m")
        p.append(f"{cx + rx:.3f} {cy + ry * k:.3f} {cx + rx * k:.3f} "
                 f"{cy + ry:.3f} {cx:.3f} {cy + ry:.3f} c")
        p.append(f"{cx - rx * k:.3f} {cy + ry:.3f} {cx - rx:.3f} "
                 f"{cy + ry * k:.3f} {cx - rx:.3f} {cy:.3f} c")
        p.append(f"{cx - rx:.3f} {cy - ry * k:.3f} {cx - rx * k:.3f} "
                 f"{cy - ry:.3f} {cx:.3f} {cy - ry:.3f} c")
        p.append(f"{cx + rx * k:.3f} {cy - ry:.3f} {cx + rx:.3f} "
                 f"{cy - ry * k:.3f} {cx + rx:.3f} {cy:.3f} c")
        p.append("h")
        self.ops.extend(p)
        if fill and stroke:
            self.ops.append("B")
        elif fill:
            self.ops.append("f")
        else:
            self.ops.append("S")

    def line(self, x1, y1, x2, y2, rgb, line_width=1.0):
        r, g, b = rgb
        self.ops.append(
            f"{r:.4f} {g:.4f} {b:.4f} RG {line_width:.4f} w "
            f"{x1:.3f} {y1:.3f} m {x2:.3f} {y2:.3f} l S"
        )

    def text(self, x, y, s, size, rgb, base_font="Helvetica"):
        if s and self.cid_subset is not None and has_non_latin(s):
            self._text_cid(x, y, s, size, rgb)
            return

        alias = self._font_alias(base_font)
        r, g, b = rgb
        leading = line_leading(size)
        self.ops.append("BT")
        self.ops.append(f"{r:.4f} {g:.4f} {b:.4f} rg")
        self.ops.append(f"/{alias} {size:.3f} Tf")
        for i, ln in enumerate(split_lines(s)):
            self.ops.append(f"{x:.3f} {y:.3f} Td" if i == 0
                            else f"0 -{leading:.3f} Td")
            self.ops.append(f"({_pdf_text_encode(ln, base_font)}) Tj")
        self.ops.append("ET")

    def _text_cid(self, x, y, s, size, rgb):
        encs = []
        for ln in split_lines(s):
            enc, missing = self.cid_subset.encode(ln)
            if missing:
                self.missing_chars.update(missing)
            encs.append(enc)
        if not any(encs):
            return
        self.cid_used = True
        r, g, b = rgb
        leading = line_leading(size)
        self.ops.append("BT")
        self.ops.append(f"{r:.4f} {g:.4f} {b:.4f} rg")
        self.ops.append(f"/{self.CID_ALIAS} {size:.3f} Tf")
        for i, enc in enumerate(encs):
            self.ops.append(f"{x:.3f} {y:.3f} Td" if i == 0
                            else f"0 -{leading:.3f} Td")
            if enc:
                self.ops.append("<%s> Tj" % enc.hex().upper())
        self.ops.append("ET")

    def measure_text(self, s, size, base_font="Helvetica"):
        widths = STANDARD_WIDTHS.get(base_font, STANDARD_WIDTHS["Helvetica"])
        total = 0.0
        for ch in s:
            total += widths.get(ch, 0.556)
        return total * size

    def to_bytes(self):
        return ("\n".join(self.ops) + "\n").encode("latin-1", "replace")

    def font_resources(self):
        out = dict(self.fonts)
        if self.cid_used and self.cid_ref is not None:
            out[self.CID_ALIAS] = self.cid_ref
        return out or None

STANDARD_WIDTHS = {
    "Helvetica": {
        " ": 278, "!": 278, '"': 355, "#": 556, "$": 556, "%": 889, "&": 667,
        "'": 191, "(": 333, ")": 333, "*": 389, "+": 584, ",": 278, "-": 333,
        ".": 278, "/": 278, "0": 556, "1": 556, "2": 556, "3": 556, "4": 556,
        "5": 556, "6": 556, "7": 556, "8": 556, "9": 556, ":": 278, ";": 278,
        "<": 584, "=": 584, ">": 584, "?": 556, "@": 1015,
        "A": 667, "B": 667, "C": 722, "D": 722, "E": 667, "F": 611, "G": 778,
        "H": 722, "I": 278, "J": 500, "K": 667, "L": 556, "M": 833, "N": 722,
        "O": 778, "P": 667, "Q": 778, "R": 722, "S": 667, "T": 611, "U": 722,
        "V": 667, "W": 944, "X": 667, "Y": 667, "Z": 611,
        "[": 278, "\\": 278, "]": 278, "^": 469, "_": 556, "`": 333,
        "a": 556, "b": 556, "c": 500, "d": 556, "e": 556, "f": 278, "g": 556,
        "h": 556, "i": 222, "j": 222, "k": 500, "l": 222, "m": 833, "n": 556,
        "o": 556, "p": 556, "q": 556, "r": 333, "s": 500, "t": 278, "u": 556,
        "v": 500, "w": 722, "x": 500, "y": 500, "z": 500,
        "{": 334, "|": 260, "}": 334, "~": 584,
    },
}
STANDARD_WIDTHS["Helvetica-Bold"] = STANDARD_WIDTHS["Helvetica"]
STANDARD_WIDTHS["Helvetica-Oblique"] = STANDARD_WIDTHS["Helvetica"]
STANDARD_WIDTHS["Times-Roman"] = STANDARD_WIDTHS["Helvetica"]
STANDARD_WIDTHS["Courier"] = {}

def split_lines(s):
    return s.replace("\r\n", "\n").replace("\r", "\n").split("\n")

def line_leading(size):
    return (size or 12.0) * 1.25

def _pdf_text_encode(s, base_font="Helvetica"):
    out = []
    for ch in s:
        code = ord(ch)
        if code > 255:
            out.append("?")
            continue
        if ch in ("(", ")", "\\"):
            out.append("\\" + ch)
        elif ch == "\n":
            out.append("\\n")
        elif ch == "\r":
            out.append("\\r")
        elif ch == "\t":
            out.append("\\t")
        elif code < 32:
            out.append("\\%03o" % code)
        else:
            out.append(ch)
    return "".join(out)

def has_non_latin(s):
    return any(ord(ch) > 255 for ch in s)

def reorder_pages(doc, order):
    refs = doc.page_refs()
    kids = [refs[i] for i in order if 0 <= i < len(refs)]
    pages_node = doc.resolve(doc.pages_ref)
    if isinstance(pages_node, dict):
        pages_node["Kids"] = kids
        pages_node["Count"] = len(kids)
    for ref in kids:
        p = doc.resolve(ref)
        if isinstance(p, dict):
            p["Parent"] = doc.pages_ref
    return len(kids)

def rotate_page(doc, index, delta):
    refs = doc.page_refs()
    if not (0 <= index < len(refs)):
        return False
    page = doc.resolve(refs[index])
    if not isinstance(page, dict):
        return False
    cur = doc.inherited(refs[index], "Rotate", 0) or 0
    try:
        cur = int(cur)
    except (TypeError, ValueError):
        cur = 0
    page["Rotate"] = (cur + delta) % 360
    return True

def insert_blank_page(doc, index, width, height):
    refs = doc.page_refs()
    if refs:
        mb = doc.inherited(refs[0], "MediaBox")
        if isinstance(mb, list) and len(mb) == 4:
            width, height = _num(mb[2]) - _num(mb[0]), _num(mb[3]) - _num(mb[1])

    content_ref = doc.new_obj(({"Length": 0}, b""))

    page = {
        "Type": Name("Page"),
        "Parent": doc.pages_ref,
        "MediaBox": [0, 0, float(width), float(height)],
        "Resources": {},
        "Contents": content_ref,
    }
    ref = doc.new_obj(page)

    pages_node = doc.resolve(doc.pages_ref)
    if not isinstance(pages_node, dict):
        return None
    kids = pages_node.get("Kids")
    if not isinstance(kids, list):
        kids = []
    index = max(0, min(index, len(kids)))
    kids.insert(index, ref)
    pages_node["Kids"] = kids
    pages_node["Count"] = len(kids)
    return ref

def delete_pages(doc, indices):
    refs = doc.page_refs()
    drop = {i for i in indices if 0 <= i < len(refs)}
    keep = [r for i, r in enumerate(refs) if i not in drop]
    pages_node = doc.resolve(doc.pages_ref)
    if isinstance(pages_node, dict):
        pages_node["Kids"] = keep
        pages_node["Count"] = len(keep)
    return len(keep)

def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def _to_rgb(color, default=(0.0, 0.0, 0.0)):
    if isinstance(color, (list, tuple)) and len(color) >= 3:
        vals = []
        for c in color[:3]:
            c = _num(c, 0.0)
            vals.append(c / 255.0 if c > 1.0 else c)
        return tuple(vals)
    if not isinstance(color, str):
        return default
    s = color.strip().lstrip("#")
    if len(s) in (3, 6) and re.fullmatch(r"[0-9a-fA-F]+", s):
        if len(s) == 3:
            s = "".join(ch * 2 for ch in s)
        return (int(s[0:2], 16) / 255.0,
                int(s[2:4], 16) / 255.0,
                int(s[4:6], 16) / 255.0)
    return default

def _width_array(widths):
    if not widths:
        return []
    out = []
    gids = sorted(widths)
    n = len(gids)
    i = 0
    while i < n:
        j = i
        w = widths[gids[i]]
        while (j + 1 < n and gids[j + 1] == gids[j] + 1
               and widths[gids[j + 1]] == w):
            j += 1
        if j - i >= 2:
            out += [gids[i], gids[j], w]
        else:
            for k in range(i, j + 1):
                out += [gids[k], [widths[gids[k]]]]
        i = j + 1
    return out

def _utf16be_hex(ch):
    return ch.encode("utf-16-be").hex().upper()

def _unicode_pref(u):
    if 0x20 <= u <= 0x7E:
        return 0
    if 0x4E00 <= u <= 0x9FFF:
        return 1
    if 0x3000 <= u <= 0x30FF or 0xFF00 <= u <= 0xFFEF:
        return 1
    if 0x3400 <= u <= 0x4DBF or 0x20000 <= u <= 0x3FFFF:
        return 2
    if 0xF900 <= u <= 0xFAFF:
        return 8
    if 0x2E80 <= u <= 0x2FDF:
        return 9
    return 5

def to_unicode_cmap(gid_of):
    best = {}
    for u, g in gid_of.items():
        prev = best.get(g)
        if prev is None or (_unicode_pref(u), u) < (_unicode_pref(prev), prev):
            best[g] = u
    pairs = sorted(best.items())

    lines = [
        "/CIDInit /ProcSet findresource begin",
        "12 dict begin",
        "begincmap",
        "/CIDSystemInfo << /Registry (Adobe) /Ordering (UCS)"
        " /Supplement 0 >> def",
        "/CMapName /Adobe-Identity-UCS def",
        "/CMapType 2 def",
        "1 begincodespacerange",
        "<0000> <FFFF>",
        "endcodespacerange",
    ]
    for i in range(0, len(pairs), 100):
        chunk = pairs[i:i + 100]
        lines.append("%d beginbfchar" % len(chunk))
        for g, u in chunk:
            lines.append("<%04X> <%s>" % (g, _utf16be_hex(chr(u))))
        lines.append("endbfchar")
    lines += [
        "endcmap",
        "CMapName currentdict /CMap defineresource pop",
        "end",
        "end",
    ]
    return "\n".join(lines).encode("ascii")

def embed_cid_font(doc, subset, ps_name="CJK-Subset", unicode_map=None):
    upem = subset.upem or 1000
    scale = 1000.0 / upem

    def s(v):
        return int(round(v * scale))

    font_data = subset.data
    fontfile = doc.new_obj((
        {"Length": len(font_data), "Length1": len(font_data)},
        font_data,
    ))

    bbox = [s(v) for v in (subset.bbox or (0, -200, 1000, 800))]
    ascent = s(subset.ascent) or 880
    descent = s(subset.descent) or -120

    descriptor = doc.new_obj({
        "Type": Name("FontDescriptor"),
        "FontName": Name(ps_name),
        "Flags": 4,
        "FontBBox": bbox,
        "ItalicAngle": 0,
        "Ascent": ascent,
        "Descent": descent,
        "CapHeight": ascent,
        "StemV": 80,
        "FontFile2": fontfile,
    })

    cid_font = doc.new_obj({
        "Type": Name("Font"),
        "Subtype": Name("CIDFontType2"),
        "BaseFont": Name(ps_name),
        "CIDSystemInfo": {"Registry": "Adobe",
                          "Ordering": "Identity",
                          "Supplement": 0},
        "FontDescriptor": descriptor,
        "DW": 1000,
        "W": _width_array(subset.widths),
        "CIDToGIDMap": Name("Identity"),
    })

    cmap_data = to_unicode_cmap(
        unicode_map if unicode_map is not None else subset.gid_of)
    tounicode = doc.new_obj(({"Length": len(cmap_data)}, cmap_data))

    return doc.new_obj({
        "Type": Name("Font"),
        "Subtype": Name("Type0"),
        "BaseFont": Name(ps_name),
        "Encoding": Name("Identity-H"),
        "DescendantFonts": [cid_font],
        "ToUnicode": tounicode,
    })

def build_cjk_font(doc, chars, base_name="NotoSansSC"):
    font = pdffont.load_default()
    subset = font.subset(chars)

    tag = hashlib.sha1(
        (",".join(str(c) for c in sorted(chars))).encode("ascii")
    ).hexdigest()[:6].upper()
    ps_name = "%s+%s-Subset" % (tag, base_name)

    used = {}
    for c in chars:
        g = subset.gid_of.get(c)
        if g is not None:
            used[c] = g
    ref = embed_cid_font(doc, subset, ps_name=ps_name, unicode_map=used)
    return ref, subset

def apply_edits(src_path, out_path, edits):
    with open(src_path, "rb") as fp:
        data = fp.read()

    doc = PDFDocument.load(data)
    if not doc.pages_ref:
        raise ValueError("无法定位页面树，文件可能不是标准 PDF")

    pages_cfg = edits.get("pages") or {}

    dels = pages_cfg.get("delete") or []
    if dels:
        delete_pages(doc, [int(i) for i in dels])
        refs_after = doc.page_refs()
        old_refs = []
        doc2_pages = doc.resolve(doc.pages_ref)
        remap = {}
        kept = 0
        original_order = _original_indices(len(doc.page_refs()) + len(dels), dels)
        for old_i in original_order:
            remap[old_i] = kept
            kept += 1
    else:
        remap = None
        refs_after = doc.page_refs()

    order = pages_cfg.get("order")
    if order:
        order = [int(i) for i in order]
        if remap:
            order = [remap.get(i, i) for i in order]
        reorder_pages(doc, order)

    rots = pages_cfg.get("rotate") or {}
    if rots:
        for k, v in rots.items():
            idx = int(k)
            if remap:
                idx = remap.get(idx, idx)
            try:
                rotate_page(doc, idx, int(v))
            except (TypeError, ValueError):
                continue

    for spec in pages_cfg.get("insert") or []:
        try:
            at = int(spec.get("at", 0))
            w = _num(spec.get("width"), 595)
            h = _num(spec.get("height"), 842)
            insert_blank_page(doc, at, w, h)
        except (TypeError, ValueError):
            continue

    refs = doc.page_refs()
    per_page = {}
    for obj in edits.get("objects") or []:
        try:
            pno = int(obj.get("page", 1))
        except (TypeError, ValueError):
            continue
        if not (1 <= pno <= len(refs)):
            continue
        per_page.setdefault(pno - 1, []).append(obj)

    all_text = "".join(
        str(o.get("text") or "")
        for o in (edits.get("objects") or [])
        if (o.get("kind") or "").lower() == "text"
    )
    cjk_chars = {ord(ch) for ch in all_text}
    cjk_ref = None
    cjk_subset = None
    if has_non_latin(all_text):
        cjk_ref, cjk_subset = build_cjk_font(doc, cjk_chars)

    extra_streams = {}
    missing_cjk = set()
    for pidx, objs in per_page.items():
        builder = ContentBuilder()
        if cjk_subset is not None:
            builder.set_cid_font(cjk_ref, cjk_subset)
        for obj in objs:
            _emit_object(builder, obj)
        content = builder.to_bytes()
        if not content.strip():
            continue
        pref = refs[pidx]
        extra_streams[tuple(pref)] = content
        fr = builder.font_resources()
        if fr:
            _attach_font_resource(doc, refs[pidx], fr)
        missing_cjk |= builder.missing_chars

    annot_stat = {"written": 0, "pages": 0, "skipped": 0}
    anns = edits.get("annotations") or []
    if anns:
        from pdfannot import apply_annotations
        annot_stat = apply_annotations(doc, refs, anns)

    size = build_pdf(doc, extra_streams, out_path)
    return {
        "ok": True,
        "size": size,
        "pages": len(doc.page_refs()),
        "objects": sum(len(v) for v in per_page.values()),
        "annotations": annot_stat,
        "cjk_glyphs": cjk_subset.num_glyphs if cjk_subset else 0,
        "cjk_chars": len(cjk_chars) if cjk_subset else 0,
        "cjk_missing": "".join(sorted(missing_cjk)),
    }

def _original_indices(total, deleted):
    return [i for i in range(total) if i not in set(deleted)]

def _emit_object(b, obj):
    kind = (obj.get("kind") or "").lower()
    x = _num(obj.get("x"))
    y = _num(obj.get("y"))
    w = _num(obj.get("w"))
    h = _num(obj.get("h"))

    if kind == "whiteout":
        b.rect(x, y, w, h, _to_rgb(obj.get("color"), (1.0, 1.0, 1.0)),
               fill=True, stroke=False)

    elif kind == "rect":
        b.rect(x, y, w, h, _to_rgb(obj.get("color"), (0.0, 0.0, 0.0)),
               fill=bool(obj.get("fill")),
               stroke=bool(obj.get("stroke", True)),
               line_width=_num(obj.get("lineWidth"), 1.0))

    elif kind == "ellipse":
        b.ellipse(x, y, w, h, _to_rgb(obj.get("color"), (0.0, 0.0, 0.0)),
                  fill=bool(obj.get("fill")),
                  stroke=bool(obj.get("stroke", True)),
                  line_width=_num(obj.get("lineWidth"), 1.0))

    elif kind == "line":
        b.line(x, y, _num(obj.get("x2")), _num(obj.get("y2")),
               _to_rgb(obj.get("color"), (0.0, 0.0, 0.0)),
               line_width=_num(obj.get("lineWidth"), 1.0))

    elif kind == "text":
        size = _num(obj.get("size"), 12.0)
        b.text(x, y, str(obj.get("text") or ""), size,
               _to_rgb(obj.get("color"), (0.0, 0.0, 0.0)),
               base_font=obj.get("font") or "Helvetica")

def _attach_font_resource(doc, page_ref, font_map, overwrite=False):
    page = doc.resolve(page_ref)
    if not isinstance(page, dict):
        return
    res = page.get("Resources")
    if isinstance(res, Ref):
        res = doc.resolve(res)
    if not isinstance(res, dict):
        res = {}
        page["Resources"] = res

    fonts = res.get("Font")
    if isinstance(fonts, Ref):
        fonts = doc.resolve(fonts)
    if not isinstance(fonts, dict):
        fonts = {}
        res["Font"] = fonts

    for alias, val in (font_map or {}).items():
        if alias in fonts and not overwrite:
            continue
        fonts[alias] = val
